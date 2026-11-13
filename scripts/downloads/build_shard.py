#!/usr/bin/env python3
"""Build one of the 20 download shards."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import tarfile
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .metadata import EXPORT_FIELDS, read_records
from src.stage import apply_stage_index_to_dir

README_TEMPLATE = """Cricket Lake — {title}

{description}

Slice id      : {sid}
Group         : {group} / {subgroup}
Filters       : {filters}
Matches       : {count}
Date range    : {earliest} to {latest}
Schema mix    : {schemas}
Gender mix    : {genders}
Validation    : {validation}
Generated at  : {generated_at}

CONTENTS
  <match_id>.json    Ball-by-ball Match JSON v3 (schema versions above)
  matches_index.csv  One row per match, with the slice filters applied
  lake_manifest.json Slice counts and coverage

INCLUDED MATCHES
{match_note}
{match_table}

MATCH INDEX
  matches_index.csv is the complete, machine-readable list: one row per match
  with match_id, date, format, team1, team2, winner, result, competition,
  venue, city, season, total_balls, total_runs, total_wickets,
  wagon_wheel_balls, pitch_line_balls, shot_type_balls, schema_version,
  validation_status, gender and level.

NOTES
  * Quarantined matches are excluded from the public lake.
{validation_note}
  * Played windows use the first date in meta.dates (the start date for
    multi-day matches). Added windows use the date a match entered the lake.
  * The original 16.4k backfill predates the incremental feed, so older matches
    do not appear in the "added" windows.
  * All-match slices include the small women's set. Women's-only slices are
    withheld for v1 until the T2 backfill is run.
  * Source: ESPNcricinfo match pages via cricket-lake-unfiltered.

Verify this archive with the sha256 in the catalog's checksums.txt.
"""


def _selected_tar_extract(tar_path: Path, wanted: set[str], dest: Path) -> None:
    with tarfile.open(tar_path, "r:gz") as tf:
        for member in tf:
            name = Path(member.name).name
            if not name.endswith(".json") or not member.isfile():
                continue
            stem = name[:-5]
            if stem not in wanted:
                continue
            with tf.extractfile(member) as src, open(dest / name, "wb") as out:
                out.write(src.read())


def _overlay_selected(deltas_dir: Path, wanted: set[str], dest: Path) -> None:
    for archive in sorted(deltas_dir.glob("lake_delta_*.tar.gz")):
        try:
            _selected_tar_extract(archive, wanted, dest)
        except (tarfile.TarError, OSError):
            continue


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clip(value: object, width: int) -> str:
    text = " ".join(str(value or "—").split())
    return text if len(text) <= width else text[: width - 3].rstrip() + "..."


def _team_label(row: dict) -> str:
    team1 = str(row.get("team1") or "").strip()
    team2 = str(row.get("team2") or "").strip()
    if team1 and team2:
        return f"{team1} vs {team2}"
    return team1 or team2 or "—"


def _venue_label(row: dict) -> str:
    parts = [str(row.get(key) or "").strip() for key in ("venue", "city")]
    return ", ".join(part for part in parts if part) or "—"


def _readme_matches(rows: list[dict]) -> tuple[str, str]:
    """A compact match list for the README; the CSV always holds all rows."""
    if not rows:
        return "No matches in this slice.", "  (empty)"
    sample = rows if len(rows) <= 100 else rows[:20] + rows[-20:]
    if len(rows) <= 100:
        note = f"All {len(rows):,} {'match' if len(rows) == 1 else 'matches'} in this slice are listed below."
    else:
        note = (
            f"Showing the first and last 20 of {len(rows):,} matches. "
            "The complete list is matches_index.csv."
        )
    widths = (10, 10, 6, 32, 34, 30)
    headers = ("match_id", "date", "format", "teams", "result", "venue")
    lines = [
        "  " + "  ".join(header.ljust(width) for header, width in zip(headers, widths)),
        "  " + "  ".join("-" * width for width in widths),
    ]
    for row in sample:
        cells = (
            str(row.get("match_id") or ""),
            str(row.get("date") or ""),
            str(row.get("format") or ""),
            _team_label(row),
            str(row.get("result") or "—"),
            _venue_label(row),
        )
        lines.append("  " + "  ".join(_clip(cell, width).ljust(width) for cell, width in zip(cells, widths)))
    if len(rows) > 100:
        lines.append("  ...")
    return note, "\n".join(lines)


def _slice_manifest(slice_def: dict, rows: list[dict], generated_at: str) -> dict:
    schemas = Counter(row.get("schema_version") or "unknown" for row in rows)
    formats = Counter(row.get("format_family") or "Other" for row in rows)
    levels = Counter(row.get("level") or "unknown" for row in rows)
    genders = Counter(row.get("gender") or "unknown" for row in rows)
    statuses = Counter(row.get("validation_status") or "clean" for row in rows)
    dates = sorted({row["date"] for row in rows if row.get("date") and row["date"] != "Unknown"})
    return {
        "dataset": "cricket-lake-v3",
        "generated_at": generated_at,
        "slice": {
            "id": slice_def["id"],
            "title": slice_def["title"],
            "group": slice_def["group"],
            "subgroup": slice_def["subgroup"],
            "filters": slice_def["filters"],
        },
        "total_matches": len(rows),
        "date_range": {"earliest": dates[0] if dates else None, "latest": dates[-1] if dates else None},
        "schema_versions": dict(schemas),
        "formats": dict(formats),
        "levels": dict(levels),
        "genders": dict(genders),
        "validation": {
            "statuses": dict(statuses),
            "warning_matches": statuses.get("warning", 0),
            "source_gap_matches": statuses.get("source_gap", 0),
        },
        "totals": {
            "deliveries": sum(int(row.get("total_balls") or 0) for row in rows),
            "runs": sum(int(row.get("total_runs") or 0) for row in rows),
            "wickets": sum(int(row.get("total_wickets") or 0) for row in rows),
        },
        "coverage": {
            "quarantined_excluded": True,
            "warning_matches": statuses.get("warning", 0),
            "source_gap_matches": statuses.get("source_gap", 0),
            "note": "Quarantined matches are excluded; warning/source-gap records carry their caveats in meta.validation_warnings.",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build one download shard")
    parser.add_argument("--plan-dir", default="download_plan")
    parser.add_argument("--shard", type=int, required=True)
    parser.add_argument("--master-tar", required=True)
    parser.add_argument("--deltas-dir", required=True)
    parser.add_argument("--stage-index", default=None, help="Optional ESPN stage index to apply after delta overlay")
    parser.add_argument("--workdir", default="download_work")
    parser.add_argument("--out", default="downloads_out")
    parser.add_argument("--generated-at", default=None)
    args = parser.parse_args()

    plan = Path(args.plan_dir)
    slices = json.loads((plan / "slices.json").read_text(encoding="utf-8"))
    shards = json.loads((plan / "shards.json").read_text(encoding="utf-8"))
    rows = read_records(plan / "records.csv")
    by_id = {row["match_id"]: row for row in rows}

    shard = next((item for item in shards if item["shard"] == args.shard), None)
    if shard is None:
        raise SystemExit(f"shard {args.shard} not in plan")
    shard_slices = [item for item in slices if item["id"] in set(shard["slice_ids"])]
    needed = {mid for item in shard_slices for mid in item["ids"]}

    workdir = Path(args.workdir)
    matches_dir = workdir / "matches"
    matches_dir.mkdir(parents=True, exist_ok=True)
    if needed:
        print(f"Extracting {len(needed)} match files...")
        _selected_tar_extract(Path(args.master_tar), needed, matches_dir)
        _overlay_selected(Path(args.deltas_dir), needed, matches_dir)
        if args.stage_index:
            apply_stage_index_to_dir(matches_dir, args.stage_index, only_ids=needed)

    out = Path(args.out)
    zips_dir = out / "zips"
    zips_dir.mkdir(parents=True, exist_ok=True)
    meta = json.loads((plan / "meta.json").read_text(encoding="utf-8"))
    generated_at = args.generated_at or meta.get("generated_at") or datetime.now(timezone.utc).isoformat()
    fragment = []

    for item in shard_slices:
        present = [mid for mid in item["ids"] if (matches_dir / f"{mid}.json").exists()]
        slice_rows = [by_id[mid] for mid in present if mid in by_id]
        if not present:
            continue
        zip_path = zips_dir / f"{item['id']}.zip"
        manifest = _slice_manifest(item, slice_rows, generated_at)
        match_note, match_table = _readme_matches(slice_rows)
        statuses = manifest["validation"]["statuses"]
        validation_label = " · ".join(
            f"{name} {count:,}" for name, count in sorted(statuses.items())
        ) or "clean"
        flagged = manifest["validation"]["warning_matches"] + manifest["validation"]["source_gap_matches"]
        if flagged:
            validation_note = (
                f"  * Validation tiers in this slice: "
                f"{manifest['validation']['warning_matches']:,} warning, "
                f"{manifest['validation']['source_gap_matches']:,} source-gap. "
                "Those records carry their exact caveats in meta.validation_warnings; "
                "scorecard and ball-by-ball totals differ as described there."
            )
        else:
            validation_note = "  * All matches in this slice pass the structural validator."
        readme = README_TEMPLATE.format(
            title=item["title"],
            description=item["description"],
            sid=item["id"],
            group=item["group"],
            subgroup=item["subgroup"],
            filters=json.dumps(item["filters"], sort_keys=True),
            count=len(present),
            earliest=(manifest["date_range"]["earliest"] or "unknown"),
            latest=(manifest["date_range"]["latest"] or "unknown"),
            schemas=json.dumps(manifest["schema_versions"], sort_keys=True),
            genders=json.dumps(manifest["genders"], sort_keys=True),
            validation=validation_label,
            validation_note=validation_note,
            generated_at=generated_at,
            match_note=match_note,
            match_table=match_table,
        )
        index_path = out / f"{item['id']}.csv"
        with open(index_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for row in slice_rows:
                writer.writerow({key: row.get(key, "") for key in EXPORT_FIELDS})

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.writestr("README.txt", readme)
            zf.write(index_path, "matches_index.csv")
            zf.writestr("lake_manifest.json", json.dumps(manifest, indent=2))
            for mid in present:
                zf.write(matches_dir / f"{mid}.json", f"{mid}.json")
        index_path.unlink(missing_ok=True)

        size = zip_path.stat().st_size
        fragment.append({
            "id": item["id"],
            "title": item["title"],
            "group": item["group"],
            "subgroup": item["subgroup"],
            "subgroup_order": item.get("subgroup_order", 0),
            "description": item["description"],
            "filters": item["filters"],
            "count": len(present),
            "size_bytes": size,
            "sha256": _sha256(zip_path),
            "url": f"zips/{item['id']}.zip",
            "date_range": manifest["date_range"],
            "schema_versions": manifest["schema_versions"],
            "formats": manifest["formats"],
            "levels": manifest["levels"],
            "genders": manifest["genders"],
            "validation": manifest["validation"],
            "generated_at": generated_at,
        })
        print(f"built {item['id']}: {len(present)} matches, {size / 1024 / 1024:.2f} MB")

    (out / f"fragment_{args.shard}.json").write_text(
        json.dumps(fragment, indent=2), encoding="utf-8"
    )
    print(json.dumps({"shard": args.shard, "slices": len(fragment), "status": "done"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
