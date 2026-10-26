"""Build the curated download slice definitions."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from .metadata import (
    COMPETITION_TYPE_LABELS,
    CompetitionRegistry,
    TeamRegistry,
    parse_date,
    parse_iso,
)

GROUP_ORDER = [
    "Recent", "Formats", "Years", "Tournaments", "Teams",
    "Countries", "Levels", "Complete",
]

GROUP_DESCRIPTIONS = {
    "Recent": "Rolling windows by match date and by the date a match entered the lake.",
    "Formats": "Every published match format.",
    "Years": "Calendar-year archives.",
    "Tournaments": "Canonical competitions; aliases and sponsor names are merged.",
    "Teams": "Curated international teams and franchises.",
    "Countries": "Matches grouped by host country (venue).",
    "Levels": "International versus domestic, derived where legacy metadata is blank.",
    "Complete": "The whole published lake in one archive.",
}


def _gender_match(record: dict, gender: str) -> bool:
    if gender == "male":
        return (record.get("gender") or "") == "male"
    return True


def _level_match(record: dict, level: str) -> bool:
    if level in ("international", "domestic"):
        return record.get("level") == level
    return True


def _cutoff(now: datetime, days: int) -> date:
    return (now - timedelta(days=days)).date()


def _matches(count: int) -> str:
    return f"{count} match" if count == 1 else f"{count} matches"


def _add(slices: list[dict], *, sid: str, title: str, group: str, subgroup: str,
         description: str, filters: dict, ids: list[str], subgroup_order: int = 0) -> None:
    if not ids:
        return
    slices.append({
        "id": sid,
        "title": title,
        "group": group,
        "subgroup": subgroup,
        "subgroup_order": subgroup_order,
        "description": description,
        "filters": filters,
        "count": len(ids),
        "ids": sorted(dict.fromkeys(ids)),
    })


def build_slices(
    records: list[dict],
    comps: CompetitionRegistry,
    teams: TeamRegistry,
    config: dict,
    now: datetime,
) -> tuple[list[dict], list[dict]]:
    slices: list[dict] = []
    gender_defs = config["genders"]
    level_defs = config["levels"]

    # --------------------------------------------------------------- recent played
    today = now.date()
    for days in config["played_days"]:
        cutoff = today - timedelta(days=days)
        for gender in gender_defs:
            for level in level_defs:
                ids = [
                    r["match_id"] for r in records
                    if (parse_date(r.get("date")) or date.min) >= cutoff
                    and _gender_match(r, gender["filter"])
                    and _level_match(r, level["filter"])
                ]
                _add(
                    slices,
                    sid=f"played_{days}d_{gender['id']}_{level['id']}",
                    title=f"Last {days} days — {gender['label']}",
                    group="Recent",
                    subgroup=f"Played — last {days} days",
                    description=f"Played · {level['label']} · {_matches(len(ids))}",
                    filters={"type": "played", "days": days, "gender": gender["id"], "level": level["id"]},
                    ids=ids, subgroup_order=1000 + days,
                )

    # ---------------------------------------------------------------- recent added
    for days in config["added_days"]:
        cutoff = _cutoff(now, days)
        for gender in gender_defs:
            for level in level_defs:
                ids = [
                    r["match_id"] for r in records
                    if (parse_iso(r.get("published_at")) or datetime.min.replace(tzinfo=now.tzinfo)).date() >= cutoff
                    and _gender_match(r, gender["filter"])
                    and _level_match(r, level["filter"])
                ]
                _add(
                    slices,
                    sid=f"added_{days}d_{gender['id']}_{level['id']}",
                    title=f"Added — last {days} days — {gender['label']}",
                    group="Recent",
                    subgroup=f"Added — last {days} days",
                    description=f"First published in the lake · {level['label']} · {_matches(len(ids))}",
                    filters={"type": "added", "days": days, "gender": gender["id"], "level": level["id"]},
                    ids=ids, subgroup_order=days,
                )

    # ------------------------------------------------------------------- formats
    for fmt_index, fmt in enumerate(config["formats"]):
        for gender in gender_defs:
            ids = [
                r["match_id"] for r in records
                if r.get("format_family") == fmt["family"]
                and _gender_match(r, gender["filter"])
            ]
            _add(
                slices,
                sid=f"format_{fmt['id']}_{gender['id']}",
                title=f"{fmt['label']} — {gender['label']}",
                group="Formats",
                subgroup=fmt["label"],
                description=f"{_matches(len(ids))} · {fmt['label']}",
                filters={"type": "format", "format": fmt["family"], "gender": gender["id"]},
                ids=ids, subgroup_order=fmt_index,
            )

    # --------------------------------------------------------------------- years
    years = sorted({int(r["year"]) for r in records if str(r.get("year", "")).isdigit()})
    for year in years:
        for gender in gender_defs:
            ids = [
                r["match_id"] for r in records
                if str(r.get("year")) == str(year) and _gender_match(r, gender["filter"])
            ]
            _add(
                slices,
                sid=f"year_{year}_{gender['id']}",
                title=f"{year} — {gender['label']}",
                group="Years",
                subgroup=str(year),
                description=f"{_matches(len(ids))} from {year}",
                filters={"type": "year", "year": year, "gender": gender["id"]},
                ids=ids, subgroup_order=year,
            )

    # --------------------------------------------------------------- tournaments
    comp_counts = Counter(r.get("competition_id") for r in records if r.get("competition_id"))
    comp_entries = [
        entry for entry in comps.browse_entries() if comp_counts.get(entry["id"], 0) > 0
    ]
    comp_entries.sort(key=lambda entry: comp_counts[entry["id"]], reverse=True)
    for entry in comp_entries[: int(config["competition_limit"])]:
        ids = [r["match_id"] for r in records if r.get("competition_id") == entry["id"]]
        _add(
            slices,
            sid=f"comp_{entry['id']}_all",
            title=f"{entry['name']} — All matches",
            group="Tournaments",
            subgroup=COMPETITION_TYPE_LABELS.get(entry["type"], "Other competitions"),
            description=f"{_matches(len(ids))} · {entry['name']}",
            filters={"type": "competition", "competition_id": entry["id"]},
            ids=ids, subgroup_order={"club": 0, "international_event": 1, "domestic": 2}.get(entry["type"], 3),
        )

    # ---------------------------------------------------------------------- teams
    team_counts: Counter = Counter()
    for record in records:
        for team_id in (record.get("team_ids") or "").split("|"):
            if team_id:
                team_counts[team_id] += 1
    team_entries = [entry for entry in teams.entries if team_counts.get(entry["id"], 0) > 0]
    team_entries.sort(key=lambda entry: team_counts[entry["id"]], reverse=True)
    for entry in team_entries[: int(config["team_limit"])]:
        ids = [
            r["match_id"] for r in records
            if entry["id"] in (r.get("team_ids") or "").split("|")
        ]
        _add(
            slices,
            sid=f"team_{entry['id']}_all",
            title=f"{entry['name']} — All matches",
            group="Teams",
            subgroup="International teams" if entry["type"] == "international" else "Franchises",
            description=f"{_matches(len(ids))} · {entry['name']}",
            filters={"type": "team", "team_id": entry["id"]},
            ids=ids, subgroup_order=0 if entry["type"] == "international" else 1,
        )

    # -------------------------------------------------------------------- countries
    country_counts = Counter(r.get("venue_country") for r in records if r.get("venue_country"))
    for index, (country, _) in enumerate(country_counts.most_common(int(config["country_limit"]))):
        ids = [r["match_id"] for r in records if r.get("venue_country") == country]
        slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in country).strip("_")
        _add(
            slices,
            sid=f"country_{slug}_all",
            title=f"Hosted in {country} — All matches",
            group="Countries",
            subgroup="Host countries",
            description=f"{_matches(len(ids))} · venue country {country}",
            filters={"type": "country", "venue_country": country},
            ids=ids, subgroup_order=index,
        )

    # ----------------------------------------------------------------------- levels
    for level_index, level in enumerate(level_defs):
        if level["id"] == "all":
            continue
        ids = [r["match_id"] for r in records if _level_match(r, level["filter"])]
        _add(
            slices,
            sid=f"level_{level['id']}_all",
            title=f"{level['label']} — All matches",
            group="Levels",
            subgroup=level["label"],
            description=f"All published {level['label'].lower()} matches",
            filters={"type": "level", "level": level["id"]},
            ids=ids, subgroup_order=level_index,
        )

    # --------------------------------------------------------------------- complete
    for gender in gender_defs:
        ids = [r["match_id"] for r in records if _gender_match(r, gender["filter"])]
        _add(
            slices,
            sid=f"complete_{gender['id']}",
            title=f"Complete lake — {gender['label']}",
            group="Complete",
            subgroup="Full lake",
            description=f"The complete published lake · {gender['label']}",
            filters={"type": "complete", "gender": gender["id"]},
            ids=ids, subgroup_order=gender_defs.index(gender),
        )

    # Deterministic presentation order, then greedy balance across shards.
    group_rank = {name: i for i, name in enumerate(GROUP_ORDER)}
    slices.sort(key=lambda s: (group_rank.get(s["group"], 99), s.get("subgroup_order", 0), s["subgroup"], s["title"]))
    shard_count = int(config.get("shards", 20))
    bins: list[dict[str, Any]] = [{"shard": i, "slice_ids": [], "estimate": 0} for i in range(shard_count)]
    for item in sorted(slices, key=lambda s: s["count"], reverse=True):
        target = min(bins, key=lambda b: b["estimate"])
        target["slice_ids"].append(item["id"])
        target["estimate"] += item["count"]
    return slices, bins
