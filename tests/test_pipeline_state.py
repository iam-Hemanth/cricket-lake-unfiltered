"""The state machine that used to live in the state files now lives in the catalog.

The lake used to keep `known_ids.json` and `lake_state.json`, and a script
decided which matches were known, which were waiting for another attempt and
which had been abandoned. Those decisions are now the catalog's rows, and
this test pins the behaviour that the old script had.
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src import catalog_db

NOW = datetime(2026, 10, 26, 0, 0, 0, tzinfo=timezone.utc)


class CatalogStateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.connection = catalog_db.open_catalog(self.root / "lake_catalog.sqlite")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.connection.close)

    def _match(self, match_id, **fields):
        payload = {"validation_tier": "failed", "maturity_state": "provisional"}
        payload.update(fields)
        catalog_db.upsert_match(self.connection, match_id, **payload)

    def test_a_failed_fetch_is_retried_and_keeps_its_error(self):
        self._match("100", last_failure_kind="transient_fetch", last_error="HTTP 400")
        row = catalog_db.get_match(self.connection, "100")
        self.assertEqual(row["last_failure_kind"], "transient_fetch")
        self.assertEqual(row["last_error"], "HTTP 400")
        self.assertEqual(row["maturity_state"], "provisional")

    def test_a_successful_fetch_is_settled(self):
        catalog_db.upsert_fetch_and_build(
            self.connection, "100",
            raw_archive="raw_pack_1.tar.gz", raw_sha256="a" * 64,
            built_archive="built_pack_1.tar.gz", built_sha256="b" * 64,
            parser_version="3.3.1-unified", validation_tier="clean",
            maturity_state="settled", now_utc=NOW,
        )
        row = catalog_db.get_match(self.connection, "100")
        self.assertEqual(row["validation_tier"], "clean")
        self.assertEqual(row["maturity_state"], "settled")
        self.assertEqual(row["fetch_attempts"], 1)

    def test_a_match_is_not_known_until_it_has_been_fetched(self):
        """The old failure mode was marking a planned match as known."""
        self._match("100", last_failure_kind="transient_fetch")
        row = catalog_db.get_match(self.connection, "100")
        self.assertEqual(row["raw_available"], 0)
        self.assertIsNone(row["built_archive"])
        self.assertNotEqual(row["validation_tier"], "clean")

    def test_a_capped_match_is_deferred_without_burning_an_attempt(self):
        """A match that did not fit in the run is picked up next time."""
        catalog_db.upsert_match(
            self.connection, "100",
            validation_tier="failed", maturity_state="provisional",
            next_refresh_at=NOW.isoformat(), last_failure_kind="transient_fetch",
            last_error="deferred by max_matches cap", fetch_attempts=0,
        )
        row = catalog_db.get_match(self.connection, "100")
        self.assertEqual(row["fetch_attempts"], 0)
        self.assertEqual(row["last_error"], "deferred by max_matches cap")
        due = catalog_db.select_due_refreshes(self.connection, limit=10, now_utc=NOW)
        self.assertEqual(due, ["100"])

    def test_a_failure_kind_is_never_inferred_from_the_error_text(self):
        """The wording of an error must not decide how it is retried."""
        self._match("100", last_failure_kind="validation_failed",
                    last_error="HTTP 400 timed out")
        row = catalog_db.get_match(self.connection, "100")
        self.assertEqual(row["last_failure_kind"], "validation_failed")
        self._match("200", last_failure_kind="transient_fetch",
                    last_error="Validation errors: running score mismatch")
        row = catalog_db.get_match(self.connection, "200")
        self.assertEqual(row["last_failure_kind"], "transient_fetch")

    def test_the_refresh_budget_is_bounded(self):
        for _ in range(4):
            catalog_db.upsert_fetch_and_build(
                self.connection, "100",
                raw_archive="raw_pack_1.tar.gz", raw_sha256="a" * 64,
                parser_version="3.3.1-unified", now_utc=NOW,
            )
        row = catalog_db.get_match(self.connection, "100")
        self.assertEqual(row["fetch_attempts"], 4)

    def test_the_catalog_reports_its_own_state(self):
        self._match("100", validation_tier="clean", maturity_state="settled")
        self._match("200", validation_tier="failed", maturity_state="provisional")
        report = catalog_db.summary(self.connection)
        self.assertEqual(report["total_matches"], 2)
        self.assertEqual(report["by_validation_tier"]["failed"], 1)
        self.assertEqual(report["by_maturity_state"]["provisional"], 1)


if __name__ == "__main__":
    unittest.main()
