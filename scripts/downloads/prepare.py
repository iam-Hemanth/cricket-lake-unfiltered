#!/usr/bin/env python3
"""Prepare the download catalog: records, slice definitions and 20 shards.

The published snapshot is the only input. The previous version extracted the
master archive and then overlaid the delta archives over it, so a match could
end up with a different body depending on which archives happened to be
present. The published snapshot is already the resolved result, and reading
it alone removes that whole class of ordering problem.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .metadata import (
    EXPORT_FIELDS,
    CompetitionRegistry,
    TeamRegistry,
    enrich,
    extract_master,
    index_rows,
    load_json,
    parse_date,
    record_from_match_json,
    recovery_time,
    write_records,
)
from .slices import GROUP_DESCRIPTIONS, GROUP_ORDER, build_slices


def _now(value: str | None) -> datetime:
    if value:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return datetime.now(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare download records and slice plan")
    parser.add_argument("--master-tar", required=True,
                        help="The published master archive")
    parser.add_argument("--master-index", required=True,
                        help="The published match index")
    parser.add_argument("--master-manifest", default=None)
    parser.add_argument("--batches-assets", default=None)
    parser.add_argument("--recovery-time", default="2026-10-24T20:24:00Z")
    parser.add_argument("--config-dir", default="downloads/config")
    parser.add_argument("--workdir", default="download_work")
    parser.add_argument("--out", default="download_plan")
    parser.add_argument("--now", default=None)
    args = parser.parse_args()

    now = _now(args.now)
    workdir = Path(args.workdir)
    matches_dir = workdir / "matches"
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Extracting the published snapshot...")
    extract_master(Path(args.master_tar), matches_dir)

    recovery = recovery_time(Path(args.batches_assets) if args.batches_assets else None, args.recovery_time)

    comps = CompetitionRegistry(load_json(Path(args.config_dir) / "competitions.json", []))
    teams = TeamRegistry(load_json(Path(args.config_dir) / "teams.json", []))
    config = load_json(Path(args.config_dir) / "catalog.json")

    rows = index_rows(Path(args.master_index))

    records: dict[str, dict] = {}
    for row in rows:
        mid = str(row["match_id"])
        first_published = (row.get("first_published_at") or "").strip()
        built_updated = (row.get("built_updated_at") or "").strip()
        published = first_published or (
            recovery if row.get("schema_version") == "3.3.0" else ""
        )
        updated = max(
            [value for value in (first_published, built_updated) if value],
            default=published,
        )
        records[mid] = enrich(row, comps, teams, published, updated)

    # A match the index does not list but the snapshot holds is read from the
    # snapshot itself, so the index and the archive cannot disagree about
    # which matches exist.
    for path in sorted(matches_dir.glob("*.json")):
        mid = path.stem
        if mid in records or not mid.isdigit():
            continue
        row = record_from_match_json(mid, path)
        records[mid] = enrich(row, comps, teams, recovery, recovery)

    # The download catalog is men's matches only. The lake itself stays
    # unfiltered; the consumer-facing slices and index carry no women's
    # records.
    records = {
        mid: row for mid, row in records.items()
        if (row.get("gender") or "") == "male"
    }

    ordered = sorted(
        records.values(),
        key=lambda r: (r.get("date") or "9999", str(r.get("match_id"))),
    )

    slices, shards = build_slices(ordered, comps, teams, config, now)
    write_records(out_dir / "records.csv", ordered)
    (out_dir / "slices.json").write_text(json.dumps(slices, indent=2), encoding="utf-8")
    (out_dir / "shards.json").write_text(json.dumps(shards, indent=2), encoding="utf-8")

    manifest = load_json(Path(args.master_manifest), {}) if args.master_manifest else {}
    coverage = manifest.get("coverage") or {}
    warned = coverage.get("warning_matches") or 0
    source_gap = coverage.get("source_gap_matches") or 0
    scorecard_only = coverage.get("scorecard_only_matches") or 0
    quarantined = coverage.get("quarantined_matches")
    validation_notes = []
    if warned:
        validation_notes.append(
            f"{warned} published matches carry recorded source warnings "
            "(meta.validation_status = warning); their warning lists are in the JSON."
        )
    if source_gap:
        validation_notes.append(
            f"{source_gap} published matches are flagged source-gap records "
            "(meta.validation_status = source_gap): source totals and ball-by-ball "
            "differ by a small, disclosed amount."
        )
    if scorecard_only:
        validation_notes.append(
            f"{scorecard_only} published matches are scorecard-only records "
            "(meta.validation_status = scorecard_only): ESPN published the scorecard "
            "but no ball-by-ball commentary for them."
        )
    if quarantined:
        validation_notes.append(f"{quarantined} quarantined matches remain excluded.")
    schema_versions = Counter(r.get("schema_version") or "unknown" for r in ordered)
    formats = Counter(r.get("format_family") or "Other" for r in ordered)
    levels = Counter(r.get("level") or "unknown" for r in ordered)
    genders = Counter(r.get("gender") or "unknown" for r in ordered)
    dates = sorted({r["date"] for r in ordered if parse_date(r.get("date"))})
    meta = {
        "generated_at": now.isoformat(),
        "dataset": "cricket-lake-v3",
        "source": "iam-Hemanth/cricket-lake-unfiltered",
        "total_matches": len(ordered),
        "with_published_at": sum(1 for r in ordered if r.get("published_at")),
        "schema_versions": dict(schema_versions),
        "formats": dict(formats),
        "levels": dict(levels),
        "genders": dict(genders),
        "date_range": {"earliest": dates[0] if dates else None, "latest": dates[-1] if dates else None},
        "excluded_quarantined": quarantined,
        "warning_matches": warned,
        "source_gap_matches": source_gap,
        "scorecard_only_matches": scorecard_only,
        "slice_count": len(slices),
        "shard_count": len(shards),
        "group_order": GROUP_ORDER,
        "group_descriptions": GROUP_DESCRIPTIONS,
        "notes": [
            *validation_notes,
            "The download catalog covers men's matches only.",
            "Played windows use the first date in meta.dates; multi-day matches are counted from their start date.",
        ],
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print(json.dumps({
        "records": len(ordered),
        "slices": len(slices),
        "shards": len(shards),
        "with_published_at": meta["with_published_at"],
        "schema_versions": meta["schema_versions"],
        "output": str(out_dir),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
