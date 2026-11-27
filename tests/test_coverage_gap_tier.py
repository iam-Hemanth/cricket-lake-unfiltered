"""A record that is short of the scorecard is incomplete, not contradictory.

ESPN sometimes publishes fewer commentary chunks than the scorecard
describes: the ball-by-ball stops early. The scorecard is the authority for
what the match was, and the commentary is a prefix of it. Such a match is
admitted with the shortfall disclosed, rather than dropped.
"""
import unittest

from helpers import ball, details

from src.builder import References, build_match
from src.validator import assess_match, source_gap_detail


def _short_payload(scorecard_balls=120, scorecard_runs=146, deliveries=1):
    """A payload whose scorecard describes more than the commentary holds."""
    payload = details()
    payload["content"]["innings"][0].update(
        runs=scorecard_runs, wickets=7, balls=scorecard_balls, penalties=0,
        wides=0, noballs=0, byes=0, legbyes=0,
    )
    return payload


class CoverageGapTierTests(unittest.TestCase):
    def test_a_truncated_commentary_is_admitted(self):
        """The commentary stops early; the runs and wickets still agree."""
        payload = _short_payload(scorecard_balls=120, scorecard_runs=0)
        # One delivery of no runs, against a scorecard of 120 balls.
        result = build_match("100", payload, {1: [ball()]}, References())
        self.assertEqual(result.tier, "coverage_gap")
        self.assertTrue(result.is_publishable)

    def test_the_shortfall_is_disclosed(self):
        payload = _short_payload(scorecard_balls=120, scorecard_runs=0)
        result = build_match("100", payload, {1: [ball()]}, References())
        meta = result.match["meta"]
        self.assertEqual(meta["validation_status"], "coverage_gap")
        self.assertTrue(meta.get("validation_warnings"))
        self.assertEqual(meta["source_gap"]["missing_balls"], 119)

    def test_a_record_with_more_balls_than_the_scorecard_is_not_a_gap(self):
        """More deliveries than the scorecard describes is a disagreement."""
        payload = details()
        payload["content"]["innings"][0].update(
            runs=0, wickets=0, balls=1, penalties=0,
            wides=0, noballs=0, byes=0, legbyes=0,
        )
        result = build_match("100", payload, {1: [ball()]}, References())
        self.assertNotEqual(result.tier, "coverage_gap")

    def test_a_complete_match_is_not_a_gap(self):
        payload = details()
        payload["content"]["innings"][0].update(
            runs=0, wickets=0, balls=1, penalties=0,
            wides=0, noballs=0, byes=0, legbyes=0,
        )
        result = build_match("100", payload, {1: [ball()]}, References())
        self.assertEqual(result.tier, "clean")

    def test_the_source_gap_thresholds_are_declared(self):
        from src import validator
        self.assertGreater(validator.SOURCE_GAP_MAX_LEGAL_BALLS, 0)
        self.assertGreater(validator.SOURCE_GAP_MAX_RUNS, 0)
        self.assertGreater(validator.SOURCE_GAP_MAX_WICKETS, 0)

    def test_an_innings_without_deliveries_is_left_out_of_the_delta(self):
        """A missing innings is a coverage gap, not a disagreement."""
        payload = _short_payload(scorecard_balls=120, scorecard_runs=0)
        payload["content"]["innings"].append(dict(payload["content"]["innings"][0]))
        payload["content"]["innings"][1]["inningNumber"] = 2
        result = build_match("100", payload, {1: [ball()]}, References())
        # An innings with no deliveries at all is the more specific case.
        self.assertEqual(result.tier, "scorecard_only")

    def test_the_delta_ignores_an_innings_without_deliveries(self):
        """The measured delta covers the innings that have deliveries."""
        payload = _short_payload(scorecard_balls=120, scorecard_runs=0)
        result = build_match("100", payload, {1: [ball()]}, References())
        detail = source_gap_detail(result.match, [])
        self.assertIsNone(detail)


if __name__ == "__main__":
    unittest.main()
