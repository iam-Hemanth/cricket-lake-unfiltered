import json
import tempfile
import unittest
from pathlib import Path

from scripts.update_state import collect_succeeded, compute_state


def entry(mid, date="2026-10-24"):
    return {"match_id": str(mid), "date": date, "format": "T20"}


class UpdateStateTests(unittest.TestCase):
    def test_failed_match_is_not_marked_known_and_enters_retry(self):
        known, state, summary = compute_state(
            known_prev=["1"],
            state_prev={},
            full_entries=[entry(2)],
            planned_entries=[entry(2)],
            succeeded=set(),
            failures={"2": "HTTP 400"},
            now="2026-10-26T00:00:00Z",
            stamp="20261026T00",
            max_attempts=5,
        )
        self.assertEqual(known, {"1"})
        self.assertEqual(state["retry"]["2"]["attempts"], 1)
        self.assertEqual(state["retry"]["2"]["last_error"], "HTTP 400")
        self.assertNotIn("2", known)
        self.assertEqual(state["last_delta_count"], 0)
        self.assertEqual(summary["retry"], 1)

    def test_succeeded_match_is_known_and_removed_from_retry(self):
        known, state, _ = compute_state(
            known_prev=["1"],
            state_prev={"retry": {"2": {"attempts": 1, "manifest": entry(2)}}},
            full_entries=[entry(2), entry(3)],
            planned_entries=[entry(2), entry(3)],
            succeeded={"2", "3"},
            failures={},
            now="2026-10-26T00:00:00Z",
            stamp="20261026T06",
            max_attempts=5,
        )
        self.assertEqual(known, {"1", "2", "3"})
        self.assertNotIn("2", state["retry"])
        self.assertEqual(state["last_delta_count"], 2)
        self.assertEqual(state["last_delta_asset"], "20261026T06")

    def test_capped_match_is_deferred_without_burning_attempts(self):
        _, state, summary = compute_state(
            known_prev=[],
            state_prev={},
            full_entries=[entry(1), entry(2)],
            planned_entries=[entry(1)],
            succeeded={"1"},
            failures={},
            now="2026-10-26T00:00:00Z",
            stamp="20261026T00",
            max_attempts=5,
        )
        self.assertEqual(state["retry"]["2"]["attempts"], 0)
        self.assertTrue(state["retry"]["2"]["deferred"])
        self.assertEqual(summary["deferred"], 1)

    def test_retry_is_abandoned_after_max_attempts(self):
        known, state, _ = compute_state(
            known_prev=[],
            state_prev={"retry": {"9": {"attempts": 4, "manifest": entry(9)}}},
            full_entries=[entry(9)],
            planned_entries=[entry(9)],
            succeeded=set(),
            failures={"9": "validation"},
            now="2026-10-26T00:00:00Z",
            stamp="20261026T00",
            max_attempts=5,
        )
        self.assertIn("9", known)
        self.assertNotIn("9", state["retry"])
        self.assertEqual(state["abandoned"]["9"]["attempts"], 5)
        self.assertEqual(state["abandoned"]["9"]["last_error"], "validation")

    def test_validation_defect_gets_shorter_retry_budget_than_live_match(self):
        validation_error = "Validation errors: running score mismatch"
        _, state, _ = compute_state(
            known_prev=[],
            state_prev={"retry": {"9": {"attempts": 4, "manifest": entry(9)}}},
            full_entries=[entry(9)],
            planned_entries=[entry(9)],
            succeeded=set(),
            failures={"9": validation_error},
            now="2026-10-26T00:00:00Z",
            stamp="20261026T00",
            max_attempts=20,
            validation_max_attempts=5,
        )
        self.assertNotIn("9", state["retry"])
        self.assertEqual(state["abandoned"]["9"]["attempt_limit"], 5)

        _, state, _ = compute_state(
            known_prev=[],
            state_prev={"retry": {"10": {"attempts": 5, "manifest": entry(10)}}},
            full_entries=[entry(10)],
            planned_entries=[entry(10)],
            succeeded=set(),
            failures={"10": "Construction error: Match 10: uncompleted source match state LIVE"},
            now="2026-10-26T00:00:00Z",
            stamp="20261026T00",
            max_attempts=20,
            validation_max_attempts=5,
        )
        self.assertIn("10", state["retry"])
        self.assertNotIn("10", state["abandoned"])

    def test_collect_succeeded_ignores_failure_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "100.json").write_text("{}")
            (root / "failed_matches.json").write_text(json.dumps({"101": "x"}))
            self.assertEqual(collect_succeeded(root), {"100"})


if __name__ == "__main__":
    unittest.main()
