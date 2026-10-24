#!/usr/bin/env python3
"""
scripts/audit_missing.py
Enumerate the full completed-match universe on ESPNcricinfo and diff it against
what is actually in the lake.

Outputs:
  missing/missing_matches.json   list of {match_id, date, teams, format, ...}
  missing/missing_by_year.csv    counts per year
  MISSING.md                     human summary

Usage:
  python scripts/audit_missing.py --from-year 2001 --known-index matches_index.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from curl_cffi import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from discover_delta import CLASSES, find_date, parse_date  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("audit")

CLASS_RANK = {"Test": 0, "ODI": 0, "T20I": 0, "First-Class": 1, "List A": 1, "T20": 1}

STATSGURU = ("https://stats.espncricinfo.com/ci/engine/records/team/match_results.html"
             "?class={cls};id={year};type=year")


def scrape_year(year: int) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for cls, label in CLASSES:
        url = STATSGURU.format(cls=cls, year=year)
        try:
            r = requests.get(url, impersonate="chrome124", timeout=40)
        except Exception as exc:
            logger.warning("%s/%s request failed: %s", year, label, exc)
            continue
        if r.status_code != 200:
            logger.warning("%s/%s HTTP %s", year, label, r.status_code)
            continue
        tbl = re.search(r'<table class="engineTable">(.*?)</table>', r.text, re.DOTALL)
        if not tbl:
            continue
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", tbl.group(1), re.DOTALL)[1:]:
            mid = re.search(r"/ci/engine/match/(\d+)\.html", tr)
            if not mid:
                continue
            cells = [re.sub(r"<[^>]+>", "", c).strip()
                     for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.DOTALL)]
            if len(cells) < 5:
                continue
            d = find_date(cells)
            if not d:
                continue
            entry = {
                "match_id": mid.group(1),
                "date": d,
                "teams": [cells[0], cells[1]],
                "winner": cells[2],
                "result": cells[3],
                "format": label,
            }
            # International classes (1-3) are supersets of 4-6; keep the most
            # specific label when the same match is listed twice.
            prev = out.get(mid.group(1))
            if prev is None or CLASS_RANK[label] < CLASS_RANK[prev["format"]]:
                out[mid.group(1)] = entry
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-year", type=int, default=2001)
    ap.add_argument("--to-year", type=int, default=date.today().year)
    ap.add_argument("--known-index", default="matches_index.csv")
    ap.add_argument("--known-ids", default=None)
    ap.add_argument("--out-dir", default="missing")
    args = ap.parse_args()

    universe: dict[str, dict] = {}
    for year in range(args.from_year, args.to_year + 1):
        got = scrape_year(year)
        logger.info("%s -> %d matches (universe %d)", year, len(got), len(universe) + len(got))
        universe.update(got)

    known: set[str] = set()
    ki = Path(args.known_index)
    if ki.exists():
        with open(ki, newline="", encoding="utf-8") as f:
            known |= {(r.get("match_id") or "").strip() for r in csv.DictReader(f)}
        known.discard("")
    if args.known_ids and Path(args.known_ids).exists():
        data = json.loads(Path(args.known_ids).read_text())
        known |= {str(x) for x in (data.keys() if isinstance(data, dict) else data)}

    missing = sorted((v for k, v in universe.items() if k not in known),
                     key=lambda x: (x["date"], x["match_id"]))
    extra = sorted(known - set(universe))

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "missing_matches.json").write_text(json.dumps(missing, indent=2), encoding="utf-8")

    by_year = Counter(m["date"][:4] for m in missing)
    by_format = Counter(m["format"] for m in missing)
    with open(out / "missing_by_year.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["year", "missing"])
        for y in sorted(by_year):
            w.writerow([y, by_year[y]])

    summary = {
        "universe": len(universe),
        "known": len(known),
        "missing": len(missing),
        "in_lake_not_in_universe": len(extra),
        "by_format": dict(by_format),
        "by_year": dict(sorted(by_year.items())),
        "scope": f"{args.from_year}-{args.to_year}",
    }
    (out / "missing_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    lines = [
        "# Missing matches",
        "",
        f"Scope: completed matches on ESPNcricinfo, {args.from_year}–{args.to_year}.",
        f"Universe: **{len(universe):,}** · in lake: **{len(known):,}** · "
        f"missing: **{len(missing):,}** ({len(missing)/max(len(universe),1):.1%}).",
        "",
        "## By format",
        "",
        "| Format | Missing |",
        "| --- | --- |",
    ]
    for fmt, n in by_format.most_common():
        lines.append(f"| {fmt} | {n:,} |")
    lines += ["", "## By year", "", "| Year | Missing |", "| --- | --- |"]
    for y in sorted(by_year):
        lines.append(f"| {y} | {by_year[y]:,} |")
    lines += [
        "",
        "Regenerate with `python scripts/audit_missing.py --known-index matches_index.csv`.",
        "Recover with the `Backfill missed matches` workflow, or locally:",
        "`python extract.py --ids-file missing/recovery_queue.json --allow-validation-failures`.",
        "",
    ]
    (out / "MISSING.md").write_text("\n".join(lines), encoding="utf-8")

    logger.info("universe=%d known=%d missing=%d (lake-only=%d)",
                len(universe), len(known), len(missing), len(extra))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
