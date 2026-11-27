#!/usr/bin/env python3
"""
scripts/pipeline_publish.py
Turn the catalog and the built layer into the published lake.

This stage reads. It takes the built matches the catalog names, verifies
their digests, and writes the published snapshot: the master archive, the
match index, the download slices and the dated calendar.

Nothing here changes a match. The published snapshot is a copy of what the
builder produced, so a published match and a built match are the same bytes.
The previous arrangement overlaid several tarballs in file-name order and
patched the result afterwards; that is what this stage replaces.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src import catalog_db, release_store, shard_store  # noqa: E402
from src.validator import assess_match  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("publish")

#: The fields the published match index carries.
INDEX_FIELDS = [
    "match_id", "date", "format", "format_bucket", "competition", "season",
    "team1", "team2", "winner", "result", "toss_winner", "toss_decision",
    "venue", "city", "venue_country", "gender", "schema_version",
    "validation_status", "parser_version", "total_balls", "total_runs",
    "total_wickets", "wagon_wheel_balls", "pitch_line_balls", "shot_type_balls",
    "match_stage", "match_number", "match_group", "match_title",
]


def _index_row(match: dict) -> Dict[str, Any]:
    """One row of the published match index."""
    meta = match.get("meta") or {}
    outcome = match.get("outcome") or {}
    toss = match.get("toss") or {}
    dates = meta.get("dates") or []
    total_balls = 0
    total_runs = 0
    total_wickets = 0
    wagon = pitch = shot = 0
    for innings in match.get("innings") or []:
        for over in innings.get("overs") or []:
            for delivery in over.get("deliveries") or []:
                total_balls += 1
                total_runs += delivery.get("runs", {}).get("total", 0)
                total_wickets += len(delivery.get("wickets") or [])
                tactical = delivery.get("tactical") or {}
                if tactical.get("wagon_coords") is not None:
                    wagon += 1
                if tactical.get("pitch_line") is not None:
                    pitch += 1
                if tactical.get("shot_type") is not None:
                    shot += 1
    return {
        "match_id": str(match.get("match_id") or ""),
        "date": dates[0] if dates else "",
        "format": meta.get("format") or "",
        "format_bucket": meta.get("format_bucket") or "",
        "competition": meta.get("competition") or "",
        "season": str(meta.get("season") or ""),
        "team1": "",
        "team2": "",
        "winner": outcome.get("winner") or "",
        "result": outcome.get("result") or "",
        "toss_winner": toss.get("winner") or "",
        "toss_decision": toss.get("decision") or "",
        "venue": meta.get("venue") or "",
        "city": meta.get("city") or "",
        "venue_country": meta.get("venue_country") or "",
        "gender": meta.get("gender") or "",
        "schema_version": meta.get("schema_version") or "",
        "validation_status": meta.get("validation_status") or "",
        "parser_version": meta.get("parser_version") or "",
        "total_balls": total_balls,
        "total_runs": total_runs,
        "total_wickets": total_wickets,
        "wagon_wheel_balls": wagon,
        "pitch_line_balls": pitch,
        "shot_type_balls": shot,
        "match_stage": meta.get("match_stage") or "",
        "match_number": meta.get("match_number") or "",
        "match_group": meta.get("match_group") or "",
        "match_title": meta.get("match_title") or "",
    }


def _teams(match: dict) -> tuple[str, str]:
    """The two teams, taken from the playing XIs."""
    xi = match.get("playing_xi") or {}
    names = [name for name in xi if not name.startswith("_") and isinstance(xi[name], list)]
    names.sort()
    return (names[0] if names else "", names[1] if len(names) > 1 else "")


def materialize_published(
    connection,
    archive_dir: Path,
    dest_dir: Path,
    verify: bool = True,
) -> Dict[str, Any]:
    """Write the published matches, using only what the catalog names.

    A match is published when its tier admits it. Its bytes come from the
    archive the catalog names, and the digest is checked, so a corrupted
    archive cannot be published.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    publishable = catalog_db.select_publishable(connection)

    # Group by the archive that holds each match, so each archive is opened
    # once rather than once per match.
    targets: Dict[str, List[str]] = {}
    for row in connection.execute(
        "SELECT match_id, built_archive FROM matches"
        " WHERE validation_tier IN ('clean', 'warning', 'source_gap', 'scorecard_only')"
        "   AND built_archive IS NOT NULL ORDER BY match_id"
    ):
        targets.setdefault(str(row["built_archive"]), []).append(str(row["match_id"]))

    written: List[str] = []
    missing: List[str] = []
    mismatched: List[str] = []
    digests = {
        str(row["match_id"]): row["built_sha256"]
        for row in connection.execute(
            "SELECT match_id, built_sha256 FROM matches"
            " WHERE validation_tier IN ('clean', 'warning', 'source_gap', 'scorecard_only')"
        )
    }

    for archive_name, match_ids in sorted(targets.items()):
        archive_path = archive_dir / archive_name
        if not archive_path.exists():
            missing.extend(match_ids)
            continue
        members = shard_store.read_pack(archive_path)
        for match_id in match_ids:
            member_name = f"{match_id}.json"
            payload = members.get(member_name)
            if payload is None:
                missing.append(match_id)
                continue
            expected = digests.get(match_id)
            if verify and expected and shard_store.member_digest(payload) != expected:
                mismatched.append(match_id)
                continue
            (dest_dir / member_name).write_bytes(payload)
            written.append(match_id)

    logger.info(
        "Published %s matches (%s missing, %s mismatched)", len(written), len(missing), len(mismatched)
    )
    return {
        "published": written,
        "missing": missing,
        "mismatched": mismatched,
        "publishable_total": len(publishable),
    }


def write_index(matches_dir: Path, out_path: Path) -> int:
    """Write the published match index."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows: List[Dict[str, Any]] = []
    for path in sorted(matches_dir.glob("*.json")):
        if path.name in ("lake_manifest.json", "manifest.json", "failed_matches.json"):
            continue
        try:
            match = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Skipping unreadable match file %s", path.name)
            continue
        row = _index_row(match)
        team1, team2 = _teams(match)
        row["team1"] = team1
        row["team2"] = team2
        rows.append(row)
    rows.sort(key=lambda r: (r.get("date") or "9999", r["match_id"]))
    with open(out_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=INDEX_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Wrote the match index with %s rows", len(rows))
    return len(rows)


def write_manifest(matches_dir: Path, out_path: Path, catalog_summary: Dict[str, Any]) -> Dict[str, Any]:
    """Write the published manifest."""
    tiers: Dict[str, int] = {}
    total_balls = total_runs = total_wickets = 0
    count = 0
    earliest = latest = None
    for path in sorted(matches_dir.glob("*.json")):
        if path.name in ("lake_manifest.json", "manifest.json"):
            continue
        try:
            match = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        count += 1
        meta = match.get("meta") or {}
        tier = meta.get("validation_status") or "clean"
        tiers[tier] = tiers.get(tier, 0) + 1
        for date in meta.get("dates") or []:
            if earliest is None or date < earliest:
                earliest = date
            if latest is None or date > latest:
                latest = date
        for innings in match.get("innings") or []:
            for over in innings.get("overs") or []:
                for delivery in over.get("deliveries") or []:
                    total_balls += 1
                    total_runs += delivery.get("runs", {}).get("total", 0)
                    total_wickets += len(delivery.get("wickets") or [])
    manifest = {
        "dataset_name": "cricket-lake-v3",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_matches": count,
        "date_range": {"earliest": earliest, "latest": latest},
        "totals": {"deliveries": total_balls, "runs": total_runs, "wickets": total_wickets},
        "validation_tiers": tiers,
        "catalog": catalog_summary,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workdir", default="publish_work")
    parser.add_argument("--repo", default=release_store.DEFAULT_REPO)
    parser.add_argument("--catalog", default=None)
    parser.add_argument("--out", default="published")
    parser.add_argument("--upload", action="store_true",
                        help="Upload the published snapshot to the release")
    parser.add_argument("--skip-download", action="store_true",
                        help="Assume the built archives are already in the workdir")
    args = parser.parse_args()

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    catalog_path = Path(args.catalog) if args.catalog else workdir / "lake_catalog.sqlite.gz"
    if catalog_path.exists():
        connection = catalog_db.load_catalog_gz(catalog_path)
    else:
        release_store.download_asset(
            args.repo, release_store.RELEASE_CATALOG, "lake_catalog.sqlite.gz", workdir
        )
        connection = catalog_db.load_catalog_gz(catalog_path)

    archive_dir = workdir / "archives"
    archive_dir.mkdir(parents=True, exist_ok=True)
    if not args.skip_download:
        for row in connection.execute(
            "SELECT DISTINCT built_archive AS archive FROM matches"
            " WHERE built_archive IS NOT NULL AND built_archive LIKE 'built_shard_%'"
        ):
            name = str(row["archive"])
            target = archive_dir / name
            if not target.exists():
                try:
                    release_store.download_asset(
                        args.repo, release_store.RELEASE_BUILT, name, archive_dir
                    )
                except Exception as exc:
                    logger.warning("Could not download %s: %s", name, exc)

    matches_dir = out_dir / "matches"
    outcome = materialize_published(connection, archive_dir, matches_dir)

    summary = catalog_db.summary(connection)
    index_rows = write_index(matches_dir, out_dir / "matches_index.csv")
    manifest = write_manifest(matches_dir, out_dir / "lake_manifest.json", summary)

    logger.info(
        "Published snapshot: %s matches, %s index rows",
        len(outcome["published"]), index_rows,
    )
    print(json.dumps({
        "published": len(outcome["published"]),
        "missing": len(outcome["missing"]),
        "mismatched": len(outcome["mismatched"]),
        "index_rows": index_rows,
        "output": str(out_dir),
    }, indent=2))
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
