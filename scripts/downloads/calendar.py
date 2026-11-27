#!/usr/bin/env python3
"""Build the calendar index and per-match gzip downloads.

The calendar page is a static view over the published lake: it reads
`calendar.json` for the match list and downloads a single match by fetching
`m/<match_id>.json.gz` from the same origin (no CORS, no third-party storage).

Sizes: ~18k matches at ~13 KB gzipped each stay around 240 MB, comfortably
inside the 1 GB GitHub Pages limit. The script refuses to go past 700 MB so a
future growth spurt fails the build instead of the deploy.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
import tarfile
from collections import Counter, defaultdict
from pathlib import Path

MAX_MATCH_BYTES = 700 * 1024 * 1024

# The source keeps fine-grained format codes; the calendar groups them the way
# the catalog does so the chips read "First-class" / "Other", not OtherODM.
FORMAT_FAMILY = {
    "Test": "Test", "ODI": "ODI", "T20I": "T20I", "T20": "T20",
    "FC": "First-class", "First-Class": "First-class",
    "ListA": "List A", "List A": "List A",
    "OtherODM": "Other", "OtherMDM": "Other", "OtherT20": "Other", "Other": "Other",
}


def load_json(path: Path, default=None):
    if not path or not Path(path).exists():
        return default
    return json.loads(Path(path).read_text(encoding="utf-8"))


def smallest_slices(slices: list[dict], fragments: dict[str, dict]) -> dict[str, str]:
    """Map match id -> smallest slice id (by zip size) that contains it."""
    best: dict[str, tuple[int, str]] = {}
    for item in slices:
        size = int((fragments.get(item.get("id"), {}) or {}).get("size_bytes") or 0)
        for mid in item.get("ids") or []:
            current = best.get(mid)
            if current is None or size < current[0]:
                best[mid] = (size, item["id"])
    return {mid: sid for mid, (_, sid) in best.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build calendar.json and m/<id>.json.gz")
    parser.add_argument("--plan-dir", required=True, type=Path)
    parser.add_argument("--master-tar", required=True, type=Path)
    parser.add_argument("--deltas-dir", type=Path, default=None)
    parser.add_argument("--fragments-dir", type=Path, default=None)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=0, help="Only write the first N match files (tests)")
    args = parser.parse_args()

    records = list(csv.DictReader((args.plan_dir / "records.csv").open(encoding="utf-8")))
    meta = load_json(args.plan_dir / "meta.json", {})
    slices = load_json(args.plan_dir / "slices.json", [])
    fragments: dict[str, dict] = {}
    if args.fragments_dir and args.fragments_dir.is_dir():
        for path in sorted(args.fragments_dir.glob("fragment_*.json")):
            for item in load_json(path, []):
                fragments[item.get("id")] = item
    slice_for = smallest_slices(slices, fragments)

    matches = []
    for row in records:
        date = (row.get("date") or "").strip()
        if date in ("Unknown", "unknown"):
            date = ""
        matches.append({
            "i": row.get("match_id") or "",
            "d": date,
            "a": row.get("team1") or "",
            "b": row.get("team2") or "",
            "c": row.get("competition") or "",
            "f": FORMAT_FAMILY.get(row.get("format") or "", "Other"),
            "v": row.get("venue") or "",
            "y": row.get("venue_country") or "",
            "r": row.get("result") or "",
            "s": row.get("validation_status") or "clean",
            "g": row.get("gender") or "unknown",
            "sl": slice_for.get(row.get("match_id") or "", ""),
        })

    wanted = {m["i"] for m in matches if m["i"]}
    out_m = args.out / "m"
    out_m.mkdir(parents=True, exist_ok=True)
    written: set[str] = set()
    total_bytes = 0

    def ingest(path: Path) -> None:
        nonlocal total_bytes
        with tarfile.open(path, "r|gz") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                name = Path(member.name).name
                if not name.endswith(".json"):
                    continue
                mid = Path(name).stem
                if not mid.isdigit() or mid not in wanted or mid in written:
                    continue
                payload = tar.extractfile(member).read()
                blob = gzip.compress(payload, compresslevel=6, mtime=0)
                (out_m / f"{mid}.json.gz").write_bytes(blob)
                written.add(mid)
                total_bytes += len(blob)
                if args.limit and len(written) >= args.limit:
                    return
                if total_bytes > MAX_MATCH_BYTES:
                    raise SystemExit(
                        f"per-match files exceed {MAX_MATCH_BYTES // (1024 * 1024)} MB; "
                        "GitHub Pages allows 1 GB, so split the store before publishing"
                    )

    # Newest source wins: deltas are append-only windows, so ingest them from
    # newest to oldest and let the master fill the remaining ids. Ingesting the
    # master first kept a stale copy for matches a delta had republished.
    if args.deltas_dir and args.deltas_dir.is_dir():
        for path in sorted(args.deltas_dir.glob("lake_delta_*.tar.gz"), reverse=True):
            if args.limit and len(written) >= args.limit:
                break
            ingest(path)
    ingest(args.master_tar)

    for m in matches:
        m["x"] = 1 if m["i"] in written else 0

    statuses = Counter(m["s"] for m in matches)
    formats = Counter(m["f"] for m in matches)
    years = Counter(m["d"][:4] for m in matches if len(m["d"]) >= 4 and m["d"][:4].isdigit())
    known = [m["d"] for m in matches if m["d"]]
    payload = {
        "generated_at": meta.get("generated_at"),
        "total": len(matches),
        "date_range": {
            "earliest": min(known) if known else None,
            "latest": max(known) if known else None,
        },
        "statuses": dict(statuses),
        "formats": dict(formats),
        "years": [{"y": year, "n": count} for year, count in sorted(years.items(), reverse=True)],
        "missing_json": len(matches) - len(written),
        "matches": matches,
    }
    (args.out / "calendar.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )

    print(json.dumps({
        "matches": len(matches),
        "per_match_files": len(written),
        "missing_json": len(matches) - len(written),
        "per_match_mb": round(total_bytes / (1024 * 1024), 1),
        "calendar_mb": round((args.out / "calendar.json").stat().st_size / (1024 * 1024), 2),
        "years": len(years),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
