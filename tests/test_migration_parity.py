"""The migration must not lose a match the published lake holds.

The migration rebuilds every match from the raw payloads, which applies the
full 3.3.0 contract to matches the old pipeline never validated. Some of
those turn out to be genuinely defective and are correctly left out; the
test that matters is that no match the published lake holds disappears
without a reason.
"""
import gzip
import json
import tempfile
import unittest
from pathlib import Path

from helpers import ball, details

from src import catalog_db, shard_store
from src.builder import References, build_match
from src.commentary import save_raw_payload, write_fetch_meta


class MigrationParityTests(unittest.TestCase):
    """A rebuilt match keeps the tier the published lake gave it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.references = References()

    def _rebuild(self, match_id, payload, commentary):
        raw_dir = self.root / "raw" / str(match_id)
        raw_dir.mkdir(parents=True)
        save_raw_payload(
            raw_dir, "scorecard.json",
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "https://example.invalid/scorecard", 200,
        )
        for key, comments in commentary.items():
            name = (f"commentary_super{key[1]}_chunk0.json" if isinstance(key, tuple)
                    else f"commentary_inn{key}_chunk0.json")
            save_raw_payload(
                raw_dir, name,
                json.dumps({"comments": comments}).encode("utf-8"),
                "https://example.invalid/comments", 200,
            )
        write_fetch_meta(raw_dir, str(match_id))
        result = build_match(match_id, payload, commentary, self.references)
        self.assertIsNotNone(result.match, result.error)
        return result

    def test_a_clean_match_stays_clean(self):
        payload = details()
        payload["content"]["innings"][0].update(
            runs=0, wickets=0, balls=1, penalties=0,
            wides=0, noballs=0, byes=0, legbyes=0,
        )
        result = self._rebuild("100", payload, {1: [ball()]})
        self.assertEqual(result.tier, "clean")

    def test_a_scorecard_only_match_is_admitted(self):
        """A match ESPN published without commentary is not dropped."""
        payload = details()
        payload["content"]["innings"][0].update(
            runs=159, wickets=7, balls=120, penalties=0,
            wides=9, noballs=0, byes=2, legbyes=0,
        )
        result = self._rebuild("100", payload, {})
        self.assertEqual(result.tier, "scorecard_only")
        self.assertTrue(result.is_publishable)

    def test_a_contradiction_is_still_rejected(self):
        """A supplied non-striker id that cannot be resolved is a defect."""
        payload = details()
        payload["content"]["innings"][0].update(
            runs=0, wickets=0, balls=1, penalties=0,
            wides=0, noballs=0, byes=0, legbyes=0,
        )
        # The ball names a non-striker that is not in the roster, so the id
        # was supplied but cannot be resolved: a contradiction, not a gap.
        commentary = dict(ball())
        commentary["nonStrikerPlayerId"] = 999999
        result = self._rebuild("100", payload, {1: [commentary]})
        self.assertNotEqual(result.tier, "clean")

    def test_the_rebuilt_match_and_the_catalog_agree(self):
        payload = details()
        payload["content"]["innings"][0].update(
            runs=0, wickets=0, balls=1, penalties=0,
            wides=0, noballs=0, byes=0, legbyes=0,
        )
        result = self._rebuild("100", payload, {1: [ball()]})

        connection = catalog_db.open_catalog(self.root / "catalog.sqlite")
        self.addCleanup(connection.close)
        catalog_db.upsert_fetch_and_build(
            connection, "100",
            raw_archive="raw_shard_36.tar.gz", raw_sha256="a" * 64,
            built_archive="built_shard_36.tar.gz",
            built_sha256=shard_store.member_digest(
                json.dumps(result.match, indent=2, ensure_ascii=False).encode("utf-8")),
            parser_version="3.3.1-unified",
            validation_tier=result.tier,
            maturity_state="settled",
        )
        row = catalog_db.get_match(connection, "100")
        self.assertEqual(row["validation_tier"], result.tier)
        self.assertEqual(row["parser_version"], "3.3.1-unified")

    def test_a_scorecard_only_match_is_publishable_in_the_catalog(self):
        payload = details()
        payload["content"]["innings"][0].update(
            runs=159, wickets=7, balls=120, penalties=0,
            wides=9, noballs=0, byes=2, legbyes=0,
        )
        result = self._rebuild("100", payload, {})
        connection = catalog_db.open_catalog(self.root / "catalog.sqlite")
        self.addCleanup(connection.close)
        catalog_db.upsert_match(
            connection, "100", validation_tier=result.tier, maturity_state="settled",
        )
        self.assertEqual(catalog_db.select_publishable(connection), ["100"])


if __name__ == "__main__":
    unittest.main()
