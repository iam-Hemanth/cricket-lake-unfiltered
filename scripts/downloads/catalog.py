#!/usr/bin/env python3
"""Merge shard fragments into downloads.json, index.html and checksums."""
from __future__ import annotations

import argparse
import json
import shutil
from collections import defaultdict
from pathlib import Path



def load_json(path: Path, default=None):
    if not path or not Path(path).exists():
        return default
    return json.loads(Path(path).read_text(encoding="utf-8"))


def slugify(value: str) -> str:
    import re
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    return value.strip("_") or "other"

def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the download catalog")
    parser.add_argument("--plan-dir", default="download_plan")
    parser.add_argument("--fragments-dir", default="fragments")
    parser.add_argument("--template", default="downloads/template.html")
    parser.add_argument("--out", default="catalog_out")
    args = parser.parse_args()

    meta = load_json(Path(args.plan_dir) / "meta.json", {})
    fragments = []
    for path in sorted(Path(args.fragments_dir).glob("fragment_*.json")):
        fragments.extend(load_json(path, []))
    fragments.sort(key=lambda item: (item.get("group", ""), item.get("subgroup", ""), item.get("title", "")))

    group_map: dict[str, dict] = {}
    for item in fragments:
        group = group_map.setdefault(item["group"], {
            "id": slugify(item["group"]),
            "title": item["group"],
            "description": (meta.get("group_descriptions") or {}).get(item["group"], ""),
            "subgroups": {},
        })
        sub = group["subgroups"].setdefault(item["subgroup"], {
            "title": item["subgroup"],
            "order": item.get("subgroup_order", 0),
            "slices": [],
        })
        sub["slices"].append(item)

    group_order = meta.get("group_order") or list(group_map)
    groups = []
    for group_name in group_order:
        group = group_map.get(group_name)
        if not group:
            continue
        groups.append({
            "id": group["id"],
            "title": group["title"],
            "description": group["description"],
            "subgroups": [
                {"title": sub["title"], "slices": sub["slices"]}
                for sub in sorted(group["subgroups"].values(), key=lambda item: (item["order"], item["title"]))
            ],
        })

    total_size = sum(int(item.get("size_bytes") or 0) for item in fragments)
    payload = {
        "generated_at": meta.get("generated_at"),
        "dataset": meta.get("dataset", "cricket-lake-v3"),
        "source": meta.get("source"),
        "total_matches": meta.get("total_matches"),
        "slice_count": len(fragments),
        "total_size_bytes": total_size,
        "total_size_label": _human(total_size),
        "schema_versions": meta.get("schema_versions", {}),
        "formats": meta.get("formats", {}),
        "levels": meta.get("levels", {}),
        "genders": meta.get("genders", {}),
        "date_range": meta.get("date_range", {}),
        "excluded_quarantined": meta.get("excluded_quarantined"),
        "notes": meta.get("notes", []),
        "groups": groups,
    }

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "downloads.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    shutil.copyfile(args.template, out / "index.html")

    lines = [f"{item['sha256']}  {item['url']}" for item in sorted(fragments, key=lambda x: x["id"])]
    (out / "checksums.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    readme = f"""Cricket Lake Downloads
======================

Dataset        : {payload['dataset']}
Generated at   : {payload['generated_at']}
Published lake : {payload['total_matches']:,} matches
Download slices: {payload['slice_count']}
Archive size   : {payload['total_size_label']}
Schema mix     : {json.dumps(payload['schema_versions'], sort_keys=True)}
Date range     : {payload['date_range'].get('earliest')} to {payload['date_range'].get('latest')}

Open index.html for the catalog. Each zip contains:
  <match_id>.json    ball-by-ball Match JSON v3
  matches_index.csv  filtered index for the slice
  lake_manifest.json slice counts, filters and coverage
  README.txt         slice-specific notes

IMPORTANT
  * Only clean, published matches are included. {payload['excluded_quarantined'] or 0:,} quarantined
    matches are excluded from the public lake.
  * Added windows cover the incremental/recovery feed; the original 16.4k
    backfill predates it and is available through Played/All/Year slices.
  * Women's-only downloads are withheld for v1 until the T2 backfill is run.
  * Verify downloads against checksums.txt.

Source: ESPNcricinfo match pages via cricket-lake-unfiltered.
"""
    (out / "README.txt").write_text(readme, encoding="utf-8")

    print(json.dumps({
        "slices": len(fragments),
        "groups": [g["title"] for g in groups],
        "total_size": _human(total_size),
        "out": str(out),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
