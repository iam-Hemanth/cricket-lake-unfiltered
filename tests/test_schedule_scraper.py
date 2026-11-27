import json
import unittest
from datetime import date

from scripts.scrape_schedule import (
    build_schedule_payload,
    enrich_active_series,
    extract_matches_payload,
    extract_next_data,
    normalize_schedule_match,
)


def _espn_match(
    *,
    object_id: int,
    title: str,
    fmt: str = "T20",
    intl_cls: int | None = 3,
    gen_cls: int | None = 6,
    start_date: str = "2026-11-29T00:00:00.000Z",
    state: str = "PRE",
    stage: str = "SCHEDULED",
    status_text: str = "Match yet to begin",
    winner_team_id: int | None = None,
    series: dict | None = None,
) -> dict:
    return {
        "objectId": object_id,
        "id": object_id,
        "title": title,
        "finalType": 0,
        "format": fmt,
        "internationalClassId": intl_cls,
        "generalClassId": gen_cls,
        "startDate": start_date,
        "endDate": start_date,
        "state": state,
        "stage": stage,
        "statusText": status_text,
        "winnerTeamId": winner_team_id,
        "ground": {"name": "Melbourne Cricket Ground"},
        "teams": [
            {"team": {"id": 2, "name": "Australia"}},
            {"team": {"id": 1, "name": "England"}},
        ],
        "series": series
        if series is not None
        else {
            "id": 18004,
            "objectId": 1528677,
            "slug": "england-in-australia-2026-27",
            "name": "England in Australia",
            "longName": "England tour of Australia",
            "season": "2026/27",
            "startDate": "2026-11-07T00:00:00.000Z",
            "endDate": "2026-12-02T00:00:00.000Z",
        },
    }


class ScheduleScraperTests(unittest.TestCase):
    def test_extract_next_data_and_payload(self):
        payload = {
            "props": {
                "appPageProps": {
                    "data": {
                        "data": {
                            "content": {
                                "matches": [_espn_match(object_id=1528709, title="4th T20I")]
                            }
                        }
                    }
                }
            }
        }
        html = (
            '<html><head><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload)
            + "</script></head></html>"
        )
        parsed = extract_next_data(html)
        matches, series = extract_matches_payload(parsed)
        self.assertIsNone(series)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["objectId"], 1528709)

    def test_separates_tour_warmup_from_official_odi_and_t20i(self):
        items = [
            normalize_schedule_match(
                _espn_match(
                    object_id=1561358,
                    title="Tour Match",
                    fmt="ODI",
                    intl_cls=None,
                    gen_cls=12,
                    start_date="2026-11-07T00:00:00.000Z",
                    state="POST",
                    stage="FINISHED",
                    winner_team_id=1,
                )
            ),
            normalize_schedule_match(
                _espn_match(
                    object_id=1528706,
                    title="1st T20I",
                    start_date="2026-11-21T00:00:00.000Z",
                    state="POST",
                    stage="FINISHED",
                    status_text="Australia won by 3 wickets",
                    winner_team_id=2,
                )
            ),
            normalize_schedule_match(
                _espn_match(
                    object_id=1528709,
                    title="4th T20I",
                    start_date="2026-11-29T00:00:00.000Z",
                    state="PRE",
                    stage="SCHEDULED",
                )
            ),
            # Duplicate with fresher LIVE state should win over PRE
            normalize_schedule_match(
                _espn_match(
                    object_id=1528708,
                    title="3rd T20I",
                    start_date="2026-11-27T00:00:00.000Z",
                    state="PRE",
                    stage="SCHEDULED",
                )
            ),
            normalize_schedule_match(
                _espn_match(
                    object_id=1528708,
                    title="3rd T20I",
                    start_date="2026-11-27T00:00:00.000Z",
                    state="LIVE",
                    stage="RUNNING",
                    status_text="Day 1",
                )
            ),
        ]
        doc = build_schedule_payload(items, generated_at="2026-11-27T10:00:00Z")
        self.assertEqual(doc["version"], "lake_schedule_v1")
        self.assertEqual(doc["counts"]["series_buckets"], 2)
        self.assertEqual(doc["counts"]["matches"], 4)

        by_fmt = {b["format_bucket"]: b for b in doc["series"]}
        self.assertFalse(by_fmt["OtherODM"]["record_stats_eligible"])
        self.assertEqual(by_fmt["OtherODM"]["total_matches"], 1)

        t20i = by_fmt["T20I"]
        self.assertTrue(t20i["record_stats_eligible"])
        self.assertEqual(t20i["source_series_id"], 18004)
        self.assertEqual(t20i["completed_count"], 1)
        self.assertEqual(t20i["live_count"], 1)
        self.assertEqual(t20i["upcoming_count"], 1)
        self.assertEqual([m["match_id"] for m in t20i["matches"]], ["1528706", "1528708", "1528709"])
        self.assertEqual(t20i["matches"][0]["winner_team"], "Australia")
        self.assertEqual(t20i["matches"][1]["state"], "LIVE")

    def test_enrich_active_series_skips_women_and_tolerates_errors(self):
        men = normalize_schedule_match(_espn_match(object_id=1528709, title="4th T20I"))
        women = normalize_schedule_match(
            _espn_match(
                object_id=999999,
                title="1st T20I",
                intl_cls=10,
                gen_cls=17,
                series={
                    "id": 9999,
                    "objectId": 999888,
                    "slug": "women-series",
                    "name": "Women's Series",
                    "season": "2026/27",
                    "endDate": "2026-12-05T00:00:00.000Z",
                },
            )
        )
        calls = []

        def fake_fetch(url):
            calls.append(url)
            return {
                "props": {
                    "appPageProps": {
                        "data": {
                            "series": {
                                "id": 18004,
                                "objectId": 1528677,
                                "slug": "england-in-australia-2026-27",
                                "name": "England in Australia",
                                "season": "2026/27",
                                "endDate": "2026-12-02T00:00:00.000Z",
                            },
                            "content": {
                                "matches": [
                                    _espn_match(
                                        object_id=1528710,
                                        title="5th T20I",
                                        start_date="2026-12-02T00:00:00.000Z",
                                        series=None,
                                    )
                                ]
                            },
                        }
                    }
                }
            }

        enriched, count = enrich_active_series(
            [men, women],
            delay_sec=0,
            reference_date=date(2026, 11, 27),
            fetcher=fake_fetch,
        )
        self.assertEqual(count, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn("england-in-australia-2026-27-1528677", calls[0])
        self.assertEqual(len(enriched), 1)
        self.assertEqual(enriched[0]["source_series_id"], 18004)


if __name__ == "__main__":
    unittest.main()
