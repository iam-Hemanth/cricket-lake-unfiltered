"""The completeness evaluator and the maturity state machine."""
import unittest
from datetime import datetime, timedelta, timezone

from helpers import ball, details

from src.builder import References, build_match
from src.completeness import (
    MATURITY_WINDOW_DAYS,
    MAX_REFRESH_ATTEMPTS,
    evaluate_completeness,
)

NOW = datetime(2026, 11, 27, 12, 0, 0, tzinfo=timezone.utc)


def _match_with(**meta_overrides):
    """Build a match and return (match_v3, assessment)."""
    payload = details()
    payload["content"]["innings"][0].update(
        runs=0, wickets=0, balls=1, penalties=0, wides=0, noballs=0, byes=0, legbyes=0
    )
    result = build_match("100", payload, {1: [ball()]}, References(), now_utc=NOW)
    for key, value in meta_overrides.items():
        result.match["meta"][key] = value
    return result.match, result.assessment


class CompletenessTests(unittest.TestCase):
    def test_a_complete_match_settles(self):
        match, assessment = _match_with(
            source_series_id="1234", match_title="Final", player_of_match_id="espn_1001"
        )
        report = evaluate_completeness({}, match, assessment, now_utc=NOW)
        self.assertEqual(report.maturity_state, "settled")
        self.assertFalse(report.needs_refresh)
        self.assertEqual(report.refresh_reasons, [])

    def test_a_missing_series_id_keeps_a_recent_match_provisional(self):
        recent = NOW.date().isoformat()
        match, assessment = _match_with(
            source_series_id=None, match_title="Final",
            player_of_match_id="espn_1001", dates=[recent],
        )
        report = evaluate_completeness({}, match, assessment, now_utc=NOW)
        self.assertEqual(report.maturity_state, "provisional")
        self.assertTrue(report.needs_refresh)
        self.assertIn("missing_series_id", report.refresh_reasons)
        self.assertIsNotNone(report.next_refresh_at)

    def test_a_match_older_than_the_window_settles_incomplete(self):
        old = (NOW - timedelta(days=MATURITY_WINDOW_DAYS + 1)).date().isoformat()
        match, assessment = _match_with(source_series_id=None, dates=[old])
        report = evaluate_completeness({}, match, assessment, now_utc=NOW)
        self.assertEqual(report.maturity_state, "settled_incomplete")
        self.assertTrue(report.gaps)

    def test_an_exhausted_refresh_budget_settles_incomplete(self):
        match, assessment = _match_with(source_series_id=None)
        report = evaluate_completeness(
            {}, match, assessment, now_utc=NOW, refresh_attempts=MAX_REFRESH_ATTEMPTS
        )
        self.assertEqual(report.maturity_state, "settled_incomplete")

    def test_a_failed_match_is_provisional_while_it_is_recent(self):
        match, assessment = _match_with(source_series_id="1234")
        assessment.tier = "failed"
        report = evaluate_completeness({}, match, assessment, now_utc=NOW)
        self.assertIn("validation_failed", report.refresh_reasons)

    def test_a_missing_player_of_the_match_is_not_expected_without_a_result(self):
        match, assessment = _match_with(source_series_id="1234")
        match["outcome"]["result"] = "No result"
        report = evaluate_completeness({}, match, assessment, now_utc=NOW)
        self.assertNotIn("missing_player_of_match", report.refresh_reasons)

    def test_a_missing_player_of_the_match_is_expected_after_a_result(self):
        match, assessment = _match_with(source_series_id="1234")
        match["outcome"]["result"] = "Team A won by 5 wickets"
        report = evaluate_completeness({}, match, assessment, now_utc=NOW)
        self.assertIn("missing_player_of_match", report.refresh_reasons)

    def test_the_match_date_is_read_from_the_metadata(self):
        match, assessment = _match_with(source_series_id="1234", dates=["2026-11-20"])
        report = evaluate_completeness({}, match, assessment, now_utc=NOW)
        self.assertEqual(report.match_date, "2026-11-20")
        self.assertAlmostEqual(report.age_days, 7.5, delta=0.01)

    def test_the_report_is_serialisable(self):
        match, assessment = _match_with(source_series_id=None)
        report = evaluate_completeness({}, match, assessment, now_utc=NOW)
        payload = report.to_dict()
        self.assertEqual(payload["maturity_state"], report.maturity_state)
        self.assertIn("refresh_reasons", payload)


if __name__ == "__main__":
    unittest.main()
