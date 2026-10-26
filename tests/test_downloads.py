import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.downloads.metadata import (
    CompetitionRegistry,
    TeamRegistry,
    derive_level,
    delta_stamp,
    enrich,
    format_family,
)
from scripts.downloads.slices import build_slices
from scripts.reconcile_state import reconcile

ROOT = Path(__file__).resolve().parents[1]


def record(mid, date, gender="male", level="international", family="T20I",
           comp="ipl", team="india", country="India", published="2026-10-24T21:00:00Z"):
    return {
        "match_id": str(mid), "date": date, "gender": gender, "level": level,
        "format_family": family, "competition_id": comp, "competition_name": comp,
        "competition_type": "club", "team_ids": team, "year": date[:4],
        "venue_country": country, "published_at": published,
        "schema_version": "3.3.0", "total_balls": "1", "total_runs": "1",
        "total_wickets": "0",
    }


class MetadataTests(unittest.TestCase):
    def test_level_derived_for_legacy_rows(self):
        self.assertEqual(derive_level({"format_bucket": "Test", "international_status": "unknown"}), "international")
        self.assertEqual(derive_level({"format_bucket": "T20", "international_status": "unknown"}), "domestic")
        self.assertEqual(derive_level({"format_bucket": "T20I", "international_status": "non_international"}), "domestic")

    def test_format_family_maps_ipl_to_t20(self):
        self.assertEqual(format_family({"format_bucket": "IPL"}), "T20")
        self.assertEqual(format_family({"format_bucket": "ListA"}), "List A")

    def test_competition_aliases_and_patterns(self):
        comps = CompetitionRegistry(json.loads((ROOT / "downloads/config/competitions.json").read_text()))
        self.assertEqual(comps.match("IPL")["id"], "ipl")
        self.assertEqual(comps.match("Pepsi Indian Premier League")["id"], "ipl")
        self.assertEqual(comps.match("BPL 2020")["id"], "bpl")
        self.assertEqual(comps.match("LV= County Championship Division Two")["id"], "county_championship")
        self.assertTrue(comps.match("Some Unlisted Cup")["id"].startswith("other_"))

    def test_team_aliases(self):
        teams = TeamRegistry(json.loads((ROOT / "downloads/config/teams.json").read_text()))
        self.assertEqual(teams.match("MI")["id"], "mumbai_indians")
        self.assertEqual(teams.match("U.A.E.")["id"], "uae")
        self.assertIsNone(teams.match("Not a team"))

    def test_enrich_adds_download_fields(self):
        comps = CompetitionRegistry(json.loads((ROOT / "downloads/config/competitions.json").read_text()))
        teams = TeamRegistry(json.loads((ROOT / "downloads/config/teams.json").read_text()))
        row = enrich({"match_id": "1", "date": "2026-01-01", "format_bucket": "T20I",
                      "international_status": "unknown", "gender": "male", "competition": "IPL",
                      "team1": "MI", "team2": "CSK"},
                     comps, teams, "2026-10-24T21:00:00Z")
        self.assertEqual(row["level"], "international")
        self.assertEqual(row["competition_id"], "ipl")
        self.assertEqual(row["team_ids"], "chennai_super_kings|mumbai_indians")
        self.assertEqual(row["year"], 2026)

    def test_delta_stamp_handles_old_and_new_names(self):
        self.assertEqual(delta_stamp("lake_delta_20261025T20.tar.gz").hour, 20)
        self.assertEqual(delta_stamp("lake_delta_20261026T120000Z-123.tar.gz").minute, 0)
        self.assertIsNone(delta_stamp("nope.tar.gz"))


class SliceTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "downloads/config/catalog.json").read_text())
        self.comps = CompetitionRegistry(json.loads((ROOT / "downloads/config/competitions.json").read_text()))
        self.teams = TeamRegistry(json.loads((ROOT / "downloads/config/teams.json").read_text()))

    def test_slices_and_shards(self):
        records = [
            record(1, "2026-10-25", level="international"),
            record(2, "2026-10-25", level="domestic", family="T20", comp="bbl"),
            record(3, "2026-10-20", gender="female", level="international"),
            record(4, "2026-09-01", level="domestic"),
        ]
        slices, shards = build_slices(records, self.comps, self.teams, self.config,
                                      datetime(2026, 10, 26, 12, tzinfo=timezone.utc))
        ids = {item["id"] for item in slices}
        self.assertIn("played_2d_all_all", ids)
        self.assertIn("played_2d_male_all", ids)
        self.assertNotIn("played_2d_female_all", ids)
        self.assertEqual(len(shards), 20)
        assigned = [sid for shard in shards for sid in shard["slice_ids"]]
        self.assertEqual(sorted(assigned), sorted(ids))
        self.assertEqual(len(assigned), len(set(assigned)))


class ReconcileTests(unittest.TestCase):
    def test_stranded_ids_go_back_to_retry(self):
        known, state, summary = reconcile(
            known_prev=["1", "2", "3", "5"],
            state_prev={"retry": {"2": {"attempts": 1}, "4": {"attempts": 1}},
                        "abandoned": {"5": {"attempts": 20}}},
            published={"1", "4", "6"},
            now="2026-10-26T00:00:00Z",
        )
        self.assertEqual(known, {"1", "5"})
        self.assertIn("2", state["retry"])
        self.assertIn("3", state["retry"])
        self.assertNotIn("4", state["retry"])  # published, so retry cleared
        self.assertEqual(summary["stranded"], 2)  # 2 and 3 are new; 5 is abandoned
        self.assertIn("5", state["abandoned"])


if __name__ == "__main__":
    unittest.main()
