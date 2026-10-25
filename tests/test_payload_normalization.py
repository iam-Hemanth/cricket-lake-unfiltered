import gzip
import json
import tempfile
import unittest
from pathlib import Path

from src.commentary import canonical_match_payload, load_raw_match_payloads


class PayloadNormalizationTests(unittest.TestCase):
    def test_flat_payload_is_unchanged(self):
        payload = {"match": {"id": 1}, "content": {"innings": []}}
        self.assertIs(canonical_match_payload(payload), payload)

    def test_nested_payload_is_unwrapped(self):
        inner = {"match": {"id": 1}, "content": {"innings": []}}
        wrapped = {"data": inner, "sponsoredFeatures": {"MATCH_POLLS": {}}}
        self.assertIs(canonical_match_payload(wrapped), inner)

    def test_unrecognised_payload_is_left_alone(self):
        payload = {"data": {"something": "else"}}
        self.assertIs(canonical_match_payload(payload), payload)

    def test_raw_scorecard_loader_normalises_nested_page(self):
        inner = {"match": {"id": 1}, "content": {"innings": []}}
        wrapped = {"data": inner, "sponsoredFeatures": {}}
        with tempfile.TemporaryDirectory() as tmp:
            match_dir = Path(tmp) / "1"
            match_dir.mkdir()
            with gzip.open(match_dir / "scorecard.json.gz", "wt", encoding="utf-8") as f:
                json.dump(wrapped, f)
            details, _, err = load_raw_match_payloads(match_dir)
        self.assertIsNone(err)
        self.assertEqual(details, inner)


if __name__ == "__main__":
    unittest.main()
