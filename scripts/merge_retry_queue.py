#!/usr/bin/env python3
"""
Merge the persistent retry queue into this run's discovery manifest.

Failed extractions are kept in ``lake_state.json`` under ``retry`` so they are
re-attempted even when they have aged out of the discovery lookback window.
The queue entries carry their original manifest metadata when available.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def merge(manifest_path: Path, state_path: Path) -> tuple[int, int]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    state = {}
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))

    retry = state.get("retry") or {}
    existing = {str(entry["match_id"]) for entry in manifest
                if isinstance(entry, dict) and "match_id" in entry}
    added = 0
    merged: list[dict] = []
    for mid, record in sorted(retry.items()):
        if mid in existing:
            continue
        entry = None
        if isinstance(record, dict):
            candidate = record.get("manifest")
            if isinstance(candidate, dict) and str(candidate.get("match_id")) == str(mid):
                entry = dict(candidate)
        if entry is None:
            entry = {"match_id": str(mid)}
        merged.append(entry)
        existing.add(str(mid))
        added += 1

    # Retries first: they are the oldest work and must not be starved by a cap.
    merged.extend(manifest)
    manifest_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    print(f"retry queue: {len(retry)} entries, {added} merged into manifest")
    return len(retry), added


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge retry queue into delta manifest")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--state", required=True)
    args = parser.parse_args()
    merge(Path(args.manifest), Path(args.state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
