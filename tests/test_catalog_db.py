"""The catalog is the only authority for what the lake contains."""
import gzip
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src import catalog_db
from src.builder import PARSER_VERSION

NOW = datetime(2026, 11, 27, 12, 0, 0, tzinfo=timezone.utc)


class CatalogDbTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.connection = catalog_db.open_catalog(self.root / "lake_catalog.sqlite")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.connection.close)

    def _seed(self, match_id="100", **fields):
        payload = {
            "validation_tier": "clean",
            "maturity_state": "settled",
        }
        payload.update(fields)
        catalog_db.upsert_match(self.connection, match_id, **payload)
        return match_id

    # -- the basics -------------------------------------------------------

    def test_a_new_catalog_is_empty_and_versioned(self):
        self.assertEqual(catalog_db.count_matches(self.connection), 0)
        row = self.connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        self.assertEqual(int(row["value"]), catalog_db.SCHEMA_VERSION)

    def test_a_match_row_round_trips(self):
        self._seed("100", match_date="2026-11-01", format="T20")
        row = catalog_db.get_match(self.connection, "100")
        self.assertEqual(row["match_id"], "100")
        self.assertEqual(row["match_date"], "2026-11-01")
        self.assertEqual(row["format"], "T20")
        self.assertEqual(row["validation_tier"], "clean")

    def test_upsert_updates_only_the_supplied_columns(self):
        self._seed("100", match_date="2026-11-01", format="T20")
        catalog_db.upsert_match(self.connection, "100", format="ODI")
        row = catalog_db.get_match(self.connection, "100")
        self.assertEqual(row["format"], "ODI")
        self.assertEqual(row["match_date"], "2026-11-01")

    # -- the idempotency rule --------------------------------------------

    def test_an_unchanged_payload_does_not_re_emit_the_built_artifact(self):
        catalog_db.upsert_fetch_and_build(
            self.connection, "100",
            raw_archive="raw_pack_1.tar.gz", raw_sha256="a" * 64,
            built_archive="built_pack_1.tar.gz", built_sha256="b" * 64,
            parser_version=PARSER_VERSION, validation_tier="clean",
            maturity_state="settled", now_utc=NOW,
        )
        result = catalog_db.upsert_fetch_and_build(
            self.connection, "100",
            raw_archive="raw_pack_1.tar.gz", raw_sha256="a" * 64,
            built_archive="built_pack_2.tar.gz", built_sha256="c" * 64,
            parser_version=PARSER_VERSION, validation_tier="clean",
            maturity_state="settled", now_utc=NOW,
        )
        self.assertFalse(result["rebuild_required"])
        self.assertTrue(result["raw_unchanged"])
        row = catalog_db.get_match(self.connection, "100")
        self.assertEqual(row["built_sha256"], "b" * 64)
        self.assertEqual(row["built_archive"], "built_pack_1.tar.gz")
        self.assertEqual(row["fetch_attempts"], 2)

    def test_a_changed_payload_triggers_a_rebuild(self):
        catalog_db.upsert_fetch_and_build(
            self.connection, "100",
            raw_archive="raw_pack_1.tar.gz", raw_sha256="a" * 64,
            built_archive="built_pack_1.tar.gz", built_sha256="b" * 64,
            parser_version=PARSER_VERSION, validation_tier="clean", now_utc=NOW,
        )
        result = catalog_db.upsert_fetch_and_build(
            self.connection, "100",
            raw_archive="raw_pack_2.tar.gz", raw_sha256="d" * 64,
            built_archive="built_pack_2.tar.gz", built_sha256="e" * 64,
            parser_version=PARSER_VERSION, validation_tier="warning", now_utc=NOW,
        )
        self.assertTrue(result["rebuild_required"])
        row = catalog_db.get_match(self.connection, "100")
        self.assertEqual(row["built_sha256"], "e" * 64)
        self.assertEqual(row["validation_tier"], "warning")

    def test_a_changed_parser_version_triggers_a_rebuild(self):
        catalog_db.upsert_fetch_and_build(
            self.connection, "100",
            raw_archive="raw_pack_1.tar.gz", raw_sha256="a" * 64,
            built_archive="built_pack_1.tar.gz", built_sha256="b" * 64,
            parser_version="3.3.0-old", validation_tier="clean", now_utc=NOW,
        )
        result = catalog_db.upsert_fetch_and_build(
            self.connection, "100",
            raw_archive="raw_pack_1.tar.gz", raw_sha256="a" * 64,
            built_archive="built_pack_2.tar.gz", built_sha256="f" * 64,
            parser_version=PARSER_VERSION, validation_tier="clean", now_utc=NOW,
        )
        self.assertTrue(result["rebuild_required"])
        self.assertTrue(result["same_parser"] is False)
        row = catalog_db.get_match(self.connection, "100")
        self.assertEqual(row["built_sha256"], "f" * 64)

    def test_an_unchanged_payload_still_moves_the_attempt_counter(self):
        catalog_db.upsert_fetch_and_build(
            self.connection, "100",
            raw_archive="raw_pack_1.tar.gz", raw_sha256="a" * 64,
            parser_version=PARSER_VERSION, now_utc=NOW,
        )
        catalog_db.upsert_fetch_and_build(
            self.connection, "100",
            raw_archive="raw_pack_1.tar.gz", raw_sha256="a" * 64,
            parser_version=PARSER_VERSION, now_utc=NOW,
        )
        row = catalog_db.get_match(self.connection, "100")
        self.assertEqual(row["fetch_attempts"], 2)
        self.assertIsNotNone(row["last_attempt_at"])

    def test_a_fetch_always_records_the_raw_payload(self):
        catalog_db.upsert_fetch_and_build(
            self.connection, "100",
            raw_archive="raw_pack_1.tar.gz", raw_sha256="a" * 64,
            parser_version=PARSER_VERSION, now_utc=NOW,
        )
        row = catalog_db.get_match(self.connection, "100")
        self.assertEqual(row["raw_available"], 1)
        self.assertEqual(row["raw_archive"], "raw_pack_1.tar.gz")
        self.assertEqual(row["raw_sha256"], "a" * 64)

    # -- selection --------------------------------------------------------

    def test_due_refreshes_are_selected_by_time(self):
        past = (NOW - timedelta(hours=1)).isoformat()
        future = (NOW + timedelta(hours=1)).isoformat()
        self._seed("100", maturity_state="provisional", next_refresh_at=past)
        self._seed("200", maturity_state="provisional", next_refresh_at=future)
        self._seed("300", maturity_state="settled", next_refresh_at=past)
        ids = catalog_db.select_due_refreshes(self.connection, limit=10, now_utc=NOW)
        self.assertEqual(ids, ["100"])

    def test_due_refreshes_put_failed_fetches_first(self):
        past = (NOW - timedelta(hours=1)).isoformat()
        self._seed("100", maturity_state="provisional", next_refresh_at=past,
                   validation_tier="clean")
        self._seed("200", maturity_state="provisional", next_refresh_at=past,
                   validation_tier="failed")
        ids = catalog_db.select_due_refreshes(self.connection, limit=10, now_utc=NOW)
        self.assertEqual(ids[0], "200")

    def test_stale_parser_selects_only_matches_with_a_raw_payload(self):
        self._seed("100", raw_available=1, parser_version="old")
        self._seed("200", raw_available=0, parser_version="old")
        self._seed("300", raw_available=1, parser_version=PARSER_VERSION)
        ids = catalog_db.select_stale_parser(self.connection, PARSER_VERSION)
        self.assertEqual(ids, ["100"])

    def test_publishable_matches_exclude_the_failed_tier(self):
        self._seed("100", validation_tier="clean")
        self._seed("200", validation_tier="warning")
        self._seed("300", validation_tier="source_gap")
        self._seed("400", validation_tier="failed")
        self.assertEqual(catalog_db.select_publishable(self.connection),
                         ["100", "200", "300"])

    def test_legacy_unbacked_matches_are_listed(self):
        self._seed("100", raw_available=0, maturity_state="legacy_unbacked")
        self._seed("200", raw_available=1, maturity_state="settled")
        self.assertEqual(catalog_db.select_legacy_unbacked(self.connection), ["100"])

    # -- persistence ------------------------------------------------------

    def test_the_catalog_survives_a_gzip_round_trip(self):
        self._seed("100", match_date="2026-11-01", format="T20",
                   raw_available=1, raw_archive="raw_pack_1.tar.gz", raw_sha256="a" * 64)
        path = catalog_db.save_catalog_gz(self.connection, self.root / "lake_catalog.sqlite.gz")
        self.assertTrue(path.exists())
        restored = catalog_db.load_catalog_gz(path)
        self.addCleanup(restored.close)
        row = catalog_db.get_match(restored, "100")
        self.assertEqual(row["match_date"], "2026-11-01")
        self.assertEqual(row["raw_sha256"], "a" * 64)
        self.assertEqual(catalog_db.count_matches(restored), 1)

    def test_the_gzip_write_is_atomic(self):
        target = self.root / "lake_catalog.sqlite.gz"
        catalog_db.save_catalog_gz(self.connection, target)
        before = target.read_bytes()
        self._seed("200")
        catalog_db.save_catalog_gz(self.connection, target)
        self.assertNotEqual(target.read_bytes(), before)
        # No temporary file is left behind.
        leftovers = [p.name for p in self.root.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    # -- reporting --------------------------------------------------------

    def test_the_summary_counts_by_tier_and_maturity(self):
        self._seed("100", validation_tier="clean", maturity_state="settled")
        self._seed("200", validation_tier="warning", maturity_state="provisional")
        report = catalog_db.summary(self.connection)
        self.assertEqual(report["total_matches"], 2)
        self.assertEqual(report["by_validation_tier"]["clean"], 1)
        self.assertEqual(report["by_validation_tier"]["warning"], 1)
        self.assertEqual(report["by_maturity_state"]["provisional"], 1)

    def test_the_index_rows_come_from_the_catalog(self):
        self._seed("100", match_date="2026-11-01", format="T20",
                   validation_tier="clean", index_meta_json=json.dumps({"total_balls": 120}))
        rows = catalog_db.export_matches_index_rows(self.connection)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["match_id"], "100")
        self.assertEqual(rows[0]["validation_status"], "clean")
        self.assertEqual(rows[0]["total_balls"], 120)

    def test_a_run_is_recorded(self):
        catalog_db.record_run(self.connection, "run-1", "ingest", summary={"matches": 3})
        row = self.connection.execute("SELECT * FROM runs WHERE run_id='run-1'").fetchone()
        self.assertEqual(row["workflow"], "ingest")
        self.assertEqual(json.loads(row["summary_json"])["matches"], 3)


if __name__ == "__main__":
    unittest.main()
