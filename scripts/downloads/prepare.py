#!/usr/bin/env python3
"""Prepare the download catalog: records, slice definitions and 20 shards."""
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
    asset_times,
    enrich,
    extract_master,
    index_rows,
    load_json,
    overlay_deltas,
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
    parser.add_argument("--master-tar", required=True)
    parser.add_argument("--master-index", required=True)
    parser.add_argument("--master-manifest", default=None)
    parser.add_argument("--deltas-dir", required=True)
    parser.add_argument("--delta-assets", default=None)
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

    print("Extracting master archive...")
    extract_master(Path(args.master_tar), matches_dir)

    times = asset_times(Path(args.delta_assets) if args.delta_assets else None)
    print("Overlaying delta archives...")
    delta_published = overlay_deltas(Path(args.deltas_dir), matches_dir, times)
    recovery = recovery_time(Path(args.batches_assets) if args.batches_assets else None, args.recovery_time)

    comps = CompetitionRegistry(load_json(Path(args.config_dir) / "competitions.json", []))
    teams = TeamRegistry(load_json(Path(args.config_dir) / "teams.json", []))
    config = load_json(Path(args.config_dir) / "catalog.json")

    rows = index_rows(Path(args.master_index))
    by_id = {str(row["match_id"]): row for row in rows}

    records: dict[str, dict] = {}
    for row in rows:
        mid = str(row["match_id"])
        published = recovery if row.get("schema_version") == "3.3.0" else ""
        records[mid] = enrich(row, comps, teams, published)

    # Delta JSON is the latest version of these matches, whether or not the id
    # was already in the master index when the index was built.
    for mid, published_at in delta_published.items():
        path = matches_dir / f"{mid}.json"
        if not path.exists():
            continue
        row = record_from_match_json(mid, path)
        records[mid] = enrich(row, comps, teams, published_at)

    ordered = sorted(
        records.values(),
        key=lambda r: (r.get("date") or "9999", str(r.get("match_id"))),
    )

    slices, shards = build_slices(ordered, comps, teams, config, now)
    write_records(out_dir / "records.csv", ordered)
    (out_dir / "slices.json").write_text(json.dumps(slices, indent=2), encoding="utf-8")
    (out_dir / "shards.json").write_text(json.dumps(shards, indent=2), encoding="utf-8")

    manifest = load_json(Path(args.master_manifest), {}) if args.master_manifest else {}
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
        "excluded_quarantined": ((manifest.get("coverage") or {}).get("quarantined_matches")),
        "slice_count": len(slices),
        "shard_count": len(shards),
        "group_order": GROUP_ORDER,
        "group_descriptions": GROUP_DESCRIPTIONS,
        "notes": [
            "Only clean, published matches are included. Quarantined matches remain excluded.",
            "Men-only slices are listed separately; all-match slices include the small women's set.",
            "Women's-only downloads are withheld for v1 until the T2 backfill is run.",
            "Added windows cover matches first published by the delta/recovery pipeline; the original 16.4k backfill predates this feed.",
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
