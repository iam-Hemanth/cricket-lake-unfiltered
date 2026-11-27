"""The shard store keeps the catalog and the archives in step."""
import gzip
import json
import tempfile
import unittest
from pathlib import Path

from src import catalog_db, shard_store


class ShardStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.archives = self.root / "archives"
        self.archives.mkdir()
        self.connection = catalog_db.open_catalog(self.root / "lake_catalog.sqlite")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.connection.close)

    # -- sharding ---------------------------------------------------------

    def test_a_numeric_id_lands_on_its_modulus(self):
        self.assertEqual(shard_store.shard_id("100"), 100 % 64)
        self.assertEqual(shard_store.shard_id("1529281"), 1529281 % 64)

    def test_the_shard_of_an_id_is_stable(self):
        self.assertEqual(shard_store.shard_id("1529281"), shard_store.shard_id("1529281"))

    def test_a_non_numeric_id_still_gets_a_shard(self):
        index = shard_store.shard_id("not-a-number")
        self.assertTrue(0 <= index < 64)
        self.assertEqual(index, shard_store.shard_id("not-a-number"))

    def test_the_shards_cover_the_whole_id_space(self):
        shards = {shard_store.shard_id(str(i)) for i in range(0, 640)}
        self.assertEqual(len(shards), 64)

    # -- packing ----------------------------------------------------------

    def test_a_pack_holds_the_members_it_was_given(self):
        path = shard_store.write_pack(
            self.archives / "raw_pack_1.tar.gz", shard_store.LAYER_RAW,
            {"100/scorecard.json.gz": b"payload"},
        )
        self.assertTrue(path.exists())
        members = shard_store.read_pack(path)
        self.assertEqual(members["100/scorecard.json.gz"], b"payload")

    def test_a_pack_is_written_atomically(self):
        target = self.archives / "raw_pack_1.tar.gz"
        shard_store.write_pack(target, shard_store.LAYER_RAW, {"100/scorecard.json.gz": b"x"})
        leftovers = [p.name for p in self.archives.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_a_raw_pack_carries_the_scorecard_digest(self):
        raw_dir = self.root / "raw" / "100"
        raw_dir.mkdir(parents=True)
        with gzip.open(raw_dir / "scorecard.json.gz", "wb") as handle:
            handle.write(b"scorecard")
        path, digests = shard_store.write_raw_pack(
            self.archives / "raw_pack_1.tar.gz", {"100": raw_dir}
        )
        self.assertIn("100", digests)
        self.assertEqual(len(digests["100"]), 64)

    def test_a_built_pack_accepts_an_id_with_or_without_the_extension(self):
        """A key that already ends in .json must not become .json.json."""
        path, digests = shard_store.write_built_pack(
            self.archives / "built_pack_1.tar.gz",
            {"100": b"a", "200.json": b"b"},
        )
        members = shard_store.read_pack(path)
        self.assertEqual(sorted(members), ["100.json", "200.json"])
        self.assertEqual(sorted(digests), ["100", "200"])

    # -- materializing ----------------------------------------------------

    def _seed_raw(self, match_id, payload, archive="raw_pack_1.tar.gz"):
        """Store one match's raw payload and point the catalog at it."""
        members = {f"{match_id}/scorecard.json.gz": payload}
        shard_store.write_pack(self.archives / archive, shard_store.LAYER_RAW, members)
        digest = shard_store.member_digest(payload)
        catalog_db.upsert_match(
            self.connection, match_id,
            raw_available=1, raw_archive=archive, raw_sha256=digest,
            validation_tier="clean", maturity_state="settled",
        )
        return digest

    def _seed_raw_pack(self, match_ids, payloads, archive="raw_pack_1.tar.gz"):
        """Store several matches' raw payloads in one archive."""
        members = {
            f"{match_id}/scorecard.json.gz": payloads[match_id] for match_id in match_ids
        }
        shard_store.write_pack(self.archives / archive, shard_store.LAYER_RAW, members)
        for match_id in match_ids:
            digest = shard_store.member_digest(payloads[match_id])
            catalog_db.upsert_match(
                self.connection, match_id,
                raw_available=1, raw_archive=archive, raw_sha256=digest,
                validation_tier="clean", maturity_state="settled",
            )

    def test_materializing_extracts_the_member_the_catalog_names(self):
        self._seed_raw("100", b"payload-100")
        result = shard_store.materialize_from_catalog(
            self.connection, ["100"], self.archives, self.root / "out", shard_store.LAYER_RAW
        )
        self.assertEqual(result["extracted"], ["100"])
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["mismatched"], [])
        target = self.root / "out" / "100" / "scorecard.json.gz"
        self.assertTrue(target.exists())
        with gzip.open(target, "rb") as handle:
            self.assertEqual(handle.read(), b"payload-100")

    def test_a_stale_duplicate_in_another_pack_is_ignored(self):
        """The catalog decides which archive wins, not the file order."""
        self._seed_raw("100", b"current", archive="raw_pack_2.tar.gz")
        # An older pack holds a different copy of the same match.
        shard_store.write_pack(
            self.archives / "raw_pack_1.tar.gz", shard_store.LAYER_RAW,
            {"100/scorecard.json.gz": b"stale"},
        )
        result = shard_store.materialize_from_catalog(
            self.connection, ["100"], self.archives, self.root / "out", shard_store.LAYER_RAW
        )
        self.assertEqual(result["extracted"], ["100"])
        self.assertEqual(result["archives_opened"], ["raw_pack_2.tar.gz"])
        with gzip.open(self.root / "out" / "100" / "scorecard.json.gz", "rb") as handle:
            self.assertEqual(handle.read(), b"current")

    def test_a_member_whose_digest_does_not_match_is_rejected(self):
        self._seed_raw("100", b"payload")
        # Replace the archive with content that no longer matches the digest.
        shard_store.write_pack(
            self.archives / "raw_pack_1.tar.gz", shard_store.LAYER_RAW,
            {"100/scorecard.json.gz": b"corrupted"},
        )
        result = shard_store.materialize_from_catalog(
            self.connection, ["100"], self.archives, self.root / "out", shard_store.LAYER_RAW
        )
        self.assertEqual(result["extracted"], [])
        self.assertEqual(result["mismatched"], ["100"])

    def test_a_match_without_an_archive_is_reported_as_missing(self):
        catalog_db.upsert_match(
            self.connection, "999", raw_available=1,
            raw_archive="raw_pack_absent.tar.gz", raw_sha256="a" * 64,
            validation_tier="clean", maturity_state="settled",
        )
        result = shard_store.materialize_from_catalog(
            self.connection, ["999"], self.archives, self.root / "out", shard_store.LAYER_RAW
        )
        self.assertEqual(result["missing"], ["999"])

    def test_each_archive_is_opened_once(self):
        self._seed_raw_pack(
            ["100", "101", "102"],
            {"100": b"a-100", "101": b"b-101", "102": b"c-102"},
        )
        result = shard_store.materialize_from_catalog(
            self.connection, ["100", "101", "102"], self.archives,
            self.root / "out", shard_store.LAYER_RAW,
        )
        self.assertEqual(len(result["extracted"]), 3)
        self.assertEqual(result["archive_count"], 1)

    def test_only_the_wanted_matches_are_extracted(self):
        self._seed_raw_pack(
            ["100", "101"], {"100": b"a-100", "101": b"b-101"},
        )
        result = shard_store.materialize_from_catalog(
            self.connection, ["100"], self.archives, self.root / "out", shard_store.LAYER_RAW
        )
        self.assertEqual(result["extracted"], ["100"])
        self.assertFalse((self.root / "out" / "101").exists())

    # -- compaction -------------------------------------------------------

    def test_compaction_folds_a_pack_into_the_shards(self):
        self._seed_raw("100", b"payload-100")
        result = shard_store.compact_shards(
            self.connection, shard_store.LAYER_RAW, self.archives,
            self.root / "shards", ["raw_pack_1.tar.gz"],
        )
        expected = shard_store.shard_name(shard_store.LAYER_RAW, 100 % 64)
        self.assertIn(expected, result["shards_written"])
        self.assertTrue((self.root / "shards" / expected).exists())

    def test_compaction_repoints_the_catalog_at_the_shard(self):
        self._seed_raw("100", b"payload-100")
        shard_store.compact_shards(
            self.connection, shard_store.LAYER_RAW, self.archives,
            self.root / "shards", ["raw_pack_1.tar.gz"],
        )
        row = catalog_db.get_match(self.connection, "100")
        expected = shard_store.shard_name(shard_store.LAYER_RAW, 100 % 64)
        self.assertEqual(row["raw_archive"], expected)

    def test_compaction_keeps_the_authoritative_copy_when_a_pack_is_stale(self):
        """A pack holding an older copy must not overwrite the newest one."""
        # The catalog points at a shard that holds the current copy.
        shard_name = shard_store.shard_name(shard_store.LAYER_RAW, 100 % 64)
        shard_store.write_pack(
            self.archives / shard_name, shard_store.LAYER_RAW,
            {"100/scorecard.json.gz": b"current"},
        )
        catalog_db.upsert_match(
            self.connection, "100", raw_available=1, raw_archive=shard_name,
            raw_sha256=shard_store.member_digest(b"current"),
            validation_tier="clean", maturity_state="settled",
        )
        # A pack holds a stale copy of the same match.
        shard_store.write_pack(
            self.archives / "raw_pack_1.tar.gz", shard_store.LAYER_RAW,
            {"100/scorecard.json.gz": b"stale"},
        )
        shard_store.compact_shards(
            self.connection, shard_store.LAYER_RAW, self.archives,
            self.root / "shards", ["raw_pack_1.tar.gz"],
        )
        members = shard_store.read_pack(
            self.root / "shards" / shard_name
        )
        self.assertEqual(members["100/scorecard.json.gz"], b"current")

    def test_compaction_places_a_match_in_its_own_shard(self):
        self._seed_raw("100", b"payload-100")
        self._seed_raw("164", b"payload-164")
        result = shard_store.compact_shards(
            self.connection, shard_store.LAYER_RAW, self.archives,
            self.root / "shards", ["raw_pack_1.tar.gz"],
        )
        for match_id in ("100", "164"):
            expected = shard_store.shard_name(shard_store.LAYER_RAW, shard_store.shard_id(match_id))
            self.assertIn(expected, result["shards_written"])

    def test_compaction_updates_every_repointed_row(self):
        self._seed_raw("100", b"payload-100")
        self._seed_raw("101", b"payload-101")
        result = shard_store.compact_shards(
            self.connection, shard_store.LAYER_RAW, self.archives,
            self.root / "shards", ["raw_pack_1.tar.gz"],
        )
        self.assertEqual(result["rows_repointed"], 2)
        for match_id in ("100", "101"):
            row = catalog_db.get_match(self.connection, match_id)
            self.assertTrue(row["raw_archive"].startswith("raw_shard_"))


if __name__ == "__main__":
    unittest.main()
