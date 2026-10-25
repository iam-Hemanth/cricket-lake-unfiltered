#!/usr/bin/env python3
"""
scripts/discover_delta.py
Discover completed matches on ESPNcricinfo and emit only those that are not
already in the lake.

Differences from scripts/discover_recent_matches.py (which this replaces):

  * no hard-coded absolute paths        -> CI safe
  * no hard-coded year                  -> works after 2026
  * scans every year spanned by --since -> no silent gaps at New Year
  * correct multi-day date parsing      -> "Oct 07-08, 2026" no longer dropped
  * dedupes against a real known-id set  -> idempotent, safe to run 4x/day
  * writes a machine summary             -> easy to gate the workflow on

Output manifest is a list of dicts shaped for `extract.py --manifest`.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from curl_cffi import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("discover")

STATSGURU = (
    "https://stats.espncricinfo.com/ci/engine/records/team/match_results.html"
    "?class={cls};id={year};type=year"
)
LIVE_RESULTS = "https://www.espncricinfo.com/live-cricket-match-results"

# Statsguru class codes -> display label
CLASSES = [
    (1, "Test"),
    (2, "ODI"),
    (3, "T20I"),
    (4, "First-Class"),
    (5, "List A"),
    (6, "T20"),
]

DATE_RANGE_RE = re.compile(r"([A-Za-z]{3})\s+(\d{1,2})(?:\s*-\s*(\d{1,2}))?,\s*(\d{4})")
ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

# Statsguru lists live multi-day matches alongside completed ones; such rows
# have no result text yet. Only schedule matches that actually finished.
RESULT_MARKERS = (
    "won by", "drawn", "tied", "no result", "abandoned", "cancelled",
    "awarded", "forfeit", "forfait", "walkover",
)
NOT_STARTED_MARKERS = ("without a ball bowled",)


def result_index(cells: list[str]) -> int | None:
    """Index of the result/margin cell, or None when the match has not finished."""
    joined = " ".join(c.lower() for c in cells)
    if any(marker in joined for marker in NOT_STARTED_MARKERS):
        return None
    for idx, cell in enumerate(cells):
        if any(marker in cell.lower() for marker in RESULT_MARKERS):
            return idx
    return None


def parse_date(raw: str) -> str | None:
    """Parse Statsguru date cells into YYYY-MM-DD.

    Handles: "Oct 07, 2026", "Oct 07-08, 2026", "2026-10-07", "7 Oct 2026".
    Returns the *start* date for multi-day ranges.
    """
    if not raw:
        return None
    raw = raw.strip().replace("\xa0", " ")

    m = ISO_RE.search(raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    m = DATE_RANGE_RE.search(raw)
    if m:
        month, day, _, year = m.groups()
        for fmt in ("%b %d %Y", "%B %d %Y"):
            try:
                return datetime.strptime(f"{month} {day} {year}", fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue

    for fmt in ("%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def find_date(cells: list[str]) -> str | None:
    """Statsguru column layout is not fixed.

    Rows for matches without a result carry one cell fewer (no margin/ground),
    which shifts the date column. Scanning is the only reliable approach.
    """
    for cell in cells:
        if DATE_RANGE_RE.search(cell) or ISO_RE.search(cell):
            d = parse_date(cell)
            if d:
                return d
    return None


def load_known_ids(index_csv: Path | None, extra: Path | None) -> set[str]:
    """Ids already present in the lake (from the published index and/or a list)."""
    known: set[str] = set()

    if index_csv and index_csv.exists():
        with open(index_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                mid = (row.get("match_id") or "").strip()
                if mid:
                    known.add(mid)
        logger.info("Loaded %d known ids from %s", len(known), index_csv)

    if extra and extra.exists():
        with open(extra, encoding="utf-8") as f:
            data = json.load(f)
        ids = data.keys() if isinstance(data, dict) else [
            x.get("match_id") if isinstance(x, dict) else x for x in data
        ]
        before = len(known)
        known.update(str(i).strip() for i in ids if i)
        logger.info("Loaded %d extra ids from %s", len(known) - before, extra)

    return known


def scrape_statsguru(year: int, since: str) -> dict[str, dict]:
    found: dict[str, dict] = {}

    for cls, label in CLASSES:
        url = STATSGURU.format(cls=cls, year=year)
        try:
            resp = requests.get(url, impersonate="chrome124", timeout=30)
        except Exception as exc:  # network
            logger.warning("class %s (%s): request failed: %s", cls, label, exc)
            continue

        if resp.status_code != 200:
            logger.warning("class %s (%s): HTTP %s", cls, label, resp.status_code)
            continue

        table = re.search(r'<table class="engineTable">(.*?)</table>', resp.text, re.DOTALL)
        if not table:
            logger.warning("class %s (%s): no results table", cls, label)
            continue

        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table.group(1), re.DOTALL)
        n = 0
        unfinished = 0
        for tr in rows[1:]:
            mid_m = re.search(r"/ci/engine/match/(\d+)\.html", tr)
            if not mid_m:
                continue
            cells = [re.sub(r"<[^>]+>", "", c).strip()
                     for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.DOTALL)]
            if len(cells) < 5:
                continue
            d = find_date(cells)
            if not d or d < since:
                continue
            ri = result_index(cells)
            if ri is None:
                unfinished += 1
                continue
            winner = cells[2].strip() if len(cells) > 2 else ""
            found[mid_m.group(1)] = {
                "match_id": mid_m.group(1),
                "teams": [cells[0], cells[1]],
                "winner": None if winner in ("", "-", "—") else winner,
                "result": cells[ri],
                "format": label,
                "season": str(year),
                "date": d,
                "competition": None,
            }
            n += 1
        logger.info("class %s (%s) %s: %d completed matches since %s (%d unfinished skipped)",
                    cls, label, year, n, since, unfinished)

    return found


def scrape_live(since: str) -> dict[str, dict]:
    """Newest fixtures; also fills in the series name the stats page omits."""
    found: dict[str, dict] = {}
    try:
        r = requests.get(LIVE_RESULTS, impersonate="chrome124", timeout=30)
        if r.status_code != 200:
            return found
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text)
        if not m:
            return found
        data = json.loads(m.group(1))
        matches = (data.get("props", {}).get("appPageProps", {})
                   .get("data", {}).get("data", {}).get("content", {}).get("matches", []))
        for match in matches:
            mid = str(match.get("objectId") or "")
            start = (match.get("startDate") or "")[:10]
            status = (match.get("statusText") or "")
            if not mid or not start or start < since:
                continue
            completed = any(w in status.lower() for w in
                            ("won by", "tied", "drawn", "no result", "abandoned"))
            if not completed:
                continue
            series = (match.get("series") or {}).get("longName")
            teams = [t.get("team", {}).get("name") for t in (match.get("teams") or [])]
            entry = found.setdefault(mid, {
                "match_id": mid,
                "teams": teams,
                "format": match.get("format") or "T20",
                "season": start[:4],
                "date": start,
                "winner": None,
                "result": status,
                "competition": series,
            })
            if series:
                entry["competition"] = series
    except Exception as exc:
        logger.warning("live results scrape failed: %s", exc)
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description="Discover new completed matches for the lake")
    ap.add_argument("--since", default=None, help="Earliest match date (YYYY-MM-DD)")
    ap.add_argument("--lookback-days", type=int, default=3,
                    help="Overlap window applied on top of --since (late corrections)")
    ap.add_argument("--known-index", default=None, help="matches_index.csv of the current lake")
    ap.add_argument("--known-ids", default=None, help="JSON list/dict of ids already in the lake")
    ap.add_argument("--output", default="data/manifest_delta.json")
    ap.add_argument("--summary", default=None, help="Path for a small machine-readable summary")
    args = ap.parse_args()

    if args.since:
        since = args.since
    else:
        since = (date.today() - timedelta(days=7)).isoformat()
    since = (datetime.strptime(since, "%Y-%m-%d").date()
             - timedelta(days=args.lookback_days)).isoformat()

    today = date.today()
    years = list(range(int(since[:4]), today.year + 1))

    discovered: dict[str, dict] = {}
    for year in years:
        discovered.update(scrape_statsguru(year, since))
    discovered.update(scrape_live(since))

    known = load_known_ids(Path(args.known_index) if args.known_index else None,
                           Path(args.known_ids) if args.known_ids else None)

    delta = sorted((v for k, v in discovered.items() if k not in known),
                   key=lambda x: (x["date"], x["match_id"]))

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(delta, f, indent=2)

    summary = {
        "since": since,
        "years": years,
        "discovered": len(discovered),
        "already_known": len(discovered) - len(delta),
        "new_matches": len(delta),
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }
    if args.summary:
        Path(args.summary).write_text(json.dumps(summary, indent=2), encoding="utf-8")

    logger.info("discovered=%d known=%d new=%d -> %s",
                summary["discovered"], summary["already_known"],
                summary["new_matches"], out)
    if summary["discovered"] == 0:
        logger.error("Discovery returned zero matches from upstream - treating as failure")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
