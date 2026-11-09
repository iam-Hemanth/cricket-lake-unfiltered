#!/usr/bin/env python3
"""Admit quarantined matches that pass the current warn-tier validator.

The validator now treats two conditions as warnings instead of failures:
  * a per-ball commentary running total that disagrees while the innings'
    final total reconciles exactly with the scorecard;
  * a missing non-striker id (the field stays null and is already counted in
    meta.missing_non_striker_deliveries).

A match is admitted only when validate_match_data() returns no errors. The
original recorded errors are preserved in meta.validation_warnings, so the
disclosure survives publication.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.validator import validate_match_data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--summary", type=Path, default=None)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    admitted = []
    rejected = 0
    skipped = 0

    for path in sorted(args.input_dir.glob("*.json")):
        if path.name in {"lake_manifest.json", "manifest.json", "meta.json", "failed_matches.json"}:
            continue
        try:
            match = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            rejected += 1
            continue
        meta = match.get("meta") or {}
        if meta.get("validation_status") != "failed":
            skipped += 1
            continue
        if validate_match_data(match):
            rejected += 1
            continue

        match_id = str(match.get("match_id") or meta.get("match_id") or path.stem)
        meta["validation_status"] = "warning"
        meta["validation_warnings"] = [str(error) for error in meta.get("validation_errors") or []]
        meta["validation_errors"] = []
        (args.out_dir / f"{match_id}.json").write_text(
            json.dumps(match, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        admitted.append(match_id)

    summary = {
        "admitted": len(admitted),
        "still_failed": rejected,
        "skipped_non_failed": skipped,
        "admitted_ids": admitted,
    }
    if args.summary:
        args.summary.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items() if key != "admitted_ids"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
