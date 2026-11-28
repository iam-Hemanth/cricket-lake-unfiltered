#!/usr/bin/env python3
"""
scripts/pipeline_ingest.py
The lake's only path for new data.

Three modes, run in sequence by ``ingest.yml``:

``--plan``
    Work out which matches to fetch. The plan comes from the catalog, not
    from a file in the repository: a match is fetched when it is missing, when
    its scheduled refresh is due, or when it was named explicitly.

``--worker --chunk N``
    Fetch a chunk of the plan, store every raw payload, and build the
    matches. A match whose build fails still keeps its raw payload.

``--commit``
    Compare the fetched payloads against the catalog, keep only what
    actually changed, upload it, and update the catalog.

The plan never becomes a file in the repository. A match that could not be
fetched this time is recorded in the catalog as provisional, and the next
run picks it up again.
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src import catalog_db, release_store, shard_store  # noqa: E402
from src.builder import PARSER_VERSION, References, build_match  # noqa: E402
from src.catalog_fields import extract_catalog_metadata  # noqa: E402
from src.client import CircuitBreakerTripped, CricinfoClient  # noqa: E402
from src.commentary import FetchBundleError, bundle_sha256, fetch_match_bundle  # noqa: E402
from src.completeness import failure_maturity, retry_schedule  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("ingest")


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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(now: Optional[datetime] = None) -> str:
    """The timestamp used in asset names."""
    now = now or _now()
    return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _stored_raw_digest(raw_match_dir: Path) -> Optional[str]:
    """The digest of a bundle already on disk, or None when there is none."""
    if not (Path(raw_match_dir) / "scorecard.json.gz").exists():
        return None
    return bundle_sha256(Path(raw_match_dir))


def _stored_fetched_at(raw_match_dir: Path) -> Optional[str]:
    """When the bundle on disk was fetched, if it recorded that."""
    meta_path = Path(raw_match_dir) / "fetch_meta.json"
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8")).get("fetched_at")
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

def build_plan(
    connection,
    discovered: Sequence[str],
    due: Sequence[str],
    explicit: Sequence[str],
    max_matches: int,
    now: datetime,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """Decide which matches this run will fetch.

    A match is fetched when it has no raw payload yet, when its scheduled
    refresh is due, or when it was named explicitly. An explicit match that
    already settled is only fetched again under ``--force-refresh``.

    The plan is returned, not written to the catalog: the plan stage must not
    leave ephemeral state behind.
    """
    known = set(catalog_db.all_match_ids(connection))
    wanted: List[str] = []
    seen = set()

    for match_id in explicit:
        if match_id in seen:
            continue
        row = catalog_db.get_match(connection, str(match_id))
        if (
            row is not None
            and not force_refresh
            and row["maturity_state"] not in ("provisional", "legacy_unbacked")
        ):
            continue
        seen.add(match_id)
        wanted.append(match_id)

    for match_id in discovered:
        if match_id not in known and match_id not in seen:
            seen.add(match_id)
            wanted.append(match_id)

    for match_id in due:
        if match_id not in seen:
            seen.add(match_id)
            wanted.append(match_id)

    truncated = wanted[max_matches:] if max_matches and len(wanted) > max_matches else []
    selected = wanted[:max_matches] if max_matches else wanted

    refresh_attempts = {}
    for match_id in selected:
        row = catalog_db.get_match(connection, str(match_id))
        refresh_attempts[str(match_id)] = int(row["fetch_attempts"] or 0) if row is not None else 0

    chunks = []
    chunk_count = _chunk_count(len(selected))
    chunk_size = max(1, (len(selected) + chunk_count - 1) // chunk_count) if selected else 1
    for index in range(chunk_count):
        part = selected[index * chunk_size:(index + 1) * chunk_size]
        if part:
            chunks.append({"chunk": index, "match_ids": part})
    return {
        "stamp": _stamp(now),
        "created_at": now.isoformat(),
        "total_selected": len(selected),
        "deferred": len(truncated),
        "deferred_ids": [str(mid) for mid in truncated],
        "refresh_attempts": refresh_attempts,
        "chunks": chunks,
    }


def _chunk_count(total: int) -> int:
    """How many chunks the plan is split into."""
    if total <= 0:
        return 1
    return max(1, min(20, (total + 19) // 20))


def _open_remote_catalog(
    workdir: Path,
    repo: str,
    allow_bootstrap: bool = False,
) -> Tuple[object, Optional[str]]:
    """Load the catalog the release holds, downloading it when it is absent.

    Returns the connection and the remote asset's ``updated_at`` as read at
    open time. The commit step uses that value to refuse to overwrite a
    catalog another run published in the meantime.

    An absent catalog is an error unless the caller explicitly bootstraps a
    brand-new lake: starting from an empty catalog would silently wipe the
    lake's knowledge of every match.
    """
    staged = workdir / "lake_catalog.sqlite.gz"
    if staged.exists():
        return catalog_db.load_catalog_gz(staged), None

    asset = release_store.find_asset(
        repo, release_store.RELEASE_CATALOG, "lake_catalog.sqlite.gz"
    )
    if asset is None:
        if not allow_bootstrap:
            raise SystemExit(
                "The lake catalog is missing from "
                f"{release_store.RELEASE_CATALOG}; refusing to start from an empty "
                "catalog. Pass --bootstrap only for a brand-new lake."
            )
        return catalog_db.open_catalog(workdir / "lake_catalog.sqlite"), None

    release_store.download_asset(
        repo, release_store.RELEASE_CATALOG, "lake_catalog.sqlite.gz", workdir
    )
    return catalog_db.load_catalog_gz(staged), asset.get("updated_at")


def run_plan(args: argparse.Namespace) -> int:
    """Work out what to fetch."""
    now = _now()
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    connection, _catalog_updated_at = _open_remote_catalog(
        workdir, args.repo, allow_bootstrap=args.bootstrap
    )

    discovered: List[str] = []
    if args.discover:
        from scripts.discover_delta import discover_match_ids
        discovered = discover_match_ids(since=args.since, known_ids=set(catalog_db.all_match_ids(connection)))

    due = catalog_db.select_due_refreshes(connection, limit=args.max_refresh, now_utc=now)
    explicit = [m.strip() for m in (args.match_ids or "").split(",") if m.strip()]

    plan = build_plan(
        connection, discovered, due, explicit, args.max_matches, now,
        force_refresh=args.force_refresh,
    )

    plan_path = workdir / "ingest_plan.json"
    plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    logger.info(
        "Planned %s matches in %s chunks (%s deferred)",
        plan["total_selected"], len(plan["chunks"]), plan["deferred"],
    )
    print(json.dumps({
        "total_selected": plan["total_selected"],
        "chunks": len(plan["chunks"]),
        "deferred": plan["deferred"],
        "plan_path": str(plan_path),
    }, indent=2))
    connection.close()
    return 0


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def run_worker(args: argparse.Namespace) -> int:
    """Fetch a chunk, store the raw payloads, and build the matches."""
    workdir = Path(args.workdir)
    plan_path = workdir / "ingest_plan.json"
    if not plan_path.exists():
        logger.error(f"Plan not found: {plan_path}")
        return 1
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    chunk = None
    for part in plan.get("chunks") or []:
        if int(part.get("chunk")) == int(args.chunk):
            chunk = part
            break
    if chunk is None:
        logger.error(f"Chunk {args.chunk} is not in the plan")
        return 1

    match_ids = [str(m) for m in chunk.get("match_ids") or []]
    refresh_attempts = plan.get("refresh_attempts") or {}
    references = load_references()
    client = CricinfoClient(breaker_threshold=args.circuit_breaker_limit)

    raw_root = workdir / "raw"
    built_root = workdir / "built"
    raw_root.mkdir(parents=True, exist_ok=True)
    built_root.mkdir(parents=True, exist_ok=True)

    results: List[Dict[str, Any]] = []
    for index, match_id in enumerate(match_ids, 1):
        attempts = int(refresh_attempts.get(match_id, 0) or 0)
        raw_match_dir = raw_root / match_id
        logger.info("[%s/%s] Fetching match %s", index, len(match_ids), match_id)
        try:
            details, commentary, fetch_meta = fetch_match_bundle(
                client=client,
                match_id=match_id,
                raw_dir=raw_root,
                delay_sec=args.delay,
            )
        except FetchBundleError as exc:
            logger.warning("Fetch failed for %s: %s", match_id, exc)
            record: Dict[str, Any] = {
                "match_id": match_id, "status": "fetch_failed",
                "failure_kind": exc.kind, "error": str(exc),
                "refresh_attempts": attempts,
            }
            # A bundle can fail after the scorecard was stored. That fetch
            # must still reach the raw layer: no successful request is lost.
            raw_sha256 = _stored_raw_digest(raw_match_dir)
            if raw_sha256:
                maturity, next_refresh = retry_schedule(attempts, _now())
                record.update({
                    "raw_sha256": raw_sha256,
                    "raw_fetched_at": _stored_fetched_at(raw_match_dir),
                    "maturity_state": maturity,
                    "next_refresh_at": next_refresh,
                })
            results.append(record)
            continue
        except CircuitBreakerTripped as exc:
            logger.critical("Circuit breaker tripped: %s", exc)
            results.append({"match_id": match_id, "status": "fetch_failed",
                            "failure_kind": "transient_fetch", "error": str(exc),
                            "refresh_attempts": attempts})
            break

        raw_sha256 = fetch_meta.get("bundle_sha256") or _stored_raw_digest(raw_match_dir)
        raw_fetched_at = fetch_meta.get("fetched_at") or _stored_fetched_at(raw_match_dir)

        # The raw payload is already stored. The build may still fail, and
        # that must not cost the fetch.
        build = build_match(
            match_id, details, commentary, references, refresh_attempts=attempts,
        )
        if not build.ok:
            logger.warning("Build failed for %s: %s", match_id, build.error)
            maturity, next_refresh = failure_maturity(details, attempts, _now())
            results.append({
                "match_id": match_id,
                "status": "failed",
                "failure_kind": build.failure_kind,
                "error": build.error,
                "raw_sha256": raw_sha256,
                "raw_fetched_at": raw_fetched_at,
                "maturity_state": maturity,
                "next_refresh_at": next_refresh,
                "refresh_attempts": attempts,
            })
            continue

        built_path = built_root / f"{match_id}.json"
        built_path.write_text(
            json.dumps(build.match, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        identity, index_meta = extract_catalog_metadata(build.match)
        completeness = build.completeness
        results.append({
            "match_id": match_id,
            "status": "ok",
            "tier": build.tier,
            "raw_sha256": raw_sha256,
            "raw_fetched_at": raw_fetched_at,
            "built_sha256": shard_store.sha256_file(built_path),
            "parser_version": PARSER_VERSION,
            "schema_version": (build.match.get("meta") or {}).get("schema_version"),
            "completeness_gaps": completeness.gaps if completeness else [],
            "maturity_state": completeness.maturity_state if completeness else "settled",
            "next_refresh_at": completeness.next_refresh_at if completeness else None,
            "identity": identity,
            "index_meta": index_meta,
        })
        time.sleep(args.delay + random.uniform(0.1, 0.4))

    results_path = workdir / f"chunk_results_{args.chunk}.json"
    results_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    ok = sum(1 for r in results if r["status"] == "ok")
    logger.info("Chunk %s finished: %s built, %s failed", args.chunk, ok, len(results) - ok)
    print(json.dumps({"chunk": args.chunk, "built": ok, "total": len(results)}, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------

def run_commit(args: argparse.Namespace) -> int:
    """Keep what changed, upload it, and update the catalog."""
    workdir = Path(args.workdir)
    plan_path = workdir / "ingest_plan.json"
    if not plan_path.exists():
        logger.error(f"Plan not found: {plan_path}")
        return 1
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    stamp = plan.get("stamp") or _stamp()
    raw_pack_name = shard_store.pack_name(shard_store.LAYER_RAW, stamp)
    built_pack_name = shard_store.pack_name(shard_store.LAYER_BUILT, stamp)

    connection, catalog_updated_at = _open_remote_catalog(
        workdir, args.repo, allow_bootstrap=args.bootstrap
    )

    raw_root = workdir / "raw"
    built_root = workdir / "built"

    results: List[Dict[str, Any]] = []
    for path in sorted(workdir.glob("chunk_results_*.json")):
        results.extend(json.loads(path.read_text(encoding="utf-8")))

    now = _now()
    now_text = now.astimezone(timezone.utc).isoformat()
    changed_raw: Dict[str, Path] = {}
    changed_built: Dict[str, Path] = {}
    processed = set()

    for record in results:
        match_id = str(record.get("match_id"))
        processed.add(match_id)
        existing = catalog_db.get_match(connection, match_id)
        raw_sha256 = record.get("raw_sha256")
        raw_dir = raw_root / match_id

        if record.get("status") != "ok":
            if raw_sha256 and raw_dir.is_dir():
                # The fetch succeeded and the raw payload is stored under a
                # new digest; only the build failed. The payload still has to
                # reach the raw layer, so it is packed even when no built
                # match is produced.
                changed_raw[match_id] = raw_dir
                maturity = record.get("maturity_state")
                next_refresh = record.get("next_refresh_at")
                if existing is not None and existing["maturity_state"] == "legacy_unbacked":
                    logger.warning(
                        "legacy_unbacked %s: raw payload stored but the build failed; "
                        "keeping its published copy", match_id,
                    )
                    catalog_db.upsert_match(
                        connection, match_id,
                        raw_available=1,
                        raw_archive=raw_pack_name,
                        raw_sha256=raw_sha256,
                        raw_fetched_at=record.get("raw_fetched_at") or now_text,
                    )
                    catalog_db.record_fetch_failure(
                        connection, match_id,
                        failure_kind=record.get("failure_kind"),
                        error=record.get("error"),
                        next_refresh_at=next_refresh,
                        maturity_state=maturity,
                        now_utc=now,
                    )
                else:
                    catalog_db.upsert_fetch_and_build(
                        connection, match_id,
                        raw_archive=raw_pack_name,
                        raw_sha256=raw_sha256,
                        raw_fetched_at=record.get("raw_fetched_at"),
                        validation_tier="failed",
                        maturity_state=maturity,
                        next_refresh_at=next_refresh,
                        failure_kind=record.get("failure_kind") or "construction_error",
                        error=record.get("error"),
                        now_utc=now,
                        force_rebuild=True,
                    )
            else:
                attempts = int(
                    record.get("refresh_attempts")
                    or (existing["fetch_attempts"] if existing is not None else 0)
                    or 0
                )
                maturity, next_refresh = retry_schedule(attempts, now)
                catalog_db.record_fetch_failure(
                    connection, match_id,
                    failure_kind=record.get("failure_kind") or "transient_fetch",
                    error=record.get("error"),
                    next_refresh_at=next_refresh,
                    maturity_state=maturity,
                    now_utc=now,
                )
            continue

        if not raw_dir.is_dir():
            logger.warning("Built match %s has no staged raw payload; skipped", match_id)
            continue
        built_path = built_root / f"{match_id}.json"
        if not built_path.exists():
            logger.warning("Match %s has no built JSON; skipped", match_id)
            continue

        outcome = catalog_db.upsert_fetch_and_build(
            connection, match_id,
            raw_archive=raw_pack_name,
            raw_sha256=bundle_sha256(raw_dir),
            raw_fetched_at=record.get("raw_fetched_at"),
            built_archive=built_pack_name,
            built_sha256=shard_store.sha256_file(built_path),
            parser_version=record.get("parser_version"),
            schema_version=record.get("schema_version"),
            validation_tier=record.get("tier"),
            completeness_gaps=record.get("completeness_gaps") or [],
            maturity_state=record.get("maturity_state"),
            next_refresh_at=record.get("next_refresh_at"),
            failure_kind=None,
            error=None,
            identity=record.get("identity"),
            index_meta=record.get("index_meta"),
            now_utc=now,
        )
        if outcome["rebuild_required"]:
            changed_raw[match_id] = raw_dir
            changed_built[match_id] = built_path

    # A match that was planned but never reached a worker stays provisional
    # so the next run picks it up.
    for match_id in plan.get("deferred_ids") or []:
        match_id = str(match_id)
        if match_id in processed:
            continue
        catalog_db.record_fetch_failure(
            connection, match_id,
            failure_kind=None,
            error="deferred by max_matches cap",
            next_refresh_at=now_text,
            maturity_state="provisional",
            now_utc=now,
        )

    has_raw_changes = bool(changed_raw)
    has_built_changes = bool(changed_built)

    if has_raw_changes:
        _, raw_digests = shard_store.write_raw_pack(workdir / raw_pack_name, changed_raw)
        unpackable = sorted(set(changed_raw) - set(raw_digests))
        if unpackable:
            raise SystemExit(
                "raw bundles without a scorecard cannot be packed: "
                + ", ".join(unpackable[:10])
            )
        for match_id, digest in raw_digests.items():
            catalog_db.upsert_match(
                connection, match_id,
                raw_available=1, raw_archive=raw_pack_name, raw_sha256=digest,
            )

    if has_built_changes:
        _, built_digests = shard_store.write_built_pack(
            workdir / built_pack_name,
            {match_id: path.read_bytes() for match_id, path in changed_built.items()},
        )
        for match_id, digest in built_digests.items():
            catalog_db.upsert_match(
                connection, match_id,
                built_archive=built_pack_name, built_sha256=digest,
            )

    connection.commit()

    if args.upload:
        if has_raw_changes:
            release_store.upload_asset(args.repo, release_store.RELEASE_RAW, workdir / raw_pack_name)
        if has_built_changes:
            release_store.upload_asset(args.repo, release_store.RELEASE_BUILT, workdir / built_pack_name)

    summary = {
        "has_raw_changes": has_raw_changes,
        "has_built_changes": has_built_changes,
        "raw_changed": len(changed_raw),
        "built_changed": len(changed_built),
        "processed": len(results),
        "deferred": len(plan.get("deferred_ids") or []),
    }
    catalog_db.record_run(
        connection, f"ingest-{stamp}", "ingest",
        started_at=plan.get("created_at"), finished_at=now_text, summary=summary,
    )
    catalog_db.save_catalog_gz(connection, workdir / "lake_catalog.sqlite.gz")
    if args.upload:
        release_store.upload_asset(
            args.repo, release_store.RELEASE_CATALOG,
            workdir / "lake_catalog.sqlite.gz",
            expected_updated_at=catalog_updated_at,
        )

    logger.info("Commit complete: %s", json.dumps(summary))
    print(json.dumps(summary, indent=2))
    connection.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plan", action="store_true", help="Work out what to fetch")
    parser.add_argument("--worker", action="store_true", help="Fetch and build one chunk")
    parser.add_argument("--commit", action="store_true", help="Publish what changed")
    parser.add_argument("--chunk", type=int, default=0, help="Chunk index for --worker")
    parser.add_argument("--workdir", default="ingest_work")
    parser.add_argument("--repo", default=release_store.DEFAULT_REPO)
    parser.add_argument("--match-ids", default=None,
                        help="Comma-separated match ids to fetch explicitly")
    parser.add_argument("--since", default=None,
                        help="Discover matches played since this date (YYYY-MM-DD)")
    parser.add_argument("--discover", action="store_true",
                        help="Discover new matches from ESPN")
    parser.add_argument("--max-matches", type=int, default=400,
                        help="Maximum matches to fetch in one run")
    parser.add_argument("--max-refresh", type=int, default=200,
                        help="Maximum due refreshes to take in one run")
    parser.add_argument("--force-refresh", action="store_true",
                        help="Fetch the named matches even when they are settled")
    parser.add_argument("--delay", type=float, default=0.6)
    parser.add_argument("--circuit-breaker-limit", type=int, default=5)
    parser.add_argument("--upload", action="store_true",
                        help="Upload the assets and the catalog to the release")
    parser.add_argument("--bootstrap", action="store_true",
                        help="Allow starting from an empty catalog when the release has none")
    args = parser.parse_args()

    if args.plan:
        return run_plan(args)
    if args.worker:
        return run_worker(args)
    if args.commit:
        return run_commit(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
