"""The repaired pipeline, driven offline end to end.

These tests exercise the failures the remediation plan names: a build that
fails must still keep its raw payload, a failed fetch must not demote a
published match, a deferred plan row must not be written during planning, an
offline reparse must update the catalog, and compaction must never prune a
pack the catalog still names.
"""
import argparse
import contextlib
import gzip
import io
import json
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from helpers import ball, details

from src import catalog_db, shard_store
from src.builder import PARSER_VERSION
from src.catalog_fields import CATALOG_FIELDS, INDEX_CATALOG_FIELDS
from src.commentary import FetchBundleError, save_raw_payload, write_fetch_meta

NOW = datetime(2026, 11, 28, 12, 0, 0, tzinfo=timezone.utc)


def _recent_details():
    payload = details()
    payload["match"]["startDate"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    payload["content"]["innings"][0].update(
        runs=0, wickets=0, balls=1, penalties=0,
        wides=0, noballs=0, byes=0, legbyes=0,
    )
    return payload


def _store_bundle(raw_root: Path, match_id: str, payload: dict, commentary=None) -> dict:
    match_dir = raw_root / match_id
    save_raw_payload(
        match_dir, "scorecard.json",
        json.dumps(payload, ensure_ascii=False).encode("utf-8"), "u", 200,
    )
    if commentary is not None:
        save_raw_payload(
            match_dir, "commentary_inn1_chunk0.json",
            json.dumps({"comments": commentary}).encode("utf-8"), "u", 200,
        )
    return write_fetch_meta(match_dir, match_id)


class IngestLifecycleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.work = self.root / "ingest_work"
        self.work.mkdir()

    def _seed_catalog(self):
        connection = catalog_db.open_catalog(self.work / "lake_catalog.sqlite")
        catalog_db.upsert_match(
            connection, "200",
            raw_available=1, raw_archive="raw_pack_existing.tar.gz",
            raw_sha256="a" * 64,
            built_archive="built_pack_existing.tar.gz", built_sha256="b" * 64,
            parser_version=PARSER_VERSION, validation_tier="clean",
            maturity_state="settled",
        )
        catalog_db.save_catalog_gz(connection, self.work / "lake_catalog.sqlite.gz")
        connection.close()

    def _write_plan(self):
        (self.work / "ingest_plan.json").write_text(json.dumps({
            "stamp": "20261128T120000Z",
            "created_at": NOW.isoformat(),
            "total_selected": 4,
            "deferred": 1,
            "deferred_ids": ["300"],
            "refresh_attempts": {"100": 0, "101": 0, "102": 0, "200": 2},
            "chunks": [{"chunk": 0, "match_ids": ["100", "101", "102", "200"]}],
        }), encoding="utf-8")

    def _fake_fetch(self, client, match_id, raw_dir=None, delay_sec=0.5):
        raw_root = Path(raw_dir)
        if match_id == "200":
            raise FetchBundleError("HTTP 503", kind="transient_fetch")
        if match_id == "101":
            payload = _recent_details()
            payload["content"].pop("innings")
            meta = _store_bundle(raw_root, match_id, payload)
            return payload, {}, meta
        if match_id == "102":
            _store_bundle(raw_root, match_id, _recent_details())
            raise FetchBundleError("Commentary inn 1 failed", kind="transient_fetch")
        payload = _recent_details()
        meta = _store_bundle(raw_root, match_id, payload, commentary=[ball()])
        return payload, {1: [ball()]}, meta

    def test_build_and_commit_keep_every_fetch_and_never_demote(self):
        from scripts import pipeline_ingest

        self._seed_catalog()
        self._write_plan()

        worker_args = argparse.Namespace(
            workdir=str(self.work), chunk=0, delay=0.0,
            circuit_breaker_limit=5,
        )
        with mock.patch.object(pipeline_ingest, "fetch_match_bundle", self._fake_fetch), \
                mock.patch.object(pipeline_ingest, "CricinfoClient", lambda **_: None):
            self.assertEqual(pipeline_ingest.run_worker(worker_args), 0)

        commit_args = argparse.Namespace(
            workdir=str(self.work), repo="example/repo",
            upload=False, bootstrap=False,
        )
        self.assertEqual(pipeline_ingest.run_commit(commit_args), 0)

        connection = catalog_db.load_catalog_gz(self.work / "lake_catalog.sqlite.gz")
        self.addCleanup(connection.close)

        built = catalog_db.get_match(connection, "100")
        self.assertEqual(built["validation_tier"], "clean")
        self.assertEqual(built["raw_available"], 1)
        self.assertIsNotNone(built["first_published_at"])
        self.assertIsNotNone(built["built_at"])
        self.assertTrue(built["built_archive"].startswith("built_pack_"))

        failed_build = catalog_db.get_match(connection, "101")
        self.assertEqual(failed_build["raw_available"], 1)
        self.assertEqual(failed_build["validation_tier"], "failed")
        self.assertTrue(failed_build["raw_archive"].startswith("raw_pack_"))

        partial_fetch = catalog_db.get_match(connection, "102")
        self.assertEqual(partial_fetch["raw_available"], 1)
        self.assertEqual(partial_fetch["validation_tier"], "failed")

        transient = catalog_db.get_match(connection, "200")
        self.assertEqual(transient["validation_tier"], "clean")
        self.assertEqual(transient["built_archive"], "built_pack_existing.tar.gz")
        self.assertEqual(transient["raw_sha256"], "a" * 64)
        self.assertEqual(transient["fetch_attempts"], 1)
        self.assertEqual(transient["last_failure_kind"], "transient_fetch")

        deferred = catalog_db.get_match(connection, "300")
        self.assertEqual(deferred["maturity_state"], "provisional")
        self.assertEqual(deferred["validation_tier"], "failed")

        raw_pack = next(self.work.glob("raw_pack_20261128T120000Z.tar.gz"))
        members = shard_store.read_pack(raw_pack)
        self.assertIn("100/scorecard.json.gz", members)
        self.assertIn("100/commentary_inn1_chunk0.json.gz", members)
        self.assertIn("101/scorecard.json.gz", members)
        self.assertIn("102/scorecard.json.gz", members)
        self.assertIn("100/fetch_meta.json", members)

        built_pack = next(self.work.glob("built_pack_20261128T120000Z.tar.gz"))
        built_members = shard_store.read_pack(built_pack)
        self.assertIn("100.json", built_members)
        self.assertNotIn("101.json", built_members)
        self.assertNotIn("102.json", built_members)

        summary = json.loads((self.work / "chunk_results_0.json").read_text(encoding="utf-8"))
        statuses = {item["match_id"]: item["status"] for item in summary}
        self.assertEqual(statuses["101"], "failed")
        self.assertEqual(statuses["102"], "fetch_failed")
        self.assertEqual(statuses["200"], "fetch_failed")

    def test_an_offline_reparse_updates_the_catalog_from_stored_raw(self):
        from scripts import pipeline_reparse

        self._seed_catalog()
        self._write_plan()
        worker_args = argparse.Namespace(
            workdir=str(self.work), chunk=0, delay=0.0,
            circuit_breaker_limit=5,
        )
        from scripts import pipeline_ingest
        with mock.patch.object(pipeline_ingest, "fetch_match_bundle", self._fake_fetch), \
                mock.patch.object(pipeline_ingest, "CricinfoClient", lambda **_: None):
            pipeline_ingest.run_worker(worker_args)
        pipeline_ingest.run_commit(argparse.Namespace(
            workdir=str(self.work), repo="example/repo", upload=False, bootstrap=False,
        ))

        connection = catalog_db.load_catalog_gz(self.work / "lake_catalog.sqlite.gz")
        row = catalog_db.get_match(connection, "100")
        raw_archive = row["raw_archive"]
        old_built_at = row["built_at"]
        catalog_db.upsert_match(
            connection, "100",
            parser_version="0.0.1-old", validation_tier="failed",
        )
        catalog_db.save_catalog_gz(connection, self.work / "lake_catalog.sqlite.gz")
        connection.close()

        archives = self.work / "archives"
        archives.mkdir()
        (archives / raw_archive).write_bytes(
            (self.work / raw_archive).read_bytes()
        )

        with mock.patch.object(
            sys, "argv",
            ["pipeline_reparse.py", "--workdir", str(self.work), "--match-ids", "100"],
        ):
            self.assertEqual(pipeline_reparse.main(), 0)

        connection = catalog_db.load_catalog_gz(self.work / "lake_catalog.sqlite.gz")
        self.addCleanup(connection.close)
        row = catalog_db.get_match(connection, "100")
        self.assertEqual(row["parser_version"], PARSER_VERSION)
        self.assertEqual(row["validation_tier"], "clean")
        self.assertEqual(row["built_at"], old_built_at)
        pack = self.work / row["built_archive"]
        self.assertTrue(pack.exists())
        self.assertIn("100.json", shard_store.read_pack(pack))


class CompactPruneTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.work = self.root / "compact_work"
        self.archives = self.work / "archives"
        self.archives.mkdir(parents=True)

    def _raw_dir(self, match_id: str):
        directory = self.root / "raw" / match_id
        directory.mkdir(parents=True)
        with gzip.open(directory / "scorecard.json.gz", "wb") as handle:
            handle.write(f"scorecard-{match_id}".encode())
        return directory

    def test_pruning_never_deletes_a_pack_the_catalog_still_names(self):
        from scripts import pipeline_compact

        connection = catalog_db.open_catalog(self.work / "lake_catalog.sqlite")
        good_dir = self._raw_dir("100")
        bad_dir = self._raw_dir("101")
        _, good_digests = shard_store.write_raw_pack(
            self.archives / "raw_pack_good.tar.gz", {"100": good_dir},
        )
        _, bad_digests = shard_store.write_raw_pack(
            self.archives / "raw_pack_bad.tar.gz", {"101": bad_dir},
        )
        catalog_db.upsert_match(
            connection, "100", raw_available=1,
            raw_archive="raw_pack_good.tar.gz", raw_sha256=good_digests["100"],
            validation_tier="clean", maturity_state="settled",
        )
        catalog_db.upsert_match(
            connection, "101", raw_available=1,
            raw_archive="raw_pack_bad.tar.gz", raw_sha256="0" * 64,
            validation_tier="clean", maturity_state="settled",
        )
        catalog_db.save_catalog_gz(connection, self.work / "lake_catalog.sqlite.gz")
        connection.close()

        deleted = []
        assets = [
            {"name": "raw_pack_good.tar.gz", "updated_at": "1"},
            {"name": "raw_pack_bad.tar.gz", "updated_at": "1"},
        ]
        with mock.patch("src.release_store.list_assets", return_value=assets), \
                mock.patch("src.release_store.download_asset"), \
                mock.patch("src.release_store.upload_asset"), \
                mock.patch("src.release_store.delete_asset",
                           side_effect=lambda repo, release, name: deleted.append(name) or True), \
                mock.patch.object(sys, "argv", [
                    "pipeline_compact.py", "--workdir", str(self.work),
                    "--layer", "raw", "--force", "--upload", "--prune",
                ]):
            self.assertEqual(pipeline_compact.main(), 0)

        self.assertEqual(deleted, ["raw_pack_good.tar.gz"])
        connection = catalog_db.load_catalog_gz(self.work / "lake_catalog.sqlite.gz")
        self.addCleanup(connection.close)
        self.assertTrue(catalog_db.get_match(connection, "100")["raw_archive"].startswith("raw_shard_"))
        self.assertEqual(
            catalog_db.get_match(connection, "101")["raw_archive"], "raw_pack_bad.tar.gz",
        )


class PublishSnapshotTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_packs_and_legacy_matches_publish_with_the_full_index(self):
        from scripts.pipeline_publish import materialize_published, write_index

        connection = catalog_db.open_catalog(self.root / "lake_catalog.sqlite")
        self.addCleanup(connection.close)

        from src.builder import References, build_match

        payloads = {}
        for match_id in ("100", "101", "102"):
            result = build_match(
                match_id, _recent_details(), {1: [ball()]}, References(),
            )
            self.assertIsNotNone(result.match, result.error)
            payloads[match_id] = json.dumps(
                result.match, indent=2, ensure_ascii=False
            ).encode("utf-8")

        archives = self.root / "archives"
        archives.mkdir()
        _, shard_digests = shard_store.write_built_pack(
            archives / "built_shard_36.tar.gz",
            {"100": payloads["100"], "102": payloads["102"]},
        )
        _, pack_digests = shard_store.write_built_pack(
            archives / "built_pack_one.tar.gz", {"101": payloads["101"]},
        )
        catalog_db.upsert_match(
            connection, "100", built_archive="built_shard_36.tar.gz",
            built_sha256=shard_digests["100"], validation_tier="clean",
            maturity_state="settled", parser_version=PARSER_VERSION,
            first_published_at="2026-11-01T00:00:00+00:00",
            built_at="2026-11-20T00:00:00+00:00",
            index_meta_json=json.dumps({"team1": "A", "team2": "B", "total_balls": 1}),
        )
        catalog_db.upsert_match(
            connection, "101", built_archive="built_pack_one.tar.gz",
            built_sha256=pack_digests["101"], validation_tier="warning",
            maturity_state="settled", parser_version=PARSER_VERSION,
            first_published_at="2026-10-01T00:00:00+00:00",
            built_at="2026-10-02T00:00:00+00:00",
            index_meta_json=json.dumps({"team1": "C", "team2": "D", "total_balls": 1}),
        )
        catalog_db.upsert_match(
            connection, "102", raw_available=0,
            built_archive="built_shard_36.tar.gz",
            built_sha256=shard_digests["102"], validation_tier="clean",
            maturity_state="legacy_unbacked", parser_version=PARSER_VERSION,
            first_published_at="2025-01-01T00:00:00+00:00",
            built_at="2025-01-01T00:00:00+00:00",
            index_meta_json=json.dumps({"team1": "E", "team2": "F", "total_balls": 1}),
        )
        connection.commit()

        matches_dir = self.root / "published"
        outcome = materialize_published(connection, archives, matches_dir)
        self.assertEqual(sorted(outcome["published"]), ["100", "101", "102"])
        self.assertEqual(outcome["missing"], [])
        for match_id, payload in payloads.items():
            self.assertEqual((matches_dir / f"{match_id}.json").read_bytes(), payload)

        rows = catalog_db.export_matches_index_rows(connection)
        count = write_index(
            matches_dir, self.root / "matches_index.csv",
            {str(row["match_id"]): row for row in rows},
        )
        self.assertEqual(count, 3)
        header = (self.root / "matches_index.csv").read_text(encoding="utf-8").splitlines()[0]
        for field in CATALOG_FIELDS + INDEX_CATALOG_FIELDS:
            self.assertIn(field, header.split(","))
        self.assertIn("102", (self.root / "matches_index.csv").read_text(encoding="utf-8"))


class WorkflowSummaryTests(unittest.TestCase):
    """The workflows parse a script's stdout as JSON, so logs must go to stderr."""

    def test_a_dry_run_prints_only_json(self):
        from scripts import pipeline_reparse

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            connection = catalog_db.open_catalog(work / "lake_catalog.sqlite")
            catalog_db.upsert_match(
                connection, "100", raw_available=1,
                raw_archive="raw_shard_17.tar.gz", raw_sha256="a" * 64,
                validation_tier="clean", maturity_state="settled",
            )
            catalog_db.save_catalog_gz(connection, work / "lake_catalog.sqlite.gz")
            connection.close()

            output = io.StringIO()
            with mock.patch.object(
                sys, "argv",
                ["pipeline_reparse.py", "--workdir", str(work),
                 "--match-ids", "100", "--dry-run"],
            ), contextlib.redirect_stdout(output):
                self.assertEqual(pipeline_reparse.main(), 0)

        summary = json.loads(output.getvalue())
        self.assertEqual(summary["would_rebuild"], 1)


class DownloadSlicePackagingTests(unittest.TestCase):
    def test_updated_slice_zip_is_a_superset_of_the_added_slice_zip(self):
        from scripts.downloads import build_shard, prepare
        from src.builder import References, build_match

        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            matches_dir = work / "master_matches"
            matches_dir.mkdir()
            for match_id in ("100", "200", "300"):
                result = build_match(
                    match_id, _recent_details(), {1: [ball()]}, References(),
                )
                self.assertIsNotNone(result.match, result.error)
                (matches_dir / f"{match_id}.json").write_text(
                    json.dumps(result.match, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            master_tar = work / "master.tar.gz"
            with tarfile.open(master_tar, "w:gz") as archive:
                for path in sorted(matches_dir.glob("*.json")):
                    archive.add(path, arcname=path.name)

            index = work / "matches_index.csv"
            index.write_text(
                "match_id,date,format,format_bucket,schema_version,gender,"
                "competition,team1,team2,international_status,validation_status,"
                "first_published_at,built_updated_at\n"
                "100,2026-11-28,T20I,T20I,3.3.0,male,IPL,India,Australia,"
                "international,clean,2026-11-28T00:00:00+00:00,2026-11-28T00:00:00+00:00\n"
                "200,2024-01-01,T20I,T20I,3.3.0,male,IPL,India,Australia,"
                "international,clean,2024-01-01T00:00:00+00:00,2026-11-27T00:00:00+00:00\n"
                "300,2026-11-28,T20I,T20I,3.3.0,female,IPL,India,Australia,"
                "international,clean,2026-11-28T00:00:00+00:00,2026-11-28T00:00:00+00:00\n",
                encoding="utf-8",
            )

            plan_dir = work / "plan"
            with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(
                sys, "argv",
                ["prepare.py", "--master-tar", str(master_tar),
                 "--master-index", str(index),
                 "--config-dir", str(root / "downloads" / "config"),
                 "--workdir", str(work / "download_work"),
                 "--out", str(plan_dir), "--now", "2026-11-28T12:00:00Z"],
            ):
                self.assertEqual(prepare.main(), 0)

            wanted = {"added_30d_male_all", "updated_30d_male_all"}
            shards = json.loads((plan_dir / "shards.json").read_text(encoding="utf-8"))
            targets = {
                item["shard"] for item in shards
                if wanted & set(item["slice_ids"])
            }
            self.assertTrue(targets)
            out = work / "downloads_out"
            for shard in sorted(targets):
                with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(
                    sys, "argv",
                    ["build_shard.py", "--plan-dir", str(plan_dir),
                     "--shard", str(shard), "--master-tar", str(master_tar),
                     "--workdir", str(work / f"shard_{shard}"),
                     "--out", str(out)],
                ):
                    self.assertEqual(build_shard.main(), 0)

            import zipfile
            added = zipfile.ZipFile(out / "zips" / "added_30d_male_all.zip")
            updated = zipfile.ZipFile(out / "zips" / "updated_30d_male_all.zip")
            self.addCleanup(added.close)
            self.addCleanup(updated.close)
            added_names = {
                name for name in added.namelist()
                if name.endswith(".json") and Path(name).stem.isdigit()
            }
            updated_names = {
                name for name in updated.namelist()
                if name.endswith(".json") and Path(name).stem.isdigit()
            }
            self.assertEqual(added_names, {"100.json"})
            self.assertEqual(updated_names, {"100.json", "200.json"})


class UpdatedSliceTests(unittest.TestCase):
    def test_updated_slices_are_a_superset_of_added_slices(self):
        from scripts.downloads.metadata import CompetitionRegistry, TeamRegistry
        from scripts.downloads.slices import build_slices

        root = Path(__file__).resolve().parents[1]
        config = json.loads((root / "downloads/config/catalog.json").read_text())
        comps = CompetitionRegistry(json.loads((root / "downloads/config/competitions.json").read_text()))
        teams = TeamRegistry(json.loads((root / "downloads/config/teams.json").read_text()))
        now = datetime(2026, 11, 28, 12, tzinfo=timezone.utc)
        records = [
            {"match_id": "1", "date": "2026-11-27", "gender": "male", "level": "international",
             "format_family": "T20I", "competition_id": "ipl", "competition_name": "IPL",
             "competition_type": "club", "team_ids": "india", "year": 2026,
             "venue_country": "India", "published_at": "2026-11-27T10:00:00Z",
             "updated_at": "2026-11-27T10:00:00Z", "schema_version": "3.3.0"},
            {"match_id": "2", "date": "2024-01-01", "gender": "male", "level": "international",
             "format_family": "T20I", "competition_id": "ipl", "competition_name": "IPL",
             "competition_type": "club", "team_ids": "india", "year": 2024,
             "venue_country": "India", "published_at": "2024-01-01T10:00:00Z",
             "updated_at": "2026-11-27T12:00:00Z", "schema_version": "3.3.0"},
        ]
        slices, _ = build_slices(records, comps, teams, config, now)
        by_id = {item["id"]: item for item in slices}
        added = set(by_id["added_30d_male_all"]["ids"])
        updated = set(by_id["updated_30d_male_all"]["ids"])
        self.assertEqual(added, {"1"})
        self.assertEqual(updated, {"1", "2"})
        self.assertTrue(added <= updated)


if __name__ == "__main__":
    unittest.main()
