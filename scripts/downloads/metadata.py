"""Normalize lake metadata into download-friendly records.

The lake index is mostly legacy (schema 3.0.0) and its tournament/team names
are source strings, not canonical labels. This module adds the derived fields
the download catalog needs without rewriting the match JSONs.
"""
from __future__ import annotations

import csv
import json
import re
import tarfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from src.source_facts import source_only_timed_out
from src.validator import count_team_wickets

EXPORT_FIELDS = [
    "filename", "match_id", "date", "format", "format_bucket", "schema_version",
    "record_class", "playing_format", "international_status", "classification_status",
    "record_stats_eligible", "gender", "competition", "season", "team1", "team2",
    "winner", "result", "toss_winner", "toss_decision", "venue", "city",
    "venue_country", "total_balls", "total_runs", "total_wickets",
    "wagon_wheel_balls", "pitch_line_balls", "shot_type_balls",
    "level", "format_family", "year", "competition_id", "competition_name",
    "competition_type", "team_ids", "published_at",
]

COMPETITION_TYPE_LABELS = {
    "club": "Club competitions",
    "international_event": "International events",
    "domestic": "Domestic competitions",
    "bilateral": "Bilateral series",
    "other": "Other competitions",
}

FORMAT_FAMILY = {
    "Test": "Test",
    "ODI": "ODI",
    "T20I": "T20I",
    "T20": "T20",
    "IPL": "T20",
    "FC": "First-Class",
    "ListA": "List A",
    "OtherT20": "Other",
    "OtherODM": "Other",
    "OtherMDM": "Other",
    "Unknown": "Other",
}

INTERNATIONAL_BUCKETS = {"Test", "ODI", "T20I"}
DOMESTIC_BUCKETS = {"T20", "IPL", "FC", "ListA", "OtherT20", "OtherODM", "OtherMDM"}


def load_json(path: Path, default=None):
    if not path or not Path(path).exists():
        return default
    return json.loads(Path(path).read_text(encoding="utf-8"))


def slugify(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    return value.strip("_") or "other"


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def delta_stamp(name: str) -> datetime | None:
    """Parse both 20261025T20 and 20261026T120000Z-123 names."""
    match = re.search(r"lake_delta_(\d{8})T(\d{2})(\d{2})?(\d{2})?Z?", name)
    if not match:
        return None
    raw = match.group(1) + match.group(2) + (match.group(3) or "00") + (match.group(4) or "00")
    try:
        return datetime.strptime(raw, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def asset_times(path: Path | None) -> dict[str, str]:
    """Map release asset name -> uploaded timestamp from `gh release view --json assets`."""
    payload = load_json(path, {}) or {}
    assets = payload.get("assets", payload if isinstance(payload, list) else [])
    out: dict[str, str] = {}
    for asset in assets:
        name = asset.get("name")
        stamp = asset.get("updatedAt") or asset.get("createdAt")
        if name and stamp:
            out[name] = stamp
    return out


def recovery_time(batches_assets: Path | None, fallback: str) -> str:
    payload = load_json(batches_assets, {}) or {}
    assets = payload.get("assets", payload if isinstance(payload, list) else [])
    times = [
        asset.get("updatedAt") or asset.get("createdAt")
        for asset in assets
        if "recovery_" in (asset.get("name") or "")
    ]
    times = [t for t in times if t]
    return max(times) if times else fallback


def derive_level(row: dict) -> str:
    status = (row.get("international_status") or "").strip().lower()
    if status == "international":
        return "international"
    if status == "non_international":
        return "domestic"
    bucket = row.get("format_bucket") or ""
    if bucket in INTERNATIONAL_BUCKETS:
        return "international"
    if bucket in DOMESTIC_BUCKETS:
        return "domestic"
    return "unknown"


def format_family(row: dict) -> str:
    return FORMAT_FAMILY.get(row.get("format_bucket") or "", "Other")


class CompetitionRegistry:
    def __init__(self, entries: list[dict]):
        self.entries = entries
        self.by_id = {entry["id"]: entry for entry in entries}
        self.alias = {}
        self.patterns = []
        for entry in entries:
            for alias in entry.get("aliases", []):
                self.alias[alias.strip().lower()] = entry
            for pattern in entry.get("patterns", []):
                self.patterns.append((re.compile(pattern, re.IGNORECASE), entry))

    def match(self, name: str | None) -> dict:
        raw = (name or "").strip()
        if raw:
            exact = self.alias.get(raw.lower())
            if exact:
                return exact
            for pattern, entry in self.patterns:
                if pattern.search(raw):
                    return entry
        return {
            "id": f"other_{slugify(raw)}" if raw else "other",
            "name": raw or "Other",
            "type": "other",
            "browse": False,
        }

    def browse_entries(self) -> list[dict]:
        return [entry for entry in self.entries if entry.get("browse")]


class TeamRegistry:
    def __init__(self, entries: list[dict]):
        self.entries = entries
        self.by_id = {entry["id"]: entry for entry in entries}
        self.alias = {}
        for entry in entries:
            for alias in entry.get("aliases", []):
                self.alias[alias.strip().lower()] = entry

    def match(self, name: str | None) -> dict | None:
        return self.alias.get((name or "").strip().lower())


def _teams_from_match(data: dict) -> tuple[str, str]:
    innings = data.get("innings") or []
    if innings:
        return innings[0].get("batting_team") or "", innings[0].get("bowling_team") or ""
    teams = [t for t in (data.get("playing_xi") or {}).keys() if not t.startswith("_")]
    return (teams[0] if teams else ""), (teams[1] if len(teams) > 1 else "")


def _metrics_from_match(data: dict) -> dict:
    innings = data.get("innings") or []
    total_balls = total_runs = total_wickets = 0
    wagon = pitch = shot = 0
    for inn in innings:
        deliveries = [d for over in inn.get("overs") or [] for d in over.get("deliveries") or []]
        total_balls += len(deliveries)
        total_runs += inn.get("innings_penalty_runs", 0)
        total_runs += sum((d.get("runs") or {}).get("total", 0) for d in deliveries)
        try:
            total_wickets += count_team_wickets(deliveries)
            total_wickets += source_only_timed_out(deliveries, inn.get("source_scorecard") or {})
        except (ValueError, KeyError, TypeError):
            # A slice's aggregate should not fail because one match is unusual.
            pass
        for delivery in deliveries:
            tactical = delivery.get("tactical") or {}
            if tactical.get("wagon_coords") is not None:
                wagon += 1
            if tactical.get("pitch_line") is not None:
                pitch += 1
            if tactical.get("shot_type") is not None:
                shot += 1
    return {
        "total_balls": total_balls, "total_runs": total_runs, "total_wickets": total_wickets,
        "wagon_wheel_balls": wagon, "pitch_line_balls": pitch, "shot_type_balls": shot,
    }


def record_from_match_json(match_id: str, path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    meta = data.get("meta") or {}
    dates = meta.get("dates") or []
    team1, team2 = _teams_from_match(data)
    outcome = data.get("outcome") or {}
    toss = data.get("toss") or {}
    winner = outcome.get("winner") or ""
    by_runs, by_wkts = outcome.get("win_by_runs"), outcome.get("win_by_wickets")
    if by_runs:
        result = f"{winner} won by {by_runs} runs"
    elif by_wkts:
        result = f"{winner} won by {by_wkts} wickets"
    else:
        result = outcome.get("result") or (f"{winner} won" if winner else "No result")
    metrics = _metrics_from_match(data)
    return {
        "filename": f"{match_id}.json",
        "match_id": str(match_id),
        "date": dates[0] if dates else "",
        "format": meta.get("format") or "Unknown",
        "format_bucket": meta.get("format_bucket") or meta.get("format") or "Unknown",
        "schema_version": meta.get("schema_version") or "unknown",
        "record_class": meta.get("record_class") or "unknown",
        "playing_format": meta.get("playing_format") or "unknown",
        "international_status": meta.get("international_status") or "unknown",
        "classification_status": meta.get("classification_status") or "legacy_unverified",
        "record_stats_eligible": meta.get("record_stats_eligible"),
        "gender": meta.get("gender") or "unknown",
        "competition": meta.get("competition") or "",
        "season": str(meta.get("season") or ""),
        "team1": team1, "team2": team2,
        "winner": winner, "result": result,
        "toss_winner": toss.get("winner") or "", "toss_decision": toss.get("decision") or "",
        "venue": meta.get("venue") or "", "city": meta.get("city") or "",
        "venue_country": meta.get("venue_country") or "",
        **metrics,
    }


def index_rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def enrich(row: dict, comps: CompetitionRegistry, teams: TeamRegistry, published_at: str | None) -> dict:
    row = dict(row)
    comp = comps.match(row.get("competition"))
    team_names = [row.get("team1") or "", row.get("team2") or ""]
    team_ids = sorted({entry["id"] for entry in (teams.match(name) for name in team_names) if entry})
    row.update({
        "level": derive_level(row),
        "format_family": format_family(row),
        "year": (parse_date(row.get("date")) or None).year if parse_date(row.get("date")) else "",
        "competition_id": comp["id"],
        "competition_name": comp["name"],
        "competition_type": comp["type"],
        "team_ids": "|".join(team_ids),
        "published_at": published_at or "",
    })
    return row


def write_records(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow({key: record.get(key, "") for key in EXPORT_FIELDS})


def read_records(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def extract_master(master_tar: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(master_tar, "r:gz") as tf:
        for member in tf:
            name = Path(member.name).name
            if not name.endswith(".json") or name in ("lake_manifest.json", "matches_index.csv"):
                continue
            if not member.isfile():
                continue
            stem = name[:-5]
            if not stem.isdigit():
                continue
            target = dest / name
            with tf.extractfile(member) as src, open(target, "wb") as out:
                out.write(src.read())


def overlay_deltas(deltas_dir: Path, dest: Path, asset_times_map: dict[str, str]) -> dict[str, str]:
    """Extract delta members over the master. Returns match_id -> published_at."""
    dest.mkdir(parents=True, exist_ok=True)
    published: dict[str, str] = {}
    archives = sorted(deltas_dir.glob("lake_delta_*.tar.gz"), key=lambda p: (asset_times_map.get(p.name) or "", p.name))
    for archive in archives:
        published_at = asset_times_map.get(archive.name)
        if not published_at:
            stamp = delta_stamp(archive.name)
            published_at = stamp.isoformat() if stamp else ""
        try:
            with tarfile.open(archive, "r:gz") as tf:
                for member in tf:
                    name = Path(member.name).name
                    if not name.endswith(".json") or not member.isfile():
                        continue
                    stem = name[:-5]
                    if not stem.isdigit():
                        continue
                    with tf.extractfile(member) as src, open(dest / name, "wb") as out:
                        out.write(src.read())
                    published[stem] = published_at
        except (tarfile.TarError, OSError):
            continue
    return published
