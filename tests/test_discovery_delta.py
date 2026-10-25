import unittest

from scripts.discover_delta import result_index


class DiscoveryCompletionTests(unittest.TestCase):
    def row(self, result):
        return ["Jersey", "Kenya", "Jersey", result, "Kigali", "Oct 25, 2026"]

    def test_completed_match_is_detected(self):
        cells = self.row("Jersey won by 123 runs")
        self.assertEqual(result_index(cells), 3)

    def test_live_match_has_no_result_cell(self):
        cells = ["Sylhet DIV", "Khulna DIV", "-", "Cox's Bazar", "Oct 24, 2026"]
        self.assertIsNone(result_index(cells))

    def test_draw_and_no_result_are_completed(self):
        self.assertIsNotNone(result_index(["A", "B", "-", "Match drawn", "G", "Oct 1, 2026"]))
        self.assertIsNotNone(result_index(["A", "B", "-", "No result", "G", "Oct 1, 2026"]))

    def test_abandoned_without_a_ball_is_not_scheduled(self):
        cells = ["A", "B", "-", "Abandoned without a ball bowled", "G", "Oct 1, 2026"]
        self.assertIsNone(result_index(cells))


if __name__ == "__main__":
    unittest.main()
