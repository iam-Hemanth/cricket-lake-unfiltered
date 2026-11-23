import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from src.constructor import construct_match_v3
from src.stage import (
    apply_stage_index_to_dir,
    derive_player_of_match,
    derive_player_of_series,
    derive_series_id,
    derive_stage,
)
from src.validator import validate_inventory_candidate
from helpers import ball, details, legacy_match


class StageDerivationTests(unittest.TestCase):
    def test_espn_title_variants(self):
        cases = [
            ({"title": "Final", "finalType": 1}, ("Final", None, None)),
            ({"title": "2nd Final", "finalType": 1}, ("Final", 2, None)),
            ({"title": "1st Semi-Final", "finalType": 3}, ("Semi Final", 1, None)),
            ({"title": "2nd semi-final", "finalType": 3}, ("Semi Final", 2, None)),
            ({"title": "1st quarter final", "finalType": 4}, ("Quarter Final", 1, None)),
            ({"title": "Qualifier 1", "finalType": 7}, ("Qualifier 1", 1, None)),
            ({"title": "1st Qualifier", "finalType": 7}, ("Qualifier 1", 1, None)),
            ({"title": "Eliminator", "finalType": 8}, ("Eliminator", None, None)),
            ({"title": "3rd place play-off", "finalType": 0}, ("3rd Place Play-off", None, None)),
            ({"title": "Group B", "finalType": 0}, (None, None, "B")),
            ({"title": "8th Match, Group B", "finalType": 0}, (None, 8, "B")),
            ({"title": "7th Match,, Group A", "finalType": 0}, (None, 7, "A")),
            ({"title": "49th Match, Super Eights, Group 2", "finalType": 0}, ("Super Eights", 49, "2")),
            ({"title": "47th Match, Super Eights,Group 1", "finalType": 0}, ("Super Eights", 47, "1")),
            ({"title": "Central & West Group", "finalType": 0}, (None, None, "Central & West")),
            ({"title": "Elite, Group A", "finalType": 0}, (None, None, "Elite A")),
            ({"title": "Plate Group", "finalType": 0}, (None, None, "Plate")),
            ({"title": "22nd Match", "finalType": 0}, (None, 22, None)),
            ({"title": "1st Test", "finalType": 0}, (None, 1, None)),
            ({"title": "Only ODI", "finalType": 0}, (None, 1, None)),
            ({"title": "1st Super", "finalType": 0}, ("Super", 1, None)),
            ({"title": "Preliminary Final", "finalType": 0}, ("Semi Final", None, None)),
            ({"title": "Elimination Final", "finalType": 0}, ("Eliminator", None, None)),
            ({"title": "Qualifying Final", "finalType": 0}, ("Qualifier", None, None)),
            ({"title": "Grand Final", "finalType": 0}, ("Final", None, None)),
            ({"title": "Challenger", "finalType": 0}, ("Challenger", None, None)),
            ({"title": "Knockout", "finalType": 0}, ("Knockout", None, None)),
            ({"title": "", "finalType": 1}, ("Final", None, None)),
            ({"title": None, "finalType": 7}, ("Qualifier", None, None)),
            ({"title": None, "finalType": 0}, (None, None, None)),
        ]
        for match, expected in cases:
            with self.subTest(match=match):
                result = derive_stage(match)
                self.assertEqual(
                    (result["match_stage"], result["match_number"], result["match_group"]),
                    expected,
                )

    def test_raw_title_is_preserved_for_audit(self):
        result = derive_stage({"title": "7th Match,, Group A", "finalType": 0})
        self.assertEqual(result["match_title"], "7th Match,, Group A")

    def test_player_of_match_derivation(self):
        content = {
            "supportInfo": {
                "mostValuedPlayerOfTheMatch": {
                    "player": {"id": 108339, "name": "Maaz Sadaqat"},
                    "team": {"id": 7, "name": "Pakistan"},
                }
            }
        }
        self.assertEqual(
            derive_player_of_match(content),
            {
                "player_of_match_id": 108339,
                "player_of_match_name": "Maaz Sadaqat",
                "player_of_match_team": "Pakistan",
            },
        )

    def test_player_of_series_derivation(self):
        content = {
            "supportInfo": {
                "playersOfTheSeries": [
                    {"type": "PLAYER_OF_TOURNAMENT", "player": {"id": 1, "name": "Skipped"}},
                    {
                        "type": "PLAYER_OF_SERIES",
                        "player": {"id": 71619, "objectId": 647785, "name": "Sahibzada Farhan"},
                        "team": {"id": 7, "name": "Pakistan"},
                    },
                ]
            }
        }
        self.assertEqual(
            derive_player_of_series(content),
            {
                "player_of_series_id": 647785,
                "player_of_series_name": "Sahibzada Farhan",
                "player_of_series_team": "Pakistan",
                "player_of_series_type": "PLAYER_OF_SERIES",
            },
        )

    def test_player_of_series_tournament_type(self):
        content = {
            "supportInfo": {
                "playersOfTheSeries": [
                    {"type": "PLAYER_OF_TOURNAMENT", "player": {"id": 2, "name": "Winner"}}
                ]
            }
        }
        result = derive_player_of_series(content)
        self.assertEqual(result["player_of_series_id"], 2)
        self.assertEqual(result["player_of_series_type"], "PLAYER_OF_TOURNAMENT")
        self.assertIsNone(result["player_of_series_team"])

    def test_player_of_series_absent_or_malformed(self):
        empty = {
            "player_of_series_id": None,
            "player_of_series_name": None,
            "player_of_series_team": None,
            "player_of_series_type": None,
        }
        self.assertEqual(derive_player_of_series(None), empty)
        self.assertEqual(derive_player_of_series({}), empty)
        self.assertEqual(
            derive_player_of_series({"supportInfo": {"playersOfTheSeries": None}}),
            empty,
        )
        self.assertEqual(
            derive_player_of_series({"supportInfo": {"playersOfTheSeries": []}}),
            empty,
        )
        self.assertEqual(
            derive_player_of_series(
                {"supportInfo": {"playersOfTheSeries": [{"type": "OTHER", "player": {"id": 1}}]}}
            ),
            empty,
        )

    def test_series_id_derivation(self):
        self.assertEqual(derive_series_id({"series": {"id": 18514}}), {"source_series_id": 18514})
        self.assertEqual(derive_series_id({"series": {"name": "No id"}}), {"source_series_id": None})
        self.assertEqual(derive_series_id({"series": "text"}), {"source_series_id": None})
        self.assertEqual(derive_series_id(None), {"source_series_id": None})

    def test_player_of_match_prefers_object_id(self):
        content = {
            "supportInfo": {
                "mostValuedPlayerOfTheMatch": {
                    "player": {"id": 7781, "objectId": 35263, "name": "V Sehwag"},
                    "team": {"id": 7, "name": "Daredevils"},
                }
            }
        }
        result = derive_player_of_match(content)
        self.assertEqual(result["player_of_match_id"], 35263)

    def test_player_of_match_absent_or_malformed(self):
        empty = {
            "player_of_match_id": None,
            "player_of_match_name": None,
            "player_of_match_team": None,
        }
        self.assertEqual(derive_player_of_match(None), empty)
        self.assertEqual(derive_player_of_match({}), empty)
        self.assertEqual(derive_player_of_match({"supportInfo": None}), empty)
        self.assertEqual(derive_player_of_match({"supportInfo": {}}), empty)
        self.assertEqual(
            derive_player_of_match({"supportInfo": {"mostValuedPlayerOfTheMatch": None}}),
            empty,
        )
        self.assertEqual(
            derive_player_of_match({"supportInfo": {"mostValuedPlayerOfTheMatch": {"player": {}}}}),
            empty,
        )

    def test_constructor_uses_espn_stage_fields(self):
        raw = details()
        raw["match"].update(title="1st Semi-Final", finalType=3)
        match, error = construct_match_v3("fixture", raw, {1: [ball()]}, {}, {}, {})
        self.assertIsNone(error)
        self.assertEqual(match["meta"]["match_stage"], "Semi Final")
        self.assertEqual(match["meta"]["match_number"], 1)
        self.assertIsNone(match["meta"]["match_group"])
        self.assertEqual(match["meta"]["match_title"], "1st Semi-Final")

    def test_constructor_uses_awards_and_series(self):
        raw = details()
        raw["match"]["series"]["id"] = 18514
        raw["content"]["supportInfo"] = {
            "mostValuedPlayerOfTheMatch": {
                "player": {"id": 108339, "name": "Maaz Sadaqat"},
                "team": {"id": 7, "name": "Pakistan"},
            },
            "playersOfTheSeries": [
                {
                    "type": "PLAYER_OF_SERIES",
                    "player": {"id": 71619, "objectId": 647785, "name": "Sahibzada Farhan"},
                    "team": {"id": 7, "name": "Pakistan"},
                }
            ],
        }
        match, error = construct_match_v3("fixture", raw, {1: [ball()]}, {}, {}, {})
        self.assertIsNone(error)
        meta = match["meta"]
        self.assertEqual(meta["player_of_match_id"], 108339)
        self.assertEqual(meta["player_of_match_name"], "Maaz Sadaqat")
        self.assertEqual(meta["player_of_match_team"], "Pakistan")
        self.assertEqual(meta["player_of_series_id"], 647785)
        self.assertEqual(meta["player_of_series_name"], "Sahibzada Farhan")
        self.assertEqual(meta["player_of_series_team"], "Pakistan")
        self.assertEqual(meta["player_of_series_type"], "PLAYER_OF_SERIES")
        self.assertEqual(meta["source_series_id"], 18514)

    def test_schema_accepts_stage_and_raw_title(self):
        schema = json.loads((Path(__file__).parents[1] / "schema/schema.json").read_text())
        raw = details()
        raw["match"].update(title="49th Match, Super Eights, Group 2", finalType=0)
        match, error = construct_match_v3("fixture", raw, {1: [ball()]}, {}, {}, {})
        self.assertIsNone(error)
        errors = list(Draft202012Validator(schema).iter_errors(match))
        self.assertEqual(errors, [], [item.message for item in errors])


class StageIndexTests(unittest.TestCase):
    def test_index_patch_is_idempotent_and_legacy_safe(self):
        match = legacy_match()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matches_dir = root / "matches"
            matches_dir.mkdir()
            (matches_dir / "100.json").write_text(json.dumps(match), encoding="utf-8")
            index_path = root / "stage_index.json"
            index_path.write_text(
                json.dumps({
                    "matches": {
                        "100": {
                            "match_stage": "Final",
                            "match_number": 2,
                            "match_group": None,
                            "match_title": "2nd Final",
                        }
                    }
                }),
                encoding="utf-8",
            )

            first = apply_stage_index_to_dir(matches_dir, index_path)
            self.assertEqual(first["changed"], 1)
            patched = json.loads((matches_dir / "100.json").read_text())
            self.assertEqual(patched["meta"]["match_stage"], "Final")
            self.assertEqual(patched["meta"]["match_number"], 2)
            self.assertEqual(patched["meta"]["match_title"], "2nd Final")

            second = apply_stage_index_to_dir(matches_dir, index_path)
            self.assertEqual(second["changed"], 0)

            errors = validate_inventory_candidate(patched)
            self.assertNotIn("Legacy version retains modern contract fields", errors)

    def test_index_patch_applies_player_of_match(self):
        match = legacy_match()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            matches_dir = root / "matches"
            matches_dir.mkdir()
            (matches_dir / "100.json").write_text(json.dumps(match), encoding="utf-8")
            index_path = root / "stage_index.json"
            index_path.write_text(
                json.dumps({
                    "matches": {
                        "100": {
                            "match_stage": "Final",
                            "match_number": 1,
                            "match_group": None,
                            "match_title": "Final",
                            "player_of_match_id": 108339,
                            "player_of_match_name": "Maaz Sadaqat",
                            "player_of_match_team": "Pakistan",
                            "player_of_series_id": 71619,
                            "player_of_series_name": "Sahibzada Farhan",
                            "player_of_series_team": "Pakistan",
                            "player_of_series_type": "PLAYER_OF_SERIES",
                            "source_series_id": 18514,
                        }
                    }
                }),
                encoding="utf-8",
            )

            first = apply_stage_index_to_dir(matches_dir, index_path)
            self.assertEqual(first["changed"], 1)
            patched = json.loads((matches_dir / "100.json").read_text())
            self.assertEqual(patched["meta"]["player_of_match_id"], 108339)
            self.assertEqual(patched["meta"]["player_of_match_name"], "Maaz Sadaqat")
            self.assertEqual(patched["meta"]["player_of_match_team"], "Pakistan")
            self.assertEqual(patched["meta"]["player_of_series_id"], 71619)
            self.assertEqual(patched["meta"]["player_of_series_type"], "PLAYER_OF_SERIES")
            self.assertEqual(patched["meta"]["source_series_id"], 18514)

            second = apply_stage_index_to_dir(matches_dir, index_path)
            self.assertEqual(second["changed"], 0)

            errors = validate_inventory_candidate(patched)
            self.assertNotIn("Legacy version retains modern contract fields", errors)

    def test_delta_record_exports_stage_fields(self):
        from scripts.downloads.metadata import record_from_match_json

        raw = details()
        raw["match"].update(title="2nd Final", finalType=1)
        match, error = construct_match_v3("100", raw, {1: [ball()]}, {}, {}, {})
        self.assertIsNone(error)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "100.json"
            path.write_text(json.dumps(match), encoding="utf-8")
            row = record_from_match_json("100", path)
        self.assertEqual(row["match_stage"], "Final")
        self.assertEqual(row["match_number"], 2)
        self.assertEqual(row["match_group"], "")
        self.assertEqual(row["match_title"], "2nd Final")


if __name__ == "__main__":
    unittest.main()
