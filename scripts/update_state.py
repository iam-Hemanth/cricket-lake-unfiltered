#!/usr/bin/env python3
"""
Advance lake-state after a publish attempt.

The old workflow added every *planned* match id to ``known_ids`` even when its
chunk failed, so failed matches were never retried. This script instead:

  * marks only ids that produced a JSON file as known;
  * keeps everything else in a persistent ``retry`` queue (with the original
    manifest entry) until it succeeds or hits ``--max-attempts``;
  * records permanently failed ids under ``abandoned`` instead of silently
    pretending they are in the lake.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def _load(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _entry_map(entries: Iterable[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for entry in entries:
        if isinstance(entry, dict) and entry.get("match_id") is not None:
            out[str(entry["match_id"])] = entry
    return out


def collect_succeeded(chunks_dir: Path) -> set[str]:
    if not chunks_dir.exists():
        return set()
    return {p.stem for p in chunks_dir.rglob("*.json")
            if p.stem.isdigit() and p.name != "failed_matches.json"}


def collect_failures(failed_dir: Path) -> dict[str, str]:
    failures: dict[str, str] = {}
    if not failed_dir.exists():
        return failures
    for path in failed_dir.rglob("failed_matches.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            failures.update({str(k): str(v) for k, v in data.items()})
    return failures


def compute_state(
    *,
    known_prev: Iterable[str],
    state_prev: dict,
    full_entries: list[dict],
    planned_entries: list[dict],
    succeeded: set[str],
    failures: dict[str, str],
    now: str,
    stamp: str | None,
    max_attempts: int,
) -> tuple[set[str], dict, dict]:
    known = {str(x) for x in known_prev}
    state = dict(state_prev or {})
    abandoned: dict[str, dict] = {
        str(k): dict(v) for k, v in (state.get("abandoned") or {}).items()
    }
    known |= set(abandoned)
    known |= succeeded

    retry: dict[str, dict] = {}
    for mid, record in (state.get("retry") or {}).items():
        mid = str(mid)
        if mid in known:
            continue
        retry[mid] = dict(record) if isinstance(record, dict) else {"attempts": 0}

    full = _entry_map(full_entries)
    planned_ids = {str(e["match_id"]) for e in planned_entries
                   if isinstance(e, dict) and e.get("match_id") is not None}

    for mid, entry in full.items():
        if mid in known:
            continue
        record = retry.get(mid)
        if record is None:
            record = {"attempts": 0, "first_seen": now}
            retry[mid] = record
        record["manifest"] = entry
        if mid in failures:
            record["attempts"] = int(record.get("attempts", 0)) + 1
            record["last_error"] = failures[mid]
            record["last_attempt"] = now
            record.pop("deferred", None)
        elif mid in planned_ids:
            # Planned but no failure record: the chunk died before writing its
            # log. Retry without burning an attempt.
            record.setdefault("last_error", "no failure record from extract job")
            record.pop("deferred", None)
        else:
            record["last_error"] = "deferred by max_matches cap"
            record["deferred"] = True

    for mid in list(retry):
        if int(retry[mid].get("attempts", 0)) >= max_attempts:
            record = retry.pop(mid)
            record["abandoned_at"] = now
            record["last_error"] = record.get("last_error", "max attempts reached")
            abandoned[mid] = record
            known.add(mid)

    updated = dict(state)
    updated["last_success_utc"] = now
    updated["last_delta_count"] = len(succeeded)
    updated["known_ids"] = len(known)
    updated["retry_count"] = len(retry)
    updated["abandoned_count"] = len(abandoned)
    updated["retry"] = retry
    updated["abandoned"] = abandoned
    if stamp and succeeded:
        updated["last_delta_asset"] = stamp

    summary = {
        "succeeded": len(succeeded),
        "known": len(known),
        "retry": len(retry),
        "abandoned": len(abandoned),
        "failed_this_run": len(failures),
        "discovered_total": len(full),
        "planned": len(planned_ids),
        "deferred": sum(1 for r in retry.values() if r.get("deferred")),
    }
    return known, updated, summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Advance lake-state after publish")
    parser.add_argument("--state-dir", default=".state")
    parser.add_argument("--manifest-full", default="data/manifest_full.json")
    parser.add_argument("--manifest-planned", default="data/manifest_delta.json")
    parser.add_argument("--chunks-dir", default="merged/raw")
    parser.add_argument("--failed-dir", default="failed")
    parser.add_argument("--stamp", default=None)
    parser.add_argument("--max-attempts", type=int, default=20)
    parser.add_argument("--now", default=None, help="Override timestamp (tests)")
    args = parser.parse_args()

    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    known_prev = _load(state_dir / "known_ids.json", [])
    state_prev = _load(state_dir / "lake_state.json", {})
    full_entries = _load(Path(args.manifest_full), [])
    planned_entries = _load(Path(args.manifest_planned), [])

    now = args.now or (datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    known, state, summary = compute_state(
        known_prev=known_prev,
        state_prev=state_prev,
        full_entries=full_entries,
        planned_entries=planned_entries,
        succeeded=collect_succeeded(Path(args.chunks_dir)),
        failures=collect_failures(Path(args.failed_dir)),
        now=now,
        stamp=args.stamp,
        max_attempts=args.max_attempts,
    )

    (state_dir / "known_ids.json").write_text(
        json.dumps(sorted(known)), encoding="utf-8"
    )
    (state_dir / "lake_state.json").write_text(
        json.dumps(state, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
