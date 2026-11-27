#!/usr/bin/env python3
"""
scripts/migrate_to_catalog.py
Seed the catalog, the raw layer and the built layer from the existing lake.

This is the one-time migration. It takes everything the lake has already
collected and puts it behind the new architecture, so that from here on the
catalog is the authority and the raw payloads are the evidence.

Three modes, run in sequence:

``--worker --batch <asset>``
    Take one raw batch asset, extract it, rebuild every match in it, and
    write the raw members and the built matches to a staging directory.
    This is the part that runs in parallel: one worker per asset.

``--merge``
    Take the staged output of every worker, group it by shard, and write
    ``raw_shard_00..63.tar.gz`` and ``built_shard_00..63.tar.gz`` together
    with the catalog. This runs once, as the single writer.

``--verify``
    Compare the migrated lake against the published master and report any
    difference in the match count, the tiers or the stage fields.

Nothing is fetched from ESPN. The migration reads what was already
collected.
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src import catalog_db, release_store, shard_store  # noqa: E402
from src.builder import PARSER_VERSION, References, build_match  # noqa: E402
from src.commentary import load_raw_match_payloads  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("migrate")

#: The releases the raw payloads live in. The second one is the external
#: repository the lake used to depend on at runtime.
SOURCE_RELEASES = [
    (release_store.DEFAULT_REPO, "data-unfiltered-batches"),
    ("iam-Hemanth/cricket-extractor", "data-v2-batches"),
]

#: The released lake the migration is compared against.
MASTER_RELEASE = "data-unfiltered-master"


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


def raw_batch_assets(repo: str, release: str) -> List[str]:
    """The raw batch assets of a release, in a stable order."""
    names = [
        str(asset["name"]) for asset in release_store.list_assets(repo, release)
        if str(asset.get("name", "")).startswith("raw_batch")
    ]
    return sorted(names)


def all_source_assets(repo: str = release_store.DEFAULT_REPO) -> List[Tuple[str, str, str]]:
    """Every raw batch asset, as (repository, release, asset name)."""
    assets: List[Tuple[str, str, str]] = []
    for source_repo, release in SOURCE_RELEASES:
        try:
            names = raw_batch_assets(source_repo, release)
        except Exception as exc:
            logger.warning("Could not list %s in %s: %s", release, source_repo, exc)
            continue
        logger.info("%s in %s holds %s raw batches", release, source_repo, len(names))
        for name in names:
            assets.append((source_repo, release, name))
    return assets


def _fetched_at(match_dir: Path) -> Optional[str]:
    """When this bundle was fetched, as recorded in its meta file."""
    meta_path = match_dir / "meta.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(meta, dict):
        return None
    times = [
        str(record.get("fetched_at"))
        for record in meta.values()
        if isinstance(record, dict) and record.get("fetched_at")
    ]
    return max(times) if times else None


def run_worker(args: argparse.Namespace) -> int:
    """Take one raw batch and produce its raw members and built matches."""
    workdir = Path(args.workdir)
    stage = workdir / "stage"
    stage.mkdir(parents=True, exist_ok=True)

    repo = args.source_repo
    release = args.source_release
    asset = args.batch

    download_dir = workdir / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    archive = download_dir / asset
    if not archive.exists():
        logger.info("Downloading %s from %s/%s", asset, repo, release)
        release_store.download_asset(repo, release, asset, download_dir)

    extract_dir = workdir / "extracted" / asset.replace(".tar.gz", "")
    extract_dir.mkdir(parents=True, exist_ok=True)
    if not any(extract_dir.iterdir()):
        logger.info("Extracting %s", asset)
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(extract_dir)

    references = load_references()
    raw_out = stage / "raw"
    built_out = stage / "built"
    raw_out.mkdir(parents=True, exist_ok=True)
    built_out.mkdir(parents=True, exist_ok=True)

    built = 0
    failed = 0
    results = []
    match_dirs = sorted(p for p in extract_dir.iterdir() if p.is_dir())
    for index, match_dir in enumerate(match_dirs, 1):
        match_id = match_dir.name
        if not match_id.isdigit():
            continue
        if not (match_dir / "scorecard.json.gz").exists():
            continue
        try:
            details, chunks, error = load_raw_match_payloads(match_dir)
        except Exception as exc:
            logger.warning("Could not load %s: %s", match_id, exc)
            failed += 1
            continue
        if error or not details:
            failed += 1
            continue
        result = build_match(match_id, details, chunks, references)
        if not result.ok:
            failed += 1
            results.append({"match_id": match_id, "status": "failed", "error": result.error})
            continue

        # The raw payload is staged as well as the built match: the raw layer
        # is the evidence the whole architecture rests on, so it has to survive
        # the migration rather than be fetched again later.
        staged_raw = raw_out / match_id
        staged_raw.mkdir(parents=True, exist_ok=True)
        for file_path in sorted(match_dir.glob("*.json.gz")):
            shutil.copy2(file_path, staged_raw / file_path.name)
        meta_path = match_dir / "meta.json"
        if meta_path.exists():
            shutil.copy2(meta_path, staged_raw / "meta.json")

        (built_out / f"{match_id}.json").write_text(
            json.dumps(result.match, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        built += 1
        results.append({
            "match_id": match_id,
            "status": "ok",
            "tier": result.tier,
            "fetched_at": _fetched_at(match_dir),
        })

    summary_path = stage / "worker_summaries" / f"{asset}.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps({
        "asset": asset,
        "repository": repo,
        "release": release,
        "built": built,
        "failed": failed,
        "matches": results,
    }, indent=2), encoding="utf-8")

    # The extracted copy is only needed while the batch is being processed.
    # Keeping it would hold three copies of every payload on disk, which the
    # whole corpus does not fit into.
    shutil.rmtree(extract_dir, ignore_errors=True)
    archive.unlink(missing_ok=True)

    logger.info("%s: built %s matches, %s failed", asset, built, failed)
    print(json.dumps({"asset": asset, "built": built, "failed": failed}, indent=2))
    return 0


def _newest_fetched_at(staged: Dict[str, List[Path]]) -> Dict[str, Path]:
    """For each match, the bundle that was fetched most recently.

    A match can appear in more than one batch. The newest fetch wins, so the
    migration keeps the most complete payload rather than an arbitrary one.
    """
    chosen: Dict[str, Path] = {}
    chosen_at: Dict[str, str] = {}
    for match_id, candidates in staged.items():
        best = None
        best_at = ""
        for path in candidates:
            fetched = _fetched_at(path) or ""
            if best is None or fetched > best_at:
                best = path
                best_at = fetched
        if best is not None:
            chosen[match_id] = best
            chosen_at[match_id] = best_at
    return chosen


def _raw_digest(directory: Optional[Path]) -> Optional[str]:
    """The digest of a match's stored scorecard."""
    if directory is None:
        return None
    scorecard = Path(directory) / "scorecard.json.gz"
    if not scorecard.exists():
        return None
    with gzip.open(scorecard, "rb") as handle:
        return shard_store.member_digest(handle.read())


def run_merge(args: argparse.Namespace) -> int:
    """Group the staged output into shards and write the catalog."""
    workdir = Path(args.workdir)
    stage = workdir / "stage"
    raw_stage = stage / "raw"
    built_stage = stage / "built"
    if not built_stage.exists():
        logger.error(f"Nothing staged: {built_stage} does not exist")
        return 1

    archives_dir = workdir / "archives"
    archives_dir.mkdir(parents=True, exist_ok=True)
    connection = catalog_db.open_catalog(workdir / "lake_catalog.sqlite")

    # -- the raw layer ---------------------------------------------------
    staged_raw: Dict[str, List[Path]] = {}
    for path in sorted(raw_stage.glob("*/")):
        match_id = path.name
        staged_raw.setdefault(match_id, []).append(path)
    chosen_raw = _newest_fetched_at(staged_raw)

    # The shards are written one at a time. Holding the whole raw layer in
    # memory would need several gigabytes, so each shard is gathered, written
    # and released before the next one is started.
    raw_written = []
    for index in range(shard_store.NUM_SHARDS):
        members: Dict[str, bytes] = {}
        for match_id, directory in chosen_raw.items():
            if shard_store.shard_id(match_id) != index:
                continue
            for file_path in sorted(Path(directory).glob("*.json.gz")):
                with gzip.open(file_path, "rb") as handle:
                    members[f"{match_id}/{file_path.name}"] = handle.read()
        if not members:
            continue
        name = shard_store.shard_name(shard_store.LAYER_RAW, index)
        shard_store.write_pack(archives_dir / name, shard_store.LAYER_RAW, members)
        raw_written.append(name)
        members.clear()
    logger.info("Seeded %s raw matches into %s shards", len(chosen_raw), len(raw_written))

    # -- the built layer -------------------------------------------------
    # Only the digest and the metadata are kept, not the match bodies. The
    # built layer is several gigabytes, and holding it in memory alongside
    # the shards would not fit.
    built_digests: Dict[str, str] = {}
    built_summary: Dict[str, dict] = {}
    built_written = []
    for index in range(shard_store.NUM_SHARDS):
        members: Dict[str, bytes] = {}
        for path in sorted(built_stage.glob("*.json")):
            match_id = path.stem
            if shard_store.shard_id(match_id) != index:
                continue
            payload = path.read_bytes()
            members[f"{match_id}.json"] = payload
            built_digests[match_id] = shard_store.member_digest(payload)
            try:
                match = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            meta = match.get("meta") or {}
            outcome = match.get("outcome") or {}
            built_summary[match_id] = {
                "tier": meta.get("validation_status") or "clean",
                "parser_version": meta.get("parser_version") or PARSER_VERSION,
                "schema_version": meta.get("schema_version"),
                "match_date": (meta.get("dates") or [None])[0],
                "format": meta.get("format"),
                "format_bucket": meta.get("format_bucket"),
                "competition": meta.get("competition"),
                "season": meta.get("season"),
                "gender": meta.get("gender"),
                "winner": outcome.get("winner"),
                "result": outcome.get("result"),
                "total_balls": sum(
                    1 for inn in match.get("innings") or []
                    for over in inn.get("overs") or []
                    for _ in over.get("deliveries") or []
                ),
            }
        if not members:
            continue
        name = shard_store.shard_name(shard_store.LAYER_BUILT, index)
        shard_store.write_pack(archives_dir / name, shard_store.LAYER_BUILT, members)
        built_written.append(name)
        members.clear()
    logger.info("Seeded %s built matches into %s shards", len(built_digests), len(built_written))

    # -- the catalog -----------------------------------------------------
    now = datetime.now(timezone.utc)
    for match_id, summary in built_summary.items():
        catalog_db.upsert_fetch_and_build(
            connection, match_id,
            raw_archive=shard_store.shard_name(shard_store.LAYER_RAW, shard_store.shard_id(match_id)),
            raw_sha256=_raw_digest(chosen_raw.get(match_id)),
            raw_fetched_at=_fetched_at(chosen_raw[match_id]) if match_id in chosen_raw else now.isoformat(),
            built_archive=shard_store.shard_name(shard_store.LAYER_BUILT, shard_store.shard_id(match_id)),
            built_sha256=built_digests[match_id],
            parser_version=summary["parser_version"],
            schema_version=summary["schema_version"],
            validation_tier=summary["tier"],
            maturity_state="settled",
            completeness_gaps=[],
            identity={
                "match_date": summary["match_date"],
                "format": summary["format"],
                "format_bucket": summary["format_bucket"],
                "competition": summary["competition"],
                "season": summary["season"],
                "gender": summary["gender"],
                "winner": summary["winner"],
                "result": summary["result"],
            },
            index_meta={
                "total_balls": sum(
                    1 for inn in match.get("innings") or []
                    for over in inn.get("overs") or []
                    for _ in over.get("deliveries") or []
                ),
            },
            now_utc=now,
        )
    connection.commit()

    catalog_db.save_catalog_gz(connection, workdir / "lake_catalog.sqlite.gz")

    summary = {
        "raw_matches": len(chosen_raw),
        "built_matches": len(built_digests),
        "raw_shards": len(raw_written),
        "built_shards": len(built_written),
        "catalog_rows": catalog_db.count_matches(connection),
    }
    logger.info("Merge complete: %s", json.dumps(summary))
    print(json.dumps(summary, indent=2))
    connection.close()
    return 0


def run_verify(args: argparse.Namespace) -> int:
    """Compare the migrated lake against the published master."""
    workdir = Path(args.workdir)
    connection = catalog_db.load_catalog_gz(workdir / "lake_catalog.sqlite.gz")

    master_dir = workdir / "master"
    master_dir.mkdir(parents=True, exist_ok=True)
    master_tar = master_dir / "cricket_lake_unfiltered_master.tar.gz"
    if not master_tar.exists():
        release_store.download_asset(
            args.repo, MASTER_RELEASE, master_tar.name, master_dir
        )

    published: Dict[str, str] = {}
    with tarfile.open(master_tar, "r|gz") as tar:
        for member in tar:
            if not member.isfile():
                continue
            name = Path(member.name).name
            if not name.endswith(".json") or name == "lake_manifest.json":
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            try:
                match = json.loads(handle.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            match_id = str(match.get("match_id") or Path(name).stem)
            published[match_id] = (match.get("meta") or {}).get("validation_status") or "clean"

    migrated = {
        str(row["match_id"]): str(row["validation_tier"])
        for row in connection.execute("SELECT match_id, validation_tier FROM matches")
    }

    only_published = sorted(set(published) - set(migrated))
    only_migrated = sorted(set(migrated) - set(published))
    tier_changes = [
        (match_id, published[match_id], migrated[match_id])
        for match_id in sorted(set(published) & set(migrated))
        if published[match_id] != migrated[match_id]
    ]

    report = {
        "published": len(published),
        "migrated": len(migrated),
        "only_in_published": len(only_published),
        "only_in_migrated": len(only_migrated),
        "tier_changes": len(tier_changes),
        "published_tiers": _counts(published.values()),
        "migrated_tiers": _counts(migrated.values()),
    }
    print(json.dumps(report, indent=2))
    if only_published[:20]:
        print("\nfirst ids only in the published lake:")
        for match_id in only_published[:20]:
            print("  ", match_id)
    if tier_changes[:20]:
        print("\nfirst tier changes:")
        for match_id, before, after in tier_changes[:20]:
            print(f"   {match_id}: {before} -> {after}")
    connection.close()
    return 0


def _counts(values: Iterable[str]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worker", action="store_true", help="Process one raw batch asset")
    parser.add_argument("--merge", action="store_true", help="Write the shards and the catalog")
    parser.add_argument("--verify", action="store_true", help="Compare against the published master")
    parser.add_argument("--list-assets", action="store_true", help="List the raw batch assets")
    parser.add_argument("--batch", default=None, help="The raw batch asset to process")
    parser.add_argument("--source-repo", default=release_store.DEFAULT_REPO)
    parser.add_argument("--source-release", default="data-unfiltered-batches")
    parser.add_argument("--workdir", default="migrate_work")
    parser.add_argument("--repo", default=release_store.DEFAULT_REPO)
    args = parser.parse_args()

    if args.list_assets:
        assets = all_source_assets(args.repo)
        print(json.dumps([
            {"repository": repo, "release": release, "asset": asset}
            for repo, release, asset in assets
        ], indent=2))
        return 0

    if args.worker:
        if not args.batch:
            logger.error("--worker needs --batch")
            return 1
        return run_worker(args)
    if args.merge:
        return run_merge(args)
    if args.verify:
        return run_verify(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
