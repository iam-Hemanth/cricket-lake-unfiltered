"""The pipeline stages must work together, offline, on a fixture corpus.

The test drives the four stages in the order the workflow runs them: plan,
worker, commit, publish, and then compaction. It never touches the network,
so it can run in CI on every pull request.
"""
import gzip
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from helpers import ball, details

from src import catalog_db, shard_store
from src.commentary import bundle_sha256, save_raw_payload, write_fetch_meta

NOW = datetime(2026, 11, 27, 12, 0, 0, tzinfo=timezone.utc)


def _raw_bundle(raw_dir: Path, match_id: str, payload: dict) -> Path:
    """Store one raw bundle the way the live extractor stores one."""
    match_dir = raw_dir / str(match_id)
    match_dir.mkdir(parents=True, exist_ok=True)
    save_raw_payload(
        match_dir, "scorecard.json",
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        "https://example.invalid/scorecard", 200,
    )
    save_raw_payload(
        match_dir, "commentary_inn1_chunk0.json",
        json.dumps({"comments": [ball()]}).encode("utf-8"),
        "https://example.invalid/comments", 200,
    )
    write_fetch_meta(match_dir, str(match_id))
    return match_dir


class PipelineIntegrationTests(unittest.TestCase):
    """Plan, commit, publish and compact, on three matches."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.raw_dir = self.root / "raw"
        self.raw_dir.mkdir()
        self.payloads = {}
        for match_id in ("100", "101", "102"):
            payload = details()
            payload["content"]["innings"][0].update(
                runs=0, wickets=0, balls=1, penalties=0,
                wides=0, noballs=0, byes=0, legbyes=0,
            )
            self.payloads[match_id] = payload
            _raw_bundle(self.raw_dir, match_id, payload)

    def _build_all(self, dest_dir: Path):
        """Build every fixture match and return their bytes."""
        from src.builder import References, build_match
        from src.commentary import load_raw_match_payloads

        references = References()
        built = {}
        for match_id in self.payloads:
            details_, chunks, error = load_raw_match_payloads(self.raw_dir / match_id)
            self.assertIsNone(error)
            result = build_match(match_id, details_, chunks, references)
            self.assertIsNotNone(result.match, result.error)
            built[match_id] = json.dumps(result.match, indent=2, ensure_ascii=False).encode("utf-8")
        return built

    # -- the stages -------------------------------------------------------

    def test_the_commit_stage_publishes_only_what_changed(self):
        """A second commit with the same payload must publish nothing."""
        from scripts.pipeline_ingest import build_plan

        catalog_path = self.root / "lake_catalog.sqlite.gz"
        connection = catalog_db.open_catalog(self.root / "lake_catalog.sqlite")
        built = self._build_all(self.root)

        # First pass: every match is new.
        built_pack = shard_store.pack_name(shard_store.LAYER_BUILT, "20261127T120000Z")
        raw_pack = shard_store.pack_name(shard_store.LAYER_RAW, "20261127T120000Z")
        shard_store.write_built_pack(self.root / built_pack, built)
        raw_members = {}
        for match_id in self.payloads:
            for path in sorted((self.raw_dir / match_id).glob("*.json.gz")):
                with gzip.open(path, "rb") as handle:
                    raw_members[f"{match_id}/{path.name}"] = handle.read()
        shard_store.write_pack(self.root / raw_pack, shard_store.LAYER_RAW, raw_members)

        for match_id in self.payloads:
            catalog_db.upsert_fetch_and_build(
                connection, match_id,
                raw_archive=raw_pack,
                raw_sha256=bundle_sha256(self.raw_dir / match_id),
                built_archive=built_pack,
                built_sha256=shard_store.member_digest(built[match_id]),
                parser_version="3.3.1-unified",
                schema_version="3.3.0",
                validation_tier="clean",
                maturity_state="settled",
                now_utc=NOW,
            )
        connection.commit()
        self.assertEqual(catalog_db.count_matches(connection), 3)

        # Second pass: the same payload and the same parser version.
        outcome = catalog_db.upsert_fetch_and_build(
            connection, "100",
            raw_archive=raw_pack,
            raw_sha256=bundle_sha256(self.raw_dir / "100"),
            built_archive=built_pack,
            built_sha256=shard_store.member_digest(built["100"]),
            parser_version="3.3.1-unified",
            schema_version="3.3.0",
            validation_tier="clean",
            maturity_state="settled",
            now_utc=NOW,
        )
        self.assertFalse(outcome["rebuild_required"])

    def test_publishing_copies_the_built_matches_unchanged(self):
        """A published match must be the same bytes the builder produced."""
        from scripts.pipeline_publish import materialize_published

        connection = catalog_db.open_catalog(self.root / "lake_catalog.sqlite")
        built = self._build_all(self.root)
        built_pack = shard_store.pack_name(shard_store.LAYER_BUILT, "20261127T120000Z")
        shard_store.write_built_pack(self.root / built_pack, built)

        for match_id in self.payloads:
            catalog_db.upsert_match(
                connection, match_id,
                raw_available=1, raw_archive="raw_pack_1.tar.gz",
                raw_sha256="a" * 64,
                built_archive=built_pack,
                built_sha256=shard_store.member_digest(built[match_id]),
                parser_version="3.3.1-unified",
                schema_version="3.3.0",
                validation_tier="clean",
                maturity_state="settled",
            )
        connection.commit()

        archives = self.root / "archives"
        archives.mkdir()
        shard_store.write_built_pack(archives / built_pack, built)

        out = self.root / "published" / "matches"
        outcome = materialize_published(connection, archives, out)
        self.assertEqual(sorted(outcome["published"]), ["100", "101", "102"])
        for match_id, payload in built.items():
            published = (out / f"{match_id}.json").read_bytes()
            self.assertEqual(published, payload)

    def test_a_failed_match_is_not_published(self):
        from scripts.pipeline_publish import materialize_published

        connection = catalog_db.open_catalog(self.root / "lake_catalog.sqlite")
        built = self._build_all(self.root)
        built_pack = shard_store.pack_name(shard_store.LAYER_BUILT, "20261127T120000Z")
        archives = self.root / "archives"
        archives.mkdir()
        shard_store.write_built_pack(archives / built_pack, built)

        catalog_db.upsert_match(
            connection, "100", built_archive=built_pack,
            built_sha256=shard_store.member_digest(built["100"]),
            validation_tier="clean", maturity_state="settled",
        )
        catalog_db.upsert_match(
            connection, "101", built_archive=built_pack,
            built_sha256=shard_store.member_digest(built["101"]),
            validation_tier="failed", maturity_state="provisional",
        )
        connection.commit()

        out = self.root / "published" / "matches"
        outcome = materialize_published(connection, archives, out)
        self.assertEqual(outcome["published"], ["100"])
        self.assertFalse((out / "101.json").exists())

    def test_the_published_index_and_manifest_describe_the_snapshot(self):
        from scripts.pipeline_publish import write_index, write_manifest

        built = self._build_all(self.root)
        matches_dir = self.root / "published" / "matches"
        matches_dir.mkdir(parents=True)
        for match_id, payload in built.items():
            (matches_dir / f"{match_id}.json").write_bytes(payload)

        rows = write_index(matches_dir, self.root / "matches_index.csv")
        self.assertEqual(rows, 3)
        text = (self.root / "matches_index.csv").read_text(encoding="utf-8")
        self.assertIn("match_id", text.splitlines()[0])
        self.assertIn("validation_status", text.splitlines()[0])

        manifest = write_manifest(matches_dir, self.root / "lake_manifest.json", {})
        self.assertEqual(manifest["total_matches"], 3)
        self.assertEqual(manifest["validation_tiers"].get("clean"), 3)

    def test_compaction_folds_the_packs_into_the_shards(self):
        connection = catalog_db.open_catalog(self.root / "lake_catalog.sqlite")
        built = self._build_all(self.root)
        built_pack = shard_store.pack_name(shard_store.LAYER_BUILT, "20261127T120000Z")
        archives = self.root / "archives"
        archives.mkdir()
        shard_store.write_built_pack(archives / built_pack, built)

        for match_id in self.payloads:
            catalog_db.upsert_match(
                connection, match_id,
                built_archive=built_pack,
                built_sha256=shard_store.member_digest(built[match_id]),
                validation_tier="clean", maturity_state="settled",
            )
        connection.commit()

        outcome = shard_store.compact_shards(
            connection, shard_store.LAYER_BUILT, archives, archives, [built_pack]
        )
        self.assertTrue(outcome["shards_written"])
        for match_id in self.payloads:
            row = catalog_db.get_match(connection, match_id)
            self.assertTrue(row["built_archive"].startswith("built_shard_"))

        # The compacted shard still holds every published match.
        from scripts.pipeline_publish import materialize_published
        out = self.root / "published" / "matches"
        result = materialize_published(connection, archives, out)
        self.assertEqual(sorted(result["published"]), ["100", "101", "102"])

    def test_the_plan_defers_what_does_not_fit(self):
        """A deferred match is named in the plan, not written during planning."""
        from scripts.pipeline_ingest import build_plan

        connection = catalog_db.open_catalog(self.root / "lake_catalog.sqlite")
        plan = build_plan(connection, ["1", "2", "3"], [], [], 2, NOW)
        self.assertEqual(plan["total_selected"], 2)
        self.assertEqual(plan["deferred"], 1)
        self.assertEqual(plan["deferred_ids"], ["3"])
        self.assertIsNone(catalog_db.get_match(connection, "3"))


class IngestCommitTests(unittest.TestCase):
    """The commit step must run, not just import."""

    def test_the_commit_step_writes_the_packs_and_the_catalog(self):
        """The commit step is the one that publishes a fetch.

        It had a call to a function that did not exist, so it would have
        raised on every run and no fetch would ever have reached the lake.
        """
        import argparse
        import json
        import tempfile

        from scripts.pipeline_ingest import run_commit

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            raw_root = work / "raw"
            built_root = work / "built"
            raw_root.mkdir(parents=True)
            built_root.mkdir(parents=True)

            payload = details()
            payload["content"]["innings"][0].update(
                runs=0, wickets=0, balls=1, penalties=0,
                wides=0, noballs=0, byes=0, legbyes=0,
            )
            match_dir = raw_root / "100"
            match_dir.mkdir()
            save_raw_payload(
                match_dir, "scorecard.json",
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "u", 200,
            )
            save_raw_payload(
                match_dir, "commentary_inn1_chunk0.json",
                json.dumps({"comments": [ball()]}).encode("utf-8"), "u", 200,
            )
            write_fetch_meta(match_dir, "100")

            from src.builder import References, build_match
            from src.commentary import load_raw_match_payloads
            details_, chunks, _ = load_raw_match_payloads(match_dir)
            result = build_match("100", details_, chunks, References())
            (built_root / "100.json").write_text(
                json.dumps(result.match, indent=2, ensure_ascii=False), encoding="utf-8"
            )

            (work / "chunk_results_0.json").write_text(json.dumps([{
                "match_id": "100",
                "status": "ok",
                "tier": result.tier,
                "parser_version": "3.3.1-unified",
                "schema_version": "3.3.0",
                "completeness_gaps": [],
                "maturity_state": "settled",
            }]), encoding="utf-8")
            (work / "ingest_plan.json").write_text(json.dumps({
                "stamp": "20261127T120000Z",
                "total_selected": 1,
                "deferred": 0,
                "chunks": [{"chunk": 0, "match_ids": ["100"]}],
            }), encoding="utf-8")

            args = argparse.Namespace(
                workdir=str(work), repo="example/repo", upload=False,
                bootstrap=True,
            )
            self.assertEqual(run_commit(args), 0)

            self.assertTrue((work / "raw_pack_20261127T120000Z.tar.gz").exists())
            self.assertTrue((work / "built_pack_20261127T120000Z.tar.gz").exists())
            catalog_path = work / "lake_catalog.sqlite.gz"
            self.assertTrue(catalog_path.exists())

            connection = catalog_db.load_catalog_gz(catalog_path)
            self.addCleanup(connection.close)
            row = catalog_db.get_match(connection, "100")
            self.assertIsNotNone(row)
            self.assertEqual(row["raw_available"], 1)
            self.assertEqual(row["raw_archive"], "raw_pack_20261127T120000Z.tar.gz")
            self.assertEqual(row["built_archive"], "built_pack_20261127T120000Z.tar.gz")
            self.assertEqual(row["validation_tier"], result.tier)


if __name__ == "__main__":
    unittest.main()
