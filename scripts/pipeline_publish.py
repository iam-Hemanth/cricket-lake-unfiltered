#!/usr/bin/env python3
"""
scripts/pipeline_publish.py
Turn the catalog and the built layer into the published lake.

This stage reads the built layer. It takes the built matches the catalog
names, verifies their digests, and writes the published snapshot: the master
archive, the match index, the download slices and the dated calendar.

Nothing here changes a match. The published snapshot is a copy of what the
builder produced, so a published match and a built match are the same bytes.
The previous arrangement overlaid several tarballs in file-name order and
patched the result afterwards; that is what this stage replaces.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src import catalog_db, release_store, shard_store  # noqa: E402
from src.catalog_fields import INDEX_FIELDS, extract_catalog_metadata  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("publish")

MASTER_ARCHIVE = "cricket_lake_unfiltered_master.tar.gz"
MASTER_ZIP = "cricket_lake_unfiltered_master.zip"
PUBLISHED_ASSETS = (
    MASTER_ARCHIVE, MASTER_ZIP, "README.txt", "matches_index.csv", "lake_manifest.json",
)

PUBLISHABLE_SQL = (
    "SELECT match_id, built_archive, built_sha256 FROM matches"
    " WHERE validation_tier IN ('clean', 'warning', 'source_gap',"
    " 'scorecard_only', 'coverage_gap')"
)


def _index_row(match: dict) -> Dict[str, Any]:
    """One row of the published match index, from the match itself."""
    return extract_catalog_metadata(match)[1]


def write_index(
    matches_dir: Path,
    out_path: Path,
    catalog_rows: Optional[Dict[str, Dict[str, Any]]] = None,
) -> int:
    """Write the published match index.

    The catalog row wins where it has a value, because it carries the
    first-published and built-at timestamps no match JSON can know. The match
    JSON fills any field the catalog row does not have, so an older migrated
    row still publishes a complete index.
    """
    catalog_rows = catalog_rows or {}
    rows: List[Dict[str, Any]] = []
    for path in sorted(matches_dir.glob("*.json")):
        if path.name in ("lake_manifest.json", "manifest.json", "failed_matches.json"):
            continue
        try:
            match = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Skipping unreadable match file %s", path.name)
            continue
        match_id = str(match.get("match_id") or path.stem)
        derived = _index_row(match)
        row = dict(catalog_rows.get(match_id) or {})
        for key, value in derived.items():
            if row.get(key) in (None, ""):
                row[key] = value
        row["match_id"] = match_id
        row["filename"] = f"{match_id}.json"
        rows.append(row)
    rows.sort(key=lambda r: (r.get("date") or "9999", r["match_id"]))
    with open(out_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=INDEX_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Wrote the match index with %s rows", len(rows))
    return len(rows)


def write_manifest(matches_dir: Path, out_path: Path, catalog_summary: Dict[str, Any]) -> Dict[str, Any]:
    """Write the published manifest."""
    tiers: Dict[str, int] = {}
    total_balls = total_runs = total_wickets = 0
    count = 0
    earliest = latest = None
    for path in sorted(matches_dir.glob("*.json")):
        if path.name in ("lake_manifest.json", "manifest.json"):
            continue
        try:
            match = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        count += 1
        meta = match.get("meta") or {}
        tier = meta.get("validation_status") or "clean"
        tiers[tier] = tiers.get(tier, 0) + 1
        for date in meta.get("dates") or []:
            if earliest is None or date < earliest:
                earliest = date
            if latest is None or date > latest:
                latest = date
        for innings in match.get("innings") or []:
            for over in innings.get("overs") or []:
                for delivery in over.get("deliveries") or []:
                    total_balls += 1
                    total_runs += delivery.get("runs", {}).get("total", 0)
                    total_wickets += len(delivery.get("wickets") or [])
    manifest = {
        "dataset_name": "cricket-lake-v3",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_matches": count,
        "date_range": {"earliest": earliest, "latest": latest},
        "totals": {"deliveries": total_balls, "runs": total_runs, "wickets": total_wickets},
        "validation_tiers": tiers,
        "coverage": {
            "warning_matches": tiers.get("warning", 0),
            "source_gap_matches": tiers.get("source_gap", 0),
            "scorecard_only_matches": tiers.get("scorecard_only", 0),
            "coverage_gap_matches": tiers.get("coverage_gap", 0),
            "quarantined_matches": 0,
        },
        "catalog": catalog_summary,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def write_readme(out_path: Path, manifest: Dict[str, Any]) -> None:
    """Write the README that ships with the master archive."""
    tiers = ", ".join(
        f"{name} {count:,}" for name, count in sorted(manifest["validation_tiers"].items())
    ) or "clean"
    date_range = manifest.get("date_range") or {}
    out_path.write_text(
        "\n".join([
            "Cricket Lake — published snapshot",
            "=" * 40,
            "",
            f"Matches        : {manifest['total_matches']:,}",
            f"Date range     : {date_range.get('earliest')} to {date_range.get('latest')}",
            f"Deliveries     : {manifest['totals']['deliveries']:,}",
            f"Runs           : {manifest['totals']['runs']:,}",
            f"Wickets        : {manifest['totals']['wickets']:,}",
            f"Validation     : {tiers}",
            "",
            "CONTENTS",
            "  <match_id>.json   Ball-by-ball Match JSON v3",
            "  matches_index.csv One row per match",
            "  lake_manifest.json Counts and coverage",
            "",
            "Every match is the bytes the lake's builder produced. A match whose",
            "validation_status is not clean carries its disclosed caveats in",
            "meta.validation_warnings.",
            "",
            "Source: ESPNcricinfo match pages via cricket-lake-unfiltered.",
            "",
        ]),
        encoding="utf-8",
    )


def write_archives(matches_dir: Path, out_dir: Path) -> None:
    """Write the master tar.gz and zip from the published snapshot."""
    members = [
        path for path in sorted(matches_dir.glob("*.json"))
        if path.name not in ("lake_manifest.json", "manifest.json")
    ]
    extras = [
        out_dir / "README.txt",
        out_dir / "matches_index.csv",
        out_dir / "lake_manifest.json",
    ]
    with tarfile.open(out_dir / MASTER_ARCHIVE, "w:gz") as archive:
        for path in extras:
            if path.exists():
                archive.add(path, arcname=path.name)
        for path in members:
            archive.add(path, arcname=path.name)
    with zipfile.ZipFile(out_dir / MASTER_ZIP, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in extras:
            if path.exists():
                archive.write(path, arcname=path.name)
        for path in members:
            archive.write(path, arcname=path.name)


def materialize_published(
    connection,
    archive_dir: Path,
    dest_dir: Path,
    verify: bool = True,
) -> Dict[str, Any]:
    """Write the published matches, using only what the catalog names.

    A match is published when its tier admits it. Its bytes come from the
    archive the catalog names, and the digest is checked, so a corrupted
    archive cannot be published. Both shards and packs are read: an
    incremental ingest writes a pack until the next compaction folds it in.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    for stale in dest_dir.glob("*.json"):
        stale.unlink()
    publishable = catalog_db.select_publishable(connection)

    targets: Dict[str, List[str]] = {}
    digests: Dict[str, Optional[str]] = {}
    for row in connection.execute(PUBLISHABLE_SQL + " ORDER BY match_id"):
        match_id = str(row["match_id"])
        digests[match_id] = row["built_sha256"]
        if row["built_archive"] is None:
            continue
        targets.setdefault(str(row["built_archive"]), []).append(match_id)

    written: List[str] = []
    missing: List[str] = []
    mismatched: List[str] = []

    for archive_name, match_ids in sorted(targets.items()):
        archive_path = archive_dir / archive_name
        if not archive_path.exists():
            missing.extend(match_ids)
            continue
        members = shard_store.read_pack(archive_path)
        for match_id in match_ids:
            member_name = f"{match_id}.json"
            payload = members.get(member_name)
            if payload is None:
                missing.append(match_id)
                continue
            expected = digests.get(match_id)
            if verify and expected and shard_store.member_digest(payload) != expected:
                mismatched.append(match_id)
                continue
            (dest_dir / member_name).write_bytes(payload)
            written.append(match_id)

    # A publishable row without a built archive is missing too, not silently
    # absent: the published lake must lose no match.
    missing.extend(
        str(row["match_id"]) for row in connection.execute(
            PUBLISHABLE_SQL + " AND built_archive IS NULL ORDER BY match_id"
        )
    )

    logger.info(
        "Published %s matches (%s missing, %s mismatched)", len(written), len(missing), len(mismatched)
    )
    return {
        "published": written,
        "missing": missing,
        "mismatched": mismatched,
        "publishable_total": len(publishable),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workdir", default="publish_work")
    parser.add_argument("--repo", default=release_store.DEFAULT_REPO)
    parser.add_argument("--catalog", default=None)
    parser.add_argument("--out", default="published")
    parser.add_argument("--upload", action="store_true",
                        help="Upload the published snapshot to the release")
    parser.add_argument("--skip-download", action="store_true",
                        help="Assume the built archives are already in the workdir")
    args = parser.parse_args()

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    catalog_path = Path(args.catalog) if args.catalog else workdir / "lake_catalog.sqlite.gz"
    if catalog_path.exists():
        connection = catalog_db.load_catalog_gz(catalog_path)
    else:
        release_store.download_asset(
            args.repo, release_store.RELEASE_CATALOG, "lake_catalog.sqlite.gz", workdir
        )
        connection = catalog_db.load_catalog_gz(catalog_path)

    archive_dir = workdir / "archives"
    archive_dir.mkdir(parents=True, exist_ok=True)
    if not args.skip_download:
        for row in connection.execute(
            "SELECT DISTINCT built_archive AS archive FROM matches"
            " WHERE validation_tier IN ('clean', 'warning', 'source_gap',"
            " 'scorecard_only', 'coverage_gap') AND built_archive IS NOT NULL"
        ):
            name = str(row["archive"])
            target = archive_dir / name
            if not target.exists():
                try:
                    release_store.download_asset(
                        args.repo, release_store.RELEASE_BUILT, name, archive_dir
                    )
                except Exception as exc:
                    logger.warning("Could not download %s: %s", name, exc)

    matches_dir = out_dir / "matches"
    outcome = materialize_published(connection, archive_dir, matches_dir)
    if outcome["missing"] or outcome["mismatched"]:
        print(json.dumps({
            "published": len(outcome["published"]),
            "missing": outcome["missing"][:50],
            "mismatched": outcome["mismatched"][:50],
        }, indent=2))
        raise SystemExit(
            "Refusing to publish: "
            f"{len(outcome['missing'])} missing and {len(outcome['mismatched'])} "
            "mismatched built matches. The catalog names bytes the archives do not hold."
        )

    summary = catalog_db.summary(connection)
    published_ids = set(outcome["published"])
    catalog_rows = {
        str(row["match_id"]): row
        for row in catalog_db.export_matches_index_rows(connection)
        if str(row["match_id"]) in published_ids
    }
    index_rows = write_index(matches_dir, out_dir / "matches_index.csv", catalog_rows)
    manifest = write_manifest(matches_dir, out_dir / "lake_manifest.json", summary)
    write_readme(out_dir / "README.txt", manifest)
    write_archives(matches_dir, out_dir)

    if args.upload:
        for name in PUBLISHED_ASSETS:
            release_store.upload_asset(
                args.repo, release_store.RELEASE_PUBLISHED, out_dir / name
            )

    logger.info(
        "Published snapshot: %s matches, %s index rows",
        len(outcome["published"]), index_rows,
    )
    print(json.dumps({
        "published": len(outcome["published"]),
        "missing": len(outcome["missing"]),
        "mismatched": len(outcome["mismatched"]),
        "index_rows": index_rows,
        "uploaded": bool(args.upload),
        "output": str(out_dir),
    }, indent=2))
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
