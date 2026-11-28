#!/usr/bin/env python3
"""
scripts/pipeline_reparse.py
Rebuild matches from the stored raw payloads, without touching the network.

The raw layer holds the complete evidence of every fetch, so a parser change
can be applied to the whole lake offline. This script finds the matches whose
stored build used a different parser version, rebuilds them through the
shared builder, and publishes the result.

There is no network access here at all. A parser bug is fixed by changing the
code and running this, never by fetching the matches again.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src import catalog_db, release_store, shard_store  # noqa: E402
from src.builder import PARSER_VERSION, SCHEMA_VERSION, References, build_match  # noqa: E402
from src.catalog_fields import extract_catalog_metadata  # noqa: E402
from src.commentary import load_raw_match_payloads  # noqa: E402
from src.completeness import failure_maturity  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("reparse")


def load_references(repo_root: Path = REPO_ROOT) -> References:
    """Load the reference data the constructor needs."""
    import csv

    profiles_path = repo_root / "data" / "player_profiles_cache.json"
    player_profiles = {}
    if profiles_path.exists():
        player_profiles = json.loads(profiles_path.read_text(encoding="utf-8"))

    venue_path = repo_root / "data" / "venue_to_country.json"
    venue_map = {}
    if venue_path.exists():
        venue_map = json.loads(venue_path.read_text(encoding="utf-8"))

    people_path = repo_root / "data" / "people.csv"
    people_registry = {}
    if people_path.exists():
        with open(people_path, "r", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                cid = row.get("identifier")
                cricinfo_id = row.get("key_cricinfo")
                if cricinfo_id and cid:
                    people_registry[str(cricinfo_id)] = cid

    return References(
        player_profiles=player_profiles,
        people_registry=people_registry,
        venue_map=venue_map,
    )


def _stamp(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def select_targets(
    connection,
    target_parser_version: str,
    match_ids: Optional[str] = None,
    force: bool = False,
    limit: Optional[int] = None,
) -> List[str]:
    """The matches this run will rebuild."""
    if match_ids:
        ids = [m.strip() for m in match_ids.split(",") if m.strip()]
        return ids[:limit] if limit else ids
    if force:
        ids = catalog_db.select_publishable(connection)
        return ids[:limit] if limit else ids
    return catalog_db.select_stale_parser(connection, target_parser_version, limit=limit)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workdir", default="reparse_work")
    parser.add_argument("--repo", default=release_store.DEFAULT_REPO)
    parser.add_argument("--catalog", default=None,
                        help="Path to the catalog (downloaded when omitted)")
    parser.add_argument("--match-ids", default=None,
                        help="Comma-separated match ids to rebuild")
    parser.add_argument("--force", action="store_true",
                        help="Rebuild every published match, not only the stale ones")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--chunk-index", type=int, default=None)
    parser.add_argument("--chunk-size", type=int, default=None)
    parser.add_argument("--target-parser-version", default=PARSER_VERSION)
    parser.add_argument("--upload", action="store_true",
                        help="Upload the rebuilt matches and the catalog")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be rebuilt and stop")
    args = parser.parse_args()

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    catalog_path = Path(args.catalog) if args.catalog else workdir / "lake_catalog.sqlite.gz"
    catalog_updated_at = None
    if catalog_path.exists():
        connection = catalog_db.load_catalog_gz(catalog_path)
        if args.catalog is None and args.upload and not args.dry_run:
            asset = release_store.find_asset(
                args.repo, release_store.RELEASE_CATALOG, "lake_catalog.sqlite.gz"
            )
            catalog_updated_at = asset.get("updated_at") if asset else None
    else:
        logger.info("Downloading the catalog from %s", release_store.RELEASE_CATALOG)
        asset = release_store.find_asset(
            args.repo, release_store.RELEASE_CATALOG, "lake_catalog.sqlite.gz"
        )
        catalog_updated_at = asset.get("updated_at") if asset else None
        release_store.download_asset(
            args.repo, release_store.RELEASE_CATALOG,
            "lake_catalog.sqlite.gz", workdir,
        )
        connection = catalog_db.load_catalog_gz(catalog_path)

    targets = select_targets(
        connection, args.target_parser_version,
        args.match_ids, args.force, args.limit,
    )
    if args.chunk_size:
        lo = (args.chunk_index or 0) * args.chunk_size
        targets = targets[lo:lo + args.chunk_size]

    logger.info("Rebuilding %s matches with parser %s", len(targets), args.target_parser_version)
    if args.dry_run:
        print(json.dumps({
            "would_rebuild": len(targets),
            "target_parser_version": args.target_parser_version,
            "sample": targets[:20],
        }, indent=2))
        connection.close()
        return 0

    # The raw payloads come from the release the catalog names. Only the
    # archives the targets actually live in are downloaded.
    archive_dir = workdir / "archives"
    archive_dir.mkdir(parents=True, exist_ok=True)
    targets_by_archive = shard_store.catalog_targets(
        connection, targets, shard_store.LAYER_RAW
    )
    for archive in sorted(targets_by_archive):
        target = archive_dir / archive
        if not target.exists():
            try:
                release_store.download_asset(
                    args.repo, release_store.RELEASE_RAW, archive, archive_dir
                )
            except Exception as exc:
                logger.warning("Could not download %s: %s", archive, exc)

    materialized = shard_store.materialize_from_catalog(
        connection, targets, archive_dir, workdir / "raw", shard_store.LAYER_RAW
    )
    logger.info(
        "Materialized %s raw bundles (%s missing, %s mismatched)",
        len(materialized["extracted"]), len(materialized["missing"]),
        len(materialized["mismatched"]),
    )

    references = load_references()
    built_root = workdir / "built"
    built_root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    stamp = _stamp()
    built_pack = shard_store.pack_name(shard_store.LAYER_BUILT, stamp)

    rebuilt: Dict[str, bytes] = {}
    failures: List[Dict[str, str]] = []
    updated_rows = 0
    for index, match_id in enumerate(materialized["extracted"], 1):
        existing = catalog_db.get_match(connection, match_id)
        if existing is None:
            failures.append({"match_id": match_id, "error": "Not in catalog"})
            continue
        attempts = int(existing["fetch_attempts"] or 0)
        match_dir = workdir / "raw" / match_id
        details, chunks, error = load_raw_match_payloads(match_dir)
        if error or not details:
            failures.append({"match_id": match_id, "error": f"Payload error: {error}"})
            continue
        result = build_match(
            match_id, details, chunks, references, refresh_attempts=attempts,
        )

        common = {
            "raw_archive": existing["raw_archive"],
            "raw_sha256": existing["raw_sha256"],
            "raw_fetched_at": existing["raw_fetched_at"],
            "parser_version": PARSER_VERSION,
            "schema_version": SCHEMA_VERSION,
            "force_rebuild": args.force,
            "now_utc": now,
        }
        if not result.ok:
            maturity, next_refresh = failure_maturity(details, attempts, now)
            catalog_db.upsert_fetch_and_build(
                connection, match_id,
                validation_tier="failed",
                completeness_gaps=[],
                maturity_state=maturity,
                next_refresh_at=next_refresh,
                failure_kind=result.failure_kind or "construction_error",
                error=result.error,
                **common,
            )
            failures.append({"match_id": match_id, "error": result.error or "Build failed"})
            updated_rows += 1
            continue

        payload = json.dumps(result.match, indent=2, ensure_ascii=False).encode("utf-8")
        rebuilt[match_id] = payload
        identity, index_meta = extract_catalog_metadata(result.match)
        catalog_db.upsert_fetch_and_build(
            connection, match_id,
            built_archive=built_pack,
            built_sha256=shard_store.member_digest(payload),
            validation_tier=result.tier,
            completeness_gaps=result.completeness.gaps if result.completeness else [],
            maturity_state=result.completeness.maturity_state if result.completeness else "settled",
            next_refresh_at=result.completeness.next_refresh_at if result.completeness else None,
            failure_kind=None,
            error=None,
            identity=identity,
            index_meta=index_meta,
            **common,
        )
        updated_rows += 1

    if rebuilt:
        _, built_digests = shard_store.write_built_pack(workdir / built_pack, rebuilt)
        for match_id, digest in built_digests.items():
            catalog_db.upsert_match(
                connection, match_id,
                built_archive=built_pack,
                built_sha256=digest,
            )
    connection.commit()

    summary = {
        "targeted": len(targets),
        "rebuilt": len(rebuilt),
        "failed": len(failures),
        "updated_rows": updated_rows,
        "has_changes": bool(rebuilt),
        "built_pack": built_pack if rebuilt else None,
    }
    catalog_db.record_run(
        connection, f"reparse-{stamp}", "reparse",
        finished_at=now.isoformat(), summary=summary,
    )
    catalog_db.save_catalog_gz(connection, workdir / "lake_catalog.sqlite.gz")

    if args.upload:
        if rebuilt:
            release_store.upload_asset(args.repo, release_store.RELEASE_BUILT, workdir / built_pack)
        if updated_rows:
            release_store.upload_asset(
                args.repo, release_store.RELEASE_CATALOG,
                workdir / "lake_catalog.sqlite.gz",
                expected_updated_at=catalog_updated_at,
            )

    logger.info("Reparse complete: %s", json.dumps(summary))
    print(json.dumps(summary, indent=2))
    if failures:
        (workdir / "reparse_failures.json").write_text(
            json.dumps(failures, indent=2), encoding="utf-8"
        )
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
