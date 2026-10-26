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

README_TEMPLATE = """Cricket Lake — {title}

{description}

Slice id      : {sid}
Group         : {group} / {subgroup}
Filters       : {filters}
Matches       : {count}
Date range    : {earliest} to {latest}
Schema mix    : {schemas}
Generated at  : {generated_at}

CONTENTS
  <match_id>.json    Ball-by-ball Match JSON v3 (schema versions above)
  matches_index.csv  One row per match, with the slice filters applied
  lake_manifest.json Slice counts and coverage

NOTES
  * Only clean, published matches are included. Quarantined matches are excluded
    from the public lake.
  * Played windows use the first date in meta.dates (the start date for
    multi-day matches). Added windows use the date a match entered the lake.
  * The original 16.4k backfill predates the incremental feed, so older matches
    do not appear in the "added" windows.
  * Women's-only downloads are withheld for v1 until the T2 backfill is run.
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


def _slice_manifest(slice_def: dict, rows: list[dict], generated_at: str) -> dict:
    schemas = Counter(row.get("schema_version") or "unknown" for row in rows)
    formats = Counter(row.get("format_family") or "Other" for row in rows)
    levels = Counter(row.get("level") or "unknown" for row in rows)
    genders = Counter(row.get("gender") or "unknown" for row in rows)
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
        "totals": {
            "deliveries": sum(int(row.get("total_balls") or 0) for row in rows),
            "runs": sum(int(row.get("total_runs") or 0) for row in rows),
            "wickets": sum(int(row.get("total_wickets") or 0) for row in rows),
        },
        "coverage": {
            "quarantined_excluded": True,
            "note": "Public lake excludes quarantined matches.",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build one download shard")
    parser.add_argument("--plan-dir", default="download_plan")
    parser.add_argument("--shard", type=int, required=True)
    parser.add_argument("--master-tar", required=True)
    parser.add_argument("--deltas-dir", required=True)
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
            generated_at=generated_at,
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
