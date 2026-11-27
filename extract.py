#!/usr/bin/env python3
"""
extract.py
Production CLI Extractor for ESPNcricinfo Data Lake.

Fetches a match, stores every raw payload it received, then builds the Match
JSON through the shared builder. This file is only a command-line wrapper:
the fetch lives in ``src.commentary`` and the build in ``src.builder``, so the
live path and the offline re-parser cannot drift apart.
"""
import argparse
import csv
import json
import logging
import random
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.builder import References, build_match
from src.client import CircuitBreakerTripped, CricinfoClient
from src.commentary import FetchBundleError, fetch_match_bundle
from src.cricsheet_audit import CricsheetAuditor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("extractor")


def load_references(repo_root: Path) -> References:
    """Load player profiles, people registry, and venue mappings."""
    profiles_path = repo_root / "data" / "player_profiles_cache.json"
    player_profiles = {}
    if profiles_path.exists():
        with open(profiles_path, "r", encoding="utf-8") as f:
            player_profiles = json.load(f)

    venue_path = repo_root / "data" / "venue_to_country.json"
    venue_map = {}
    if venue_path.exists():
        with open(venue_path, "r", encoding="utf-8") as f:
            venue_map = json.load(f)

    people_path = repo_root / "data" / "people.csv"
    people_registry = {}
    if people_path.exists():
        with open(people_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                cid = row.get("identifier")
                cricinfo_id = row.get("key_cricinfo")
                if cricinfo_id and cid:
                    people_registry[str(cricinfo_id)] = cid

    return References(
        player_profiles=player_profiles,
        people_registry=people_registry,
        venue_map=venue_map,
    )


def get_target_match_ids(args: argparse.Namespace, repo_root: Path) -> List[str]:
    """Resolve target match IDs from args (CLI, file, or manifest batch)."""
    if args.match_ids:
        return [m.strip() for m in args.match_ids.split(",") if m.strip()]

    if args.ids_file:
        p = Path(args.ids_file)
        if not p.exists():
            logger.error(f"IDs file not found: {p}")
            sys.exit(1)
        if p.suffix == ".json":
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return [str(m).strip() for m in data if str(m).strip()]
                elif isinstance(data, dict):
                    return [str(m).strip() for m in data.keys()]
        else:
            with open(p, "r", encoding="utf-8") as f:
                return [line.strip() for line in f if line.strip() and not line.startswith("#")]

    manifest_path = Path(args.manifest) if args.manifest else (repo_root / "data" / "manifest.json")
    if not manifest_path.exists():
        logger.error(f"Manifest not found: {manifest_path}")
        sys.exit(1)

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    all_ids = [str(item["match_id"]) if isinstance(item, dict) else str(item) for item in manifest]
    start_idx = args.batch_index * args.batch_size
    end_idx = start_idx + args.batch_size
    batch_ids = all_ids[start_idx:end_idx]
    logger.info(f"Resolved batch {args.batch_index}: slice [{start_idx}:{end_idx}] -> {len(batch_ids)} matches")
    return batch_ids


def atomic_write_json(file_path: Path, data: Any):
    """Write data to a temp file and atomically rename to avoid partial writes."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = file_path.with_suffix(".tmp.json")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    temp_path.replace(file_path)


def main():
    parser = argparse.ArgumentParser(description="ESPNcricinfo Lake Match Extractor")
    parser.add_argument("--batch-index", type=int, default=0, help="Batch index for manifest slice")
    parser.add_argument("--batch-size", type=int, default=500, help="Batch size for manifest slice")
    parser.add_argument("--match-ids", type=str, default=None, help="Comma-separated list of match IDs")
    parser.add_argument("--ids-file", type=str, default=None, help="Path to text or JSON file of match IDs")
    parser.add_argument("--manifest", type=str, default=None, help="Path to manifest.json")
    parser.add_argument("--raw-dir", type=str, default="data/raw", help="Path to store raw HTTP payloads")
    parser.add_argument("--output-dir", type=str, default="data/matches", help="Path to store parsed Match v3 JSONs")
    parser.add_argument("--cricsheet-dir", type=str, default=None, help="Path to local Cricsheet JSON dir for audit")
    parser.add_argument("--delay", type=float, default=0.6, help="Base delay between requests in seconds")
    parser.add_argument("--skip-existing", action="store_true", help="Skip already-extracted matches")
    parser.add_argument("--circuit-breaker-limit", type=int, default=5, help="Circuit breaker trip threshold")
    parser.add_argument(
        "--allow-validation-failures",
        action="store_true",
        help="Quarantine instead of drop: write matches that fail validation with "
             "meta.validation_status='failed' plus the error list",
    )
    parser.add_argument("--chunk-index", type=int, default=None,
                        help="0-based chunk to take after the id list is resolved (any source)")
    parser.add_argument("--chunk-size", type=int, default=None,
                        help="Chunk size; applies to --ids-file/--match-ids too")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent
    raw_dir = Path(args.raw_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    references = load_references(repo_root)
    auditor = None
    if args.cricsheet_dir and Path(args.cricsheet_dir).exists():
        auditor = CricsheetAuditor(str(repo_root / "data" / "people.csv"))

    target_ids = get_target_match_ids(args, repo_root)
    if args.chunk_size:
        lo = (args.chunk_index or 0) * args.chunk_size
        target_ids = target_ids[lo:lo + args.chunk_size]
        logger.info(f"Sliced to chunk {args.chunk_index}: [{lo}:{lo + args.chunk_size}] -> {len(target_ids)} ids")
    total_targets = len(target_ids)
    logger.info(f"Starting extraction of {total_targets} matches...")

    client = CricinfoClient(breaker_threshold=args.circuit_breaker_limit)

    succeeded = 0
    quarantined = 0
    failed_matches: Dict[str, str] = {}
    skipped = 0

    failed_log_path = output_dir / "failed_matches.json"

    try:
        for idx, mid in enumerate(target_ids, 1):
            out_file = output_dir / f"{mid}.json"
            if args.skip_existing and out_file.exists():
                logger.info(f"[{idx}/{total_targets}] Match {mid} exists, skipping (--skip-existing)")
                skipped += 1
                continue

            logger.info(f"[{idx}/{total_targets}] Extracting match {mid}...")

            # 1. Fetch and store every raw payload. The raw bundle is written
            #    to disk before anything is built, so a build failure never
            #    costs the fetch.
            try:
                details, commentary_by_inn, _fetch_meta = fetch_match_bundle(
                    client=client,
                    match_id=mid,
                    raw_dir=raw_dir,
                    delay_sec=args.delay,
                )
            except FetchBundleError as exc:
                logger.warning(f"Failed to fetch match {mid}: {exc}")
                failed_matches[mid] = str(exc)
                continue

            # 2. Build the match and assign its tier in one pass.
            result = build_match(
                mid=mid,
                raw_details=details,
                commentary_chunks_by_inn=commentary_by_inn,
                references=references,
            )
            if not result.ok:
                logger.warning(f"Failed to build match {mid}: {result.error}")
                failed_matches[mid] = result.error or "Unknown build failure"
                continue

            match_v3 = result.match

            if result.tier == "failed" and not args.allow_validation_failures:
                logger.warning(
                    f"Validation failed for match {mid} ({len(result.assessment.errors)} errors): "
                    f"{result.assessment.errors[:2]}"
                )
                failed_matches[mid] = f"Validation errors: {'; '.join(result.assessment.errors[:3])}"
                continue

            # 3. Optional Cricsheet Audit
            if auditor and args.cricsheet_dir:
                cs_file = Path(args.cricsheet_dir) / f"{mid}.json"
                if cs_file.exists():
                    try:
                        with open(cs_file, "r", encoding="utf-8") as f:
                            cs_data = json.load(f)
                        audit_res = auditor.audit_match(match_v3, cs_data)
                        if not audit_res.is_valid:
                            logger.warning(f"Cricsheet audit failed for {mid}: {audit_res.hard_errors[:2]}")
                            # Soft warn vs hard fail: record but write
                            match_v3["meta"]["audit_status"] = "audit_warning"
                        else:
                            match_v3["meta"]["audit_status"] = "audit_verified"
                    except Exception as e:
                        logger.warning(f"Could not run Cricsheet audit for {mid}: {e}")

            # 4. Atomic write
            atomic_write_json(out_file, match_v3)
            if result.tier == "failed":
                quarantined += 1
                logger.info(f"Quarantined match {mid} -> {out_file.name}")
            else:
                succeeded += 1
                logger.info(f"Successfully extracted & validated match {mid} -> {out_file.name}")

            # Jittered polite backoff
            time.sleep(args.delay + random.uniform(0.1, 0.4))

    except CircuitBreakerTripped as cbe:
        logger.critical(f"CIRCUIT BREAKER TRIPPED: {cbe}")
        if failed_matches:
            with open(failed_log_path, "w", encoding="utf-8") as f:
                json.dump(failed_matches, f, indent=2)
        sys.exit(2)

    except KeyboardInterrupt:
        logger.info("Extraction interrupted by user.")

    finally:
        # Save failed matches log
        if failed_matches:
            with open(failed_log_path, "w", encoding="utf-8") as f:
                json.dump(failed_matches, f, indent=2)
            logger.info(f"Wrote {len(failed_matches)} failed matches to {failed_log_path}")

        logger.info("=" * 60)
        logger.info(f"Extraction Batch Complete.")
        logger.info(
            f"Total: {total_targets} | Succeeded: {succeeded} | Quarantined: {quarantined} "
            f"| Skipped: {skipped} | Failed: {len(failed_matches)}"
        )
        logger.info("=" * 60)

    # A batch that fetched nothing but errors must not look green in CI.
    if succeeded == 0 and quarantined == 0 and skipped == 0 and total_targets > 0:
        sys.exit(3)
    sys.exit(0)


if __name__ == "__main__":
    main()
