import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.downloads.metadata import (
    CompetitionRegistry,
    TeamRegistry,
    derive_level,
    enrich,
    format_family,
)
from scripts.downloads.slices import build_slices
from src import catalog_db

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
        self.assertIn("played_2d_male_all", ids)
        self.assertNotIn("played_2d_all_all", ids)
        self.assertNotIn("played_2d_female_all", ids)
        assigned_matches = {mid for item in slices for mid in item["ids"]}
        self.assertNotIn("3", assigned_matches)
        self.assertEqual(len(shards), 20)
        assigned = [sid for shard in shards for sid in shard["slice_ids"]]
        self.assertEqual(sorted(assigned), sorted(ids))
        self.assertEqual(len(assigned), len(set(assigned)))


class CatalogAuthorityTests(unittest.TestCase):
    """The catalog replaces the state files the lake used to keep."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.connection = catalog_db.open_catalog(self.root / "lake_catalog.sqlite")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.connection.close)

    def test_a_match_without_a_raw_payload_is_never_published(self):
        """A published match must have evidence behind it, or say so."""
        catalog_db.upsert_match(
            self.connection, "100", raw_available=1,
            raw_archive="raw_shard_17.tar.gz", raw_sha256="a" * 64,
            built_archive="built_shard_17.tar.gz", built_sha256="b" * 64,
            validation_tier="clean", maturity_state="settled",
        )
        catalog_db.upsert_match(
            self.connection, "200", raw_available=0,
            validation_tier="clean", maturity_state="legacy_unbacked",
        )
        self.assertEqual(catalog_db.select_publishable(self.connection), ["100", "200"])
        self.assertEqual(catalog_db.select_legacy_unbacked(self.connection), ["200"])

    def test_a_stranded_match_goes_back_to_provisional(self):
        """A match the catalog knows but that has no build is retried."""
        catalog_db.upsert_match(
            self.connection, "100", validation_tier="failed",
            maturity_state="provisional", next_refresh_at="2026-10-26T00:00:00+00:00",
            last_failure_kind="transient_fetch",
        )
        due = catalog_db.select_due_refreshes(
            self.connection, limit=10, now_utc=datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
        )
        self.assertEqual(due, ["100"])

    def test_a_published_match_is_not_refreshed_again(self):
        """A settled match with no due refresh is left alone."""
        catalog_db.upsert_match(
            self.connection, "100", validation_tier="clean",
            maturity_state="settled", next_refresh_at=None,
        )
        due = catalog_db.select_due_refreshes(
            self.connection, limit=10, now_utc=datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
        )
        self.assertEqual(due, [])


if __name__ == "__main__":
    unittest.main()
