#!/usr/bin/env python3
"""Build a stage index from stored ESPN raw scorecards.

Reads ``scorecard.json.gz`` payloads from extracted raw directories or from the
release ``raw_batch_*.tar.gz`` archives and records the derived
``match_stage`` / ``match_number`` / ``match_group`` / ``match_title`` per match
id. No Cricsheet input is used.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import logging
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.stage import derive_stage, unwrap_match_payload

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("stage_index")


def _iter_dir(raw_dir: Path):
    for path in sorted(raw_dir.rglob("scorecard.json.gz")):
        mid = path.parent.name
        if not mid.isdigit():
            continue
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Skipping %s: %s", path, exc)
            continue
        yield mid, unwrap_match_payload(payload)


def _iter_tar(path: Path):
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                if not member.isfile() or not member.name.endswith("scorecard.json.gz"):
                    continue
                mid = Path(member.name).parent.name
                if not mid.isdigit():
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                try:
                    payload = json.loads(gzip.decompress(handle.read()).decode("utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    logger.warning("Skipping %s:%s: %s", path, member.name, exc)
                    continue
                yield mid, unwrap_match_payload(payload)
    except (OSError, tarfile.TarError) as exc:
        logger.error("Cannot read %s: %s", path, exc)


def _load_ids(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".csv":
        reader = csv.DictReader(text.splitlines())
        field = "match_id" if "match_id" in (reader.fieldnames or []) else None
        if field:
            return [str(row[field]).strip() for row in reader if str(row.get(field) or "").strip()]
        return [line.split(",", 1)[0].strip() for line in text.splitlines()[1:] if line.strip()]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return [line.strip() for line in text.replace(",", "\n").splitlines() if line.strip()]
    if isinstance(payload, dict):
        return [str(key) for key in payload]
    if isinstance(payload, list):
        return [str(item["match_id"]) if isinstance(item, dict) else str(item) for item in payload]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", action="append", default=[], help="Directory containing <id>/scorecard.json.gz (repeatable)")
    parser.add_argument("--tar", action="append", default=[], help="raw_batch_*.tar.gz archive (repeatable)")
    parser.add_argument("--ids-file", type=Path, default=None, help="Published match ids (CSV, JSON or text) for gap reporting")
    parser.add_argument("--output", type=Path, required=True, help="stage_index.json or stage_index.json.gz")
    parser.add_argument("--summary", type=Path, default=None, help="Optional JSON summary")
    args = parser.parse_args()

    if not args.raw_dir and not args.tar:
        parser.error("at least one --raw-dir or --tar is required")

    index: dict[str, dict] = {}
    conflicts = 0
    scorecards = 0

    sources = [(Path(path), "dir") for path in args.raw_dir] + [(Path(path), "tar") for path in args.tar]
    for path, kind in sources:
        logger.info("Reading %s %s", kind, path)
        records = _iter_dir(path) if kind == "dir" else _iter_tar(path)
        for mid, payload in records:
            scorecards += 1
            match = (payload or {}).get("match") if isinstance(payload, dict) else None
            fields = derive_stage(match)
            previous = index.get(mid)
            if previous is not None and previous != fields:
                conflicts += 1
                logger.warning("Stage conflict for %s: %s != %s", mid, previous, fields)
                continue
            index[mid] = fields

    ordered = {
        key: index[key]
        for key in sorted(index, key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value))
    }
    counts = {
        "scorecards": scorecards,
        "indexed": len(ordered),
        "with_stage": sum(1 for entry in ordered.values() if entry.get("match_stage")),
        "with_number": sum(1 for entry in ordered.values() if entry.get("match_number") is not None),
        "with_group": sum(1 for entry in ordered.values() if entry.get("match_group")),
        "with_title": sum(1 for entry in ordered.values() if entry.get("match_title")),
        "conflicts": conflicts,
    }
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "espn_raw_scorecards",
        "counts": counts,
        "matches": ordered,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.suffix == ".gz":
        with gzip.open(args.output, "wt", encoding="utf-8", compresslevel=9) as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    else:
        args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Wrote %s: %s", args.output, counts)

    summary = dict(counts)
    if args.ids_file:
        ids = _load_ids(args.ids_file)
        missing = [mid for mid in ids if mid not in ordered]
        summary["published_ids"] = len(ids)
        summary["missing_count"] = len(missing)
        summary["missing_ids"] = missing[:5000]
        logger.info("Published ids %s; missing from index %s", len(ids), len(missing))
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
