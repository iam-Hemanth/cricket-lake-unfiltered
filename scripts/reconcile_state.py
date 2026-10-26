#!/usr/bin/env python3
"""
Reconcile ``known_ids`` against what is actually published.

A match can end up in ``known_ids`` without appearing in the master index or
any delta asset (state drift, a failed publish, or an overwritten delta). This
script puts those ids back into the retry queue so the next update re-extracts
them, and drops retry entries that are now published.

Published means: present in ``matches_index.csv`` OR in any
``lake_delta_*.tar.gz`` archive. Abandoned ids stay suppressed.
"""
from __future__ import annotations

import argparse
import csv
import json
import tarfile
from datetime import datetime, timezone
from pathlib import Path


def _load(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def master_ids(index_path: Path) -> set[str]:
    if not index_path.exists():
        return set()
    with open(index_path, newline="", encoding="utf-8") as f:
        return {str(row["match_id"]) for row in csv.DictReader(f) if row.get("match_id")}


def delta_ids(deltas_dir: Path) -> set[str]:
    ids: set[str] = set()
    for archive in sorted(deltas_dir.glob("lake_delta_*.tar.gz")):
        try:
            with tarfile.open(archive, "r:gz") as tf:
                for member in tf.getmembers():
                    name = Path(member.name).name
                    if name.endswith(".json") and not name.startswith("failed_matches"):
                        stem = name[:-5]
                        if stem.isdigit():
                            ids.add(stem)
        except (tarfile.TarError, OSError):
            continue
    return ids


def reconcile(
    *,
    known_prev: list[str],
    state_prev: dict,
    published: set[str],
    now: str,
) -> tuple[set[str], dict, dict]:
    known = {str(x) for x in known_prev}
    state = dict(state_prev or {})
    abandoned = {str(k): dict(v) for k, v in (state.get("abandoned") or {}).items()}
    suppressed = set(abandoned)

    # Keep abandoned ids suppressed even if they are absent from known_ids.
    known |= suppressed
    stranded = sorted(known - published - suppressed)
    known = (known & published) | suppressed

    retry = {
        str(mid): dict(record)
        for mid, record in (state.get("retry") or {}).items()
        if str(mid) not in published and str(mid) not in suppressed
    }
    for mid in stranded:
        record = retry.get(mid) or {"attempts": 0, "first_seen": now}
        record.setdefault("first_seen", now)
        record["manifest"] = record.get("manifest") or {"match_id": mid}
        record["last_error"] = (
            "known but missing from master index and every delta archive "
            "(state drift)"
        )
        record["reconciled_utc"] = now
        retry[mid] = record

    state.update({
        "known_ids": len(known),
        "retry_count": len(retry),
        "retry": retry,
        "last_reconciled_utc": now,
        "last_reconcile": {
            "published_ids": len(published),
            "stranded_ids": len(stranded),
            "stranded": stranded,
            "removed_from_retry": sorted(set((state.get("retry") or {})) - set(retry)),
        },
    })
    summary = {
        "published": len(published),
        "known_before": len(known_prev),
        "known_after": len(known),
        "stranded": len(stranded),
        "stranded_ids": stranded,
        "retry": len(retry),
        "abandoned": len(abandoned),
    }
    return known, state, summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile lake known ids with published data")
    parser.add_argument("--state-dir", default=".state")
    parser.add_argument("--master-index", required=True)
    parser.add_argument("--deltas-dir", required=True)
    parser.add_argument("--now", default=None)
    parser.add_argument("--summary-out", default=None)
    args = parser.parse_args()

    state_dir = Path(args.state_dir)
    known_prev = _load(state_dir / "known_ids.json", [])
    state_prev = _load(state_dir / "lake_state.json", {})
    published = master_ids(Path(args.master_index)) | delta_ids(Path(args.deltas_dir))
    now = args.now or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    known, state, summary = reconcile(
        known_prev=known_prev,
        state_prev=state_prev,
        published=published,
        now=now,
    )
    (state_dir / "known_ids.json").write_text(json.dumps(sorted(known)), encoding="utf-8")
    (state_dir / "lake_state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    if args.summary_out:
        Path(args.summary_out).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
