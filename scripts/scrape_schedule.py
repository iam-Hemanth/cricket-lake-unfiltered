#!/usr/bin/env python3
"""
scripts/scrape_schedule.py
Scrape upcoming, live, and recent series fixtures from ESPNcricinfo and emit
``series_schedule.json`` (schema ``lake_schedule_v1``) for downstream apps.

Kept strictly separate from ``extract.py`` / ``consolidate.py`` so unplayed or
in-progress matches never enter the ball-by-ball completed-match lake.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from curl_cffi import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src import classification  # noqa: E402
from src.stage import derive_series_id, derive_stage  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("schedule_scraper")

SCHEDULE_URL = "https://www.espncricinfo.com/live-cricket-match-schedule-fixtures"
LIVE_RESULTS_URL = "https://www.espncricinfo.com/live-cricket-match-results"
SERIES_FIXTURES_URL = (
    "https://www.cricinfo.com/series/{slug}-{object_id}/match-schedule-fixtures-and-results"
)

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)
_STATE_PRIORITY = {"POST": 3, "LIVE": 2, "PRE": 1}


def extract_next_data(html: str) -> dict:
    """Extract and parse Next.js ``__NEXT_DATA__`` payload from an ESPN page."""
    match = _NEXT_DATA_RE.search(html or "")
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}


def extract_matches_payload(next_data: dict) -> tuple[list[dict], dict | None]:
    """Unwrap matches list and optional top-level series object from ``__NEXT_DATA__``."""
    app_data = (
        (next_data or {})
        .get("props", {})
        .get("appPageProps", {})
        .get("data", {})
    )
    if not isinstance(app_data, dict):
        return [], None

    series = app_data.get("series") if isinstance(app_data.get("series"), dict) else None
    inner = app_data.get("data") if isinstance(app_data.get("data"), dict) else app_data
    if not series and isinstance(inner.get("series"), dict):
        series = inner.get("series")
    content = inner.get("content") if isinstance(inner.get("content"), dict) else {}
    matches = content.get("matches")
    return ([m for m in matches if isinstance(m, dict)] if isinstance(matches, list) else []), series


def _iso_date(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) >= 10 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", text[:10]):
        return text[:10]
    return None


def _as_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        text = str(value).strip()
        return int(text) if text.isdigit() else None
    except Exception:
        return None


def _clean_text(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def normalize_schedule_match(raw: dict, fallback_series: dict | None = None) -> dict | None:
    """Normalize a raw ESPN fixture/result object into a schedule match record."""
    if not isinstance(raw, dict):
        return None

    mid = _as_int(raw.get("objectId")) or _as_int(raw.get("id"))
    if mid is None:
        return None

    match_for_class = dict(raw)
    raw_series = raw.get("series") if isinstance(raw.get("series"), dict) else None
    series = raw_series or (fallback_series if isinstance(fallback_series, dict) else {})
    if not raw_series and series:
        match_for_class["series"] = series

    match_date = _iso_date(raw.get("startDate"))
    if not match_date:
        return None
    end_date = _iso_date(raw.get("endDate")) or match_date

    class_meta = classification.classify(match_for_class, [])
    stage_meta = derive_stage(raw)
    series_id = derive_series_id({"series": series}).get("source_series_id")

    teams: list[str] = []
    for entry in raw.get("teams") or []:
        if not isinstance(entry, dict):
            continue
        team_obj = entry.get("team")
        if not isinstance(team_obj, dict):
            continue
        name = _clean_text(team_obj.get("name")) or _clean_text(team_obj.get("longName"))
        if name and name not in teams:
            teams.append(name)

    ground = raw.get("ground") if isinstance(raw.get("ground"), dict) else {}
    venue = _clean_text(ground.get("name")) or _clean_text(ground.get("smallName"))

    state = (_clean_text(raw.get("state")) or "").upper() or "PRE"
    stage = (_clean_text(raw.get("stage")) or "").upper() or "SCHEDULED"

    winner_team = None
    winner_team_id = _as_int(raw.get("winnerTeamId"))
    if winner_team_id is not None:
        for entry in raw.get("teams") or []:
            if not isinstance(entry, dict):
                continue
            team_obj = entry.get("team")
            if isinstance(team_obj, dict) and _as_int(team_obj.get("id")) == winner_team_id:
                winner_team = _clean_text(team_obj.get("name")) or _clean_text(team_obj.get("longName"))
                break

    return {
        "match_id": str(mid),
        "title": stage_meta.get("match_title"),
        "match_stage": stage_meta.get("match_stage"),
        "match_number": stage_meta.get("match_number"),
        "date": match_date,
        "end_date": end_date,
        "state": state,
        "stage": stage,
        "status_text": _clean_text(raw.get("statusText")),
        "winner_team": winner_team,
        "teams": teams,
        "venue": venue,
        "format": class_meta["format"],
        "format_bucket": class_meta["format_bucket"],
        "international_status": class_meta["international_status"],
        "record_stats_eligible": bool(class_meta["record_stats_eligible"]),
        "gender": class_meta["gender"],
        "international_class_id": _as_int(raw.get("internationalClassId")),
        "general_class_id": _as_int(raw.get("generalClassId")),
        "source_series_id": series_id,
        "series_object_id": _as_int(series.get("objectId")),
        "series_slug": _clean_text(series.get("slug")),
        "competition": _clean_text(series.get("name")),
        "long_name": _clean_text(series.get("longName")),
        "season": _clean_text(series.get("season")) or _clean_text(raw.get("season")),
        "series_start_date": _iso_date(series.get("startDate")),
        "series_end_date": _iso_date(series.get("endDate")),
    }


def _match_rank(item: dict) -> tuple:
    return (
        _STATE_PRIORITY.get(item.get("state") or "", 0),
        1 if item.get("source_series_id") is not None else 0,
        1 if item.get("series_object_id") is not None else 0,
        1 if item.get("venue") else 0,
        len(item.get("teams") or []),
        1 if item.get("status_text") else 0,
    )


def build_schedule_payload(
    normalized_matches: Iterable[dict],
    *,
    generated_at: str | None = None,
    horizon_days: int = 60,
    window_from: str | None = None,
    window_until: str | None = None,
    enriched_series: int = 0,
) -> dict:
    """Group deduplicated schedule fixtures into ``lake_schedule_v1`` payload."""
    deduped: dict[str, dict] = {}
    for item in normalized_matches:
        if not item or not item.get("match_id"):
            continue
        mid = str(item["match_id"])
        existing = deduped.get(mid)
        if existing is None or _match_rank(item) > _match_rank(existing):
            deduped[mid] = item

    buckets: dict[tuple, dict] = {}
    for item in deduped.values():
        sid = item.get("source_series_id")
        comp = item.get("competition") or ""
        season = item.get("season") or ""
        fmt = item.get("format_bucket") or item.get("format") or "Unknown"
        bucket_key = (
            f"id:{sid}" if sid is not None else f"name:{comp.casefold()}:{season}",
            fmt,
        )
        bucket = buckets.get(bucket_key)
        if bucket is None:
            bucket = {
                "source_series_id": sid,
                "series_object_id": item.get("series_object_id"),
                "series_slug": item.get("series_slug"),
                "competition": item.get("competition"),
                "long_name": item.get("long_name"),
                "season": item.get("season"),
                "format_bucket": fmt,
                "international_status": item.get("international_status"),
                "record_stats_eligible": item.get("record_stats_eligible"),
                "gender": item.get("gender"),
                "series_start_date": item.get("series_start_date"),
                "series_end_date": item.get("series_end_date"),
                "matches": [],
            }
            buckets[bucket_key] = bucket
        else:
            for field in (
                "series_object_id",
                "series_slug",
                "competition",
                "long_name",
                "season",
                "series_start_date",
                "series_end_date",
            ):
                if not bucket.get(field) and item.get(field):
                    bucket[field] = item[field]
            if item.get("record_stats_eligible"):
                bucket["record_stats_eligible"] = True
            if bucket.get("gender") == "unknown" and item.get("gender") in ("male", "female"):
                bucket["gender"] = item["gender"]

        bucket["matches"].append({
            "match_id": item["match_id"],
            "title": item.get("title"),
            "match_stage": item.get("match_stage"),
            "match_number": item.get("match_number"),
            "date": item["date"],
            "end_date": item.get("end_date"),
            "state": item["state"],
            "stage": item["stage"],
            "status_text": item.get("status_text"),
            "winner_team": item.get("winner_team"),
            "teams": item.get("teams") or [],
            "venue": item.get("venue"),
        })

    series_list = list(buckets.values())
    for bucket in series_list:
        bucket["matches"].sort(
            key=lambda m: (
                m["date"],
                m["match_number"] if m.get("match_number") is not None else 10**6,
                int(m["match_id"]) if str(m["match_id"]).isdigit() else 0,
                str(m["match_id"]),
            )
        )
        all_teams: list[str] = []
        for m in bucket["matches"]:
            for t in m["teams"]:
                if t not in all_teams:
                    all_teams.append(t)
        bucket["teams"] = all_teams
        bucket["total_matches"] = len(bucket["matches"])
        bucket["completed_count"] = sum(1 for m in bucket["matches"] if m["state"] == "POST")
        bucket["live_count"] = sum(1 for m in bucket["matches"] if m["state"] == "LIVE")
        bucket["upcoming_count"] = sum(1 for m in bucket["matches"] if m["state"] == "PRE")

    series_list.sort(
        key=lambda b: (
            b["matches"][0]["date"] if b["matches"] else "",
            b.get("source_series_id") or 0,
            b.get("competition") or "",
            b.get("format_bucket") or "",
        )
    )

    return {
        "version": "lake_schedule_v1",
        "generated_at": generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "horizon_days": horizon_days,
        "window": {"from": window_from, "until": window_until},
        "counts": {
            "series_buckets": len(series_list),
            "matches": len(deduped),
            "enriched_series": enriched_series,
        },
        "series": series_list,
    }


def _fetch_next_data(url: str, *, retries: int = 2, timeout: int = 20) -> dict:
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, impersonate="chrome124", timeout=timeout)
            if resp.status_code == 200:
                data = extract_next_data(resp.text)
                if data:
                    return data
            else:
                logger.warning("GET %s returned HTTP %s", url, resp.status_code)
        except Exception as exc:
            logger.warning("GET %s attempt %d failed: %s", url, attempt + 1, exc)
        if attempt < retries:
            time.sleep(0.8 * (attempt + 1))
    return {}


def fetch_schedule_windows(
    *,
    days: int = 60,
    step_days: int = 15,
    delay_sec: float = 0.4,
    start_date: date | None = None,
) -> list[dict]:
    """Fetch upcoming and live fixtures across rolling ~15-day windows."""
    base_day = start_date or datetime.now(timezone.utc).date()
    collected: list[dict] = []
    offsets = list(range(0, max(1, days), max(1, step_days)))
    for idx, offset in enumerate(offsets):
        if offset == 0:
            url = SCHEDULE_URL
        else:
            target = (base_day + timedelta(days=offset)).strftime("%d-%m-%Y")
            url = f"{SCHEDULE_URL}?date={target}"
        next_data = _fetch_next_data(url)
        raw_matches, _ = extract_matches_payload(next_data)
        logger.info("Schedule window +%dd: %d raw matches", offset, len(raw_matches))
        for raw in raw_matches:
            norm = normalize_schedule_match(raw)
            if norm:
                collected.append(norm)
        if idx + 1 < len(offsets) and delay_sec > 0:
            time.sleep(delay_sec)
    return collected


def fetch_live_results() -> list[dict]:
    """Fetch recent results + live matches from ESPN's live results page."""
    next_data = _fetch_next_data(LIVE_RESULTS_URL)
    raw_matches, _ = extract_matches_payload(next_data)
    logger.info("Live results page: %d raw matches", len(raw_matches))
    out: list[dict] = []
    for raw in raw_matches:
        norm = normalize_schedule_match(raw)
        if norm:
            out.append(norm)
    return out


def _select_series_to_enrich(
    matches: list[dict],
    *,
    reference_date: date,
    max_series: int,
) -> list[tuple[int, str]]:
    """Select active, non-women series pages worth enriching for full schedules."""
    cutoff = (reference_date - timedelta(days=7)).isoformat()
    near_horizon = (reference_date + timedelta(days=30)).isoformat()
    candidates: dict[tuple[int, str], tuple[int, int, str]] = {}
    for item in matches:
        obj_id = item.get("series_object_id")
        slug = item.get("series_slug")
        if not obj_id or not slug:
            continue
        if item.get("gender") == "female":
            continue
        comp_lower = (item.get("competition") or "").casefold()
        if "women" in comp_lower:
            continue
        start_date = item.get("series_start_date") or item.get("date") or ""
        end_date = item.get("series_end_date") or item.get("date") or ""
        is_active = item.get("state") in ("PRE", "LIVE") or (end_date >= cutoff)
        if not is_active:
            continue
        is_near_term = 1 if (not start_date or start_date <= near_horizon) else 0
        priority = (
            1 if item.get("international_status") == "international" else 0,
            is_near_term,
            end_date,
        )
        key = (int(obj_id), str(slug))
        if key not in candidates or priority > candidates[key]:
            candidates[key] = priority

    ordered = sorted(candidates.items(), key=lambda kv: kv[1], reverse=True)
    return [key for key, _ in ordered[:max_series]]


def enrich_active_series(
    matches: list[dict],
    *,
    delay_sec: float = 0.4,
    max_series: int = 40,
    reference_date: date | None = None,
    fetcher=_fetch_next_data,
) -> tuple[list[dict], int]:
    """Fetch full per-series fixture lists for active series (best-effort)."""
    ref = reference_date or datetime.now(timezone.utc).date()
    targets = _select_series_to_enrich(matches, reference_date=ref, max_series=max_series)
    enriched: list[dict] = []
    ok_series = 0

    for idx, (obj_id, slug) in enumerate(targets):
        url = SERIES_FIXTURES_URL.format(slug=slug, object_id=obj_id)
        next_data = fetcher(url)
        raw_matches, series_obj = extract_matches_payload(next_data)
        if raw_matches:
            ok_series += 1
            for raw in raw_matches:
                norm = normalize_schedule_match(raw, fallback_series=series_obj)
                if norm:
                    enriched.append(norm)
        if idx + 1 < len(targets) and delay_sec > 0:
            time.sleep(delay_sec)

    logger.info("Enriched %d/%d active series (%d fixture entries)", ok_series, len(targets), len(enriched))
    return enriched, ok_series


def main() -> int:
    ap = argparse.ArgumentParser(description="Scrape ESPNcricinfo series schedules for the lake")
    ap.add_argument("--days", type=int, default=60, help="Forward schedule window in days")
    ap.add_argument("--step-days", type=int, default=15, help="Window step size in days")
    ap.add_argument("--delay", type=float, default=0.4, help="Polite delay between requests (sec)")
    ap.add_argument("--max-series", type=int, default=40, help="Max active series pages to enrich")
    ap.add_argument(
        "--no-enrich-series",
        action="store_true",
        help="Skip per-series fixture enrichment pages",
    )
    ap.add_argument("--output", default="data/series_schedule.json", help="Output schedule JSON")
    ap.add_argument("--summary", default=None, help="Optional summary JSON path")
    args = ap.parse_args()

    today = datetime.now(timezone.utc).date()
    window_matches = fetch_schedule_windows(
        days=args.days,
        step_days=args.step_days,
        delay_sec=args.delay,
        start_date=today,
    )
    live_results = fetch_live_results()
    combined = window_matches + live_results

    if not combined:
        logger.error("Schedule scrape returned zero matches from ESPNcricinfo; aborting without overwriting output")
        return 2

    enriched_count = 0
    if not args.no_enrich_series:
        enriched, enriched_count = enrich_active_series(
            combined,
            delay_sec=args.delay,
            max_series=args.max_series,
            reference_date=today,
        )
        combined.extend(enriched)

    dates = sorted(item["date"] for item in combined if item.get("date"))
    payload = build_schedule_payload(
        combined,
        horizon_days=args.days,
        window_from=dates[0] if dates else today.isoformat(),
        window_until=dates[-1] if dates else (today + timedelta(days=args.days)).isoformat(),
        enriched_series=enriched_count,
    )

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(".tmp.json")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp_path.replace(out_path)

    if args.summary:
        summary_path = Path(args.summary)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(
            json.dumps(
                {
                    "version": payload["version"],
                    "generated_at": payload["generated_at"],
                    "horizon_days": payload["horizon_days"],
                    "window": payload["window"],
                    "counts": payload["counts"],
                },
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )

    logger.info(
        "Wrote %s: %d series buckets, %d unique matches (%d enriched series)",
        out_path,
        payload["counts"]["series_buckets"],
        payload["counts"]["matches"],
        enriched_count,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
