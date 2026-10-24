#!/usr/bin/env python3
"""
scripts/build_recovery_queue.py
Turn the raw gap analysis into a prioritised recovery queue.

Tiers
  T1 dropped      matches the pipeline fetched and then threw away (validation)
  T2 cricsheet    matches Cricsheet has and the lake does not (parity target)
  T3 universe     everything else Statsguru lists that the lake has never held

Outputs missing/recovery_queue.json + missing/recovery_summary.json and appends
a tiers section to missing/MISSING.md.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
from collections import Counter
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("queue")


def load_ids(path: Path) -> set[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return {str(k) for k in data}
    return {str(x["match_id"]) if isinstance(x, dict) else str(x) for x in data}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--missing", default="missing/missing_matches.json")
    ap.add_argument("--failed-ids", default="missing/failed_ids.json")
    ap.add_argument("--known-index", default="matches_index.csv")
    ap.add_argument("--cricsheet-dir", default=None)
    ap.add_argument("--out", default="missing/recovery_queue.json")
    args = ap.parse_args()

    known: set[str] = set()
    if Path(args.known_index).exists():
        with open(args.known_index, newline="", encoding="utf-8") as f:
            known = {(r.get("match_id") or "").strip() for r in csv.DictReader(f)}
        known.discard("")

    universe = {m["match_id"]: m for m in
                json.loads(Path(args.missing).read_text(encoding="utf-8"))
                if m["match_id"] not in known}

    failed: set[str] = set()
    if Path(args.failed_ids).exists():
        failed = load_ids(Path(args.failed_ids)) - known

    queue: dict[str, dict] = {}

    # T1 - the pipeline's own drop list (highest value: already discovered)
    for mid in sorted(failed):
        queue[mid] = {"match_id": mid, "tier": "T1_dropped", "date": None,
                      "teams": None, "format": None, "gender": None}

    # T2 - Cricsheet parity
    t2 = 0
    if args.cricsheet_dir and Path(args.cricsheet_dir).is_dir():
        for p in Path(args.cricsheet_dir).glob("*.json"):
            mid = p.stem
            if mid in known or mid in queue:
                continue
            try:
                info = json.loads(p.read_text(encoding="utf-8")).get("info", {})
            except Exception:
                continue
            queue[mid] = {
                "match_id": mid,
                "tier": "T2_cricsheet",
                "date": (info.get("dates") or [None])[0],
                "teams": info.get("teams"),
                "format": info.get("match_type"),
                "gender": info.get("gender"),
                "event": (info.get("event") or {}).get("name"),
            }
            t2 += 1

    # T3 - rest of the Statsguru universe
    t3 = 0
    for mid, meta in universe.items():
        if mid in queue:
            continue
        queue[mid] = {"match_id": mid, "tier": "T3_universe", "date": meta.get("date"),
                      "teams": meta.get("teams"), "format": meta.get("format"),
                      "gender": "male", "event": None}
        t3 += 1

    ordered = sorted(queue.values(),
                     key=lambda x: ({"T1_dropped": 0, "T2_cricsheet": 1, "T3_universe": 2}[x["tier"]],
                                    x.get("date") or "9999"))
    Path(args.out).write_text(json.dumps(ordered, indent=2), encoding="utf-8")

    tiers = Counter(x["tier"] for x in ordered)
    genders = Counter((x["tier"], x.get("gender")) for x in ordered)
    summary = {
        "total": len(ordered),
        "tiers": dict(tiers),
        "tier_gender": {f"{t}/{g}": n for (t, g), n in genders.items()},
        "known_before": len(known),
        "output": args.out,
    }
    Path(args.out).with_name("recovery_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")
    logger.info("queue: %s", dict(tiers))

    md = Path("missing/MISSING.md")
    if md.exists():
        block = [
            "",
            "## Recovery tiers",
            "",
            "| Tier | Meaning | Matches |",
            "| --- | --- | --- |",
            f"| T1 | fetched then dropped by validation | {tiers.get('T1_dropped', 0):,} |",
            f"| T2 | missing vs Cricsheet (parity) | {tiers.get('T2_cricsheet', 0):,} |",
            f"| T3 | rest of the Statsguru universe | {tiers.get('T3_universe', 0):,} |",
            "",
            f"Queue: `{args.out}` ({len(ordered):,} ids, T1 first).",
            "",
        ]
        md.write_text(md.read_text(encoding="utf-8") + "\n".join(block), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
