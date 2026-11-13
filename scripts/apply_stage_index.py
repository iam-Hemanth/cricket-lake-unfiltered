#!/usr/bin/env python3
"""Apply an ESPN stage index to normalized match JSON files.

Only non-null index values are copied. Existing stage fields are preserved when
the index has no value, so newer delta files are never downgraded.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.stage import apply_stage_index_to_dir, load_stage_index


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matches-dir", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--summary", type=Path, default=None)
    args = parser.parse_args()

    index = load_stage_index(args.index)
    stats = apply_stage_index_to_dir(args.matches_dir, index=index, dry_run=args.dry_run)
    print(json.dumps({"index_entries": len(index), **stats, "dry_run": args.dry_run}, indent=2))
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(stats, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
