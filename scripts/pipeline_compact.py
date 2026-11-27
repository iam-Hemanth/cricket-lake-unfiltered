#!/usr/bin/env python3
"""
scripts/pipeline_compact.py
Fold the incremental packs into the shards.

Every ingest writes a pack, because a pack is cheap to produce and cheap to
upload. Over time that leaves many small archives. Compaction folds them into
the fixed set of shards and repoints the catalog, and only then may the packs
be removed.

The compaction keeps the copy the catalog names. A pack that still holds an
older copy of a match cannot overwrite the authoritative one, and a member
whose digest does not match the catalog is left alone rather than promoted.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src import catalog_db, release_store, shard_store  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("compact")

#: The number of uncompacted packs that triggers a compaction.
PACK_THRESHOLD = 15


def pending_packs(archive_dir: Path, layer: str) -> List[str]:
    """The packs of a layer that have not been folded into the shards."""
    prefix = f"{layer}_pack_"
    return sorted(
        path.name for path in archive_dir.glob(f"{prefix}*.tar.gz")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workdir", default="compact_work")
    parser.add_argument("--repo", default=release_store.DEFAULT_REPO)
    parser.add_argument("--catalog", default=None)
    parser.add_argument("--layer", default="both", choices=["raw", "built", "both"])
    parser.add_argument("--threshold", type=int, default=PACK_THRESHOLD,
                        help="Compact when this many packs are pending")
    parser.add_argument("--force", action="store_true",
                        help="Compact even when the threshold is not reached")
    parser.add_argument("--prune", action="store_true",
                        help="Delete the compacted packs from the release")
    parser.add_argument("--upload", action="store_true",
                        help="Upload the shards and the catalog")
    args = parser.parse_args()

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    archive_dir = workdir / "archives"
    archive_dir.mkdir(parents=True, exist_ok=True)

    catalog_path = Path(args.catalog) if args.catalog else workdir / "lake_catalog.sqlite.gz"
    if catalog_path.exists():
        connection = catalog_db.load_catalog_gz(catalog_path)
    else:
        release_store.download_asset(
            args.repo, release_store.RELEASE_CATALOG, "lake_catalog.sqlite.gz", workdir
        )
        connection = catalog_db.load_catalog_gz(catalog_path)

    layers = ["raw", "built"] if args.layer == "both" else [args.layer]
    report: Dict[str, Any] = {}

    for layer in layers:
        release = release_store.RELEASE_RAW if layer == "raw" else release_store.RELEASE_BUILT
        packs = pending_packs(archive_dir, layer)
        if not packs and not args.force:
            report[layer] = {"packs": 0, "skipped": "no pending packs"}
            continue
        if len(packs) < args.threshold and not args.force:
            report[layer] = {"packs": len(packs), "skipped": "below the threshold"}
            continue

        # The shards the catalog names are downloaded first, because the
        # compaction reads the members that are already in them.
        for row in connection.execute(
            f"SELECT DISTINCT {layer}_archive AS archive FROM matches"
            f" WHERE {layer}_archive IS NOT NULL AND {layer}_archive LIKE '{layer}_shard_%'"
        ):
            name = str(row["archive"])
            target = archive_dir / name
            if not target.exists():
                try:
                    release_store.download_asset(args.repo, release, name, archive_dir)
                except Exception as exc:
                    logger.warning("Could not download %s: %s", name, exc)

        for name in packs:
            target = archive_dir / name
            if not target.exists():
                try:
                    release_store.download_asset(args.repo, release, name, archive_dir)
                except Exception as exc:
                    logger.warning("Could not download %s: %s", name, exc)

        outcome = shard_store.compact_shards(
            connection, layer, archive_dir, archive_dir, packs
        )
        connection.commit()

        if args.upload:
            for name in outcome["shards_written"]:
                release_store.upload_asset(args.repo, release, archive_dir / name)
        if args.prune:
            for name in packs:
                release_store.delete_asset(args.repo, release, name)

        report[layer] = {
            "packs": len(packs),
            "shards_written": outcome["shards_written"],
            "rows_repointed": outcome["rows_repointed"],
        }

    catalog_db.save_catalog_gz(connection, workdir / "lake_catalog.sqlite.gz")
    if args.upload:
        release_store.upload_asset(
            args.repo, release_store.RELEASE_CATALOG, workdir / "lake_catalog.sqlite.gz"
        )

    print(json.dumps(report, indent=2))
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
