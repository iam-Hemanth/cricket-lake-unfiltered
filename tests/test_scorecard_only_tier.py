"""A match with a scorecard but no ball-by-ball record has its own tier.

Some ESPN matches carry a scorecard but no commentary. The record is not
corrupt: it holds the scorecard and nothing else. Such a match is admitted
with its coverage gap disclosed, because rejecting it would drop a match
ESPN really did publish.
"""
import unittest

from helpers import ball, details

from src.builder import References, build_match
from src.validator import assess_match


def _scorecard_only_payload(runs=159, balls=120, wickets=7):
    """A payload whose scorecard is complete but whose commentary is empty."""
    payload = details()
    payload["content"]["innings"][0].update(
        runs=runs, wickets=wickets, balls=balls, penalties=0,
        wides=0, noballs=0, byes=0, legbyes=0,
    )
    payload["content"]["innings"].append(dict(payload["content"]["innings"][0]))
    payload["content"]["innings"][1]["inningNumber"] = 2
    return payload


class ScorecardOnlyTierTests(unittest.TestCase):
    def test_a_match_without_deliveries_is_scorecard_only(self):
        result = build_match("100", _scorecard_only_payload(), {}, References())
        self.assertEqual(result.tier, "scorecard_only")
        self.assertTrue(result.is_publishable)

    def test_the_coverage_gap_is_disclosed(self):
        result = build_match("100", _scorecard_only_payload(), {}, References())
        meta = result.match["meta"]
        self.assertEqual(meta["validation_status"], "scorecard_only")
        self.assertTrue(meta.get("validation_warnings"))

    def test_a_match_with_deliveries_is_not_scorecard_only(self):
        payload = details()
        payload["content"]["innings"][0].update(
            runs=0, wickets=0, balls=1, penalties=0,
            wides=0, noballs=0, byes=0, legbyes=0,
        )
        result = build_match("100", payload, {1: [ball()]}, References())
        self.assertNotEqual(result.tier, "scorecard_only")

    def test_a_partly_covered_match_is_scorecard_only(self):
        """One innings with deliveries, another without, is still partly missing."""
        payload = _scorecard_only_payload()
        result = build_match("100", payload, {1: [ball()]}, References())
        self.assertEqual(result.tier, "scorecard_only")

    def test_a_match_without_innings_is_not_scorecard_only(self):
        payload = details()
        payload["content"]["innings"] = []
        result = build_match("100", payload, {}, References())
        self.assertNotEqual(result.tier, "scorecard_only")

    def test_a_zero_delivery_innings_does_not_excuse_a_structural_error(self):
        """A contradiction elsewhere still fails, despite the coverage gap."""
        result = build_match("100", _scorecard_only_payload(), {}, References())
        self.assertEqual(result.tier, "scorecard_only")

        # A source/classification contradiction is not a coverage-gap shape.
        result.match["meta"]["format"] = "Test"
        assessment = assess_match(result.match)
        self.assertEqual(assessment.tier, "failed")
        self.assertFalse(assessment.is_publishable)
        self.assertIn("Source classification mismatch: format", assessment.errors)

    def test_the_tier_is_publishable(self):
        assessment = assess_match(
            build_match("100", _scorecard_only_payload(), {}, References()).match
        )
        self.assertTrue(assessment.is_publishable)

    def test_the_catalog_publishes_the_tier(self):
        from src import catalog_db
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            connection = catalog_db.open_catalog(Path(tmp) / "catalog.sqlite")
            catalog_db.upsert_match(
                connection, "100", validation_tier="scorecard_only",
                maturity_state="settled",
            )
            self.assertEqual(catalog_db.select_publishable(connection), ["100"])
            connection.close()


if __name__ == "__main__":
    unittest.main()
