#!/usr/bin/env python3
"""
scripts/inspect_catalog.py
Read-only diagnostic for the lake catalog.

This is the command to run *before* doing anything to the lake. It answers
"what does the catalog know about this match, and why does it look like
this?" in one command, so a repair never starts from a guess.

Examples:
    python scripts/inspect_catalog.py --summary
    python scripts/inspect_catalog.py --match-ids 1529281,1561358
    python scripts/inspect_catalog.py --stale-parser
    python scripts/inspect_catalog.py --provisional
    python scripts/inspect_catalog.py --quarantined
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import catalog_db


def _open(args: argparse.Namespace):
    """Open the catalog, from a local file or a downloaded release asset."""
    if args.catalog and Path(args.catalog).exists():
        path = Path(args.catalog)
        if path.suffix == ".gz":
            return catalog_db.load_catalog_gz(path)
        return catalog_db.open_catalog(path)
    raise SystemExit(
        f"Catalog not found: {args.catalog}\n"
        "Download it with:\n"
        "  gh release download lake-catalog --repo iam-Hemanth/cricket-lake-unfiltered"
    )


def _print_match(connection, match_id: str) -> None:
    row = catalog_db.get_match(connection, match_id)
    if row is None:
        print(f"{match_id}: NOT IN CATALOG")
        return
    gaps = []
    if row["completeness_gaps_json"]:
        try:
            gaps = json.loads(row["completeness_gaps_json"])
        except json.JSONDecodeError:
            gaps = []
    payload = {
        "match_id": row["match_id"],
        "match_date": row["match_date"],
        "validation_tier": row["validation_tier"],
        "maturity_state": row["maturity_state"],
        "raw_available": bool(row["raw_available"]),
        "raw_archive": row["raw_archive"],
        "raw_sha256": row["raw_sha256"],
        "raw_fetched_at": row["raw_fetched_at"],
        "parser_version": row["parser_version"],
        "schema_version": row["schema_version"],
        "built_archive": row["built_archive"],
        "built_sha256": row["built_sha256"],
        "fetch_attempts": row["fetch_attempts"],
        "next_refresh_at": row["next_refresh_at"],
        "last_failure_kind": row["last_failure_kind"],
        "last_error": row["last_error"],
        "completeness_gaps": gaps,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--catalog", default="lake_catalog.sqlite.gz",
                        help="Path to the catalog (a .gz file or a SQLite file)")
    parser.add_argument("--summary", action="store_true", help="Counts by tier and maturity state")
    parser.add_argument("--match-ids", default=None,
                        help="Comma-separated match ids to inspect")
    parser.add_argument("--stale-parser", action="store_true",
                        help="Match ids whose build used a different parser version")
    parser.add_argument("--provisional", action="store_true",
                        help="Match ids still waiting for a completeness refresh")
    parser.add_argument("--quarantined", action="store_true",
                        help="Match ids in the failed tier")
    parser.add_argument("--legacy-unbacked", action="store_true",
                        help="Published match ids with no raw payload behind them")
    parser.add_argument("--target-parser-version", default=None,
                        help="The parser version to compare against for --stale-parser")
    args = parser.parse_args()

    connection = _open(args)
    printed = False

    if args.summary:
        print(json.dumps(catalog_db.summary(connection), indent=2))
        printed = True

    if args.match_ids:
        for match_id in [m.strip() for m in args.match_ids.split(",") if m.strip()]:
            _print_match(connection, match_id)
            printed = True

    if args.stale_parser:
        from src.builder import PARSER_VERSION
        target = args.target_parser_version or PARSER_VERSION
        ids = catalog_db.select_stale_parser(connection, target)
        print(json.dumps({"target_parser_version": target, "count": len(ids), "match_ids": ids},
                         indent=2))
        printed = True

    if args.provisional:
        ids = catalog_db.select_due_refreshes(connection, limit=10000)
        print(json.dumps({"count": len(ids), "match_ids": ids}, indent=2))
        printed = True

    if args.quarantined:
        ids = catalog_db.select_by_tier(connection, "failed")
        print(json.dumps({"count": len(ids), "match_ids": ids}, indent=2))
        printed = True

    if args.legacy_unbacked:
        ids = catalog_db.select_legacy_unbacked(connection)
        print(json.dumps({"count": len(ids), "match_ids": ids}, indent=2))
        printed = True

    if not printed:
        parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
