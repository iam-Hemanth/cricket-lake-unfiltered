"""The live extractor and the offline re-parser must agree exactly.

A match is fetched once, stored raw, and then rebuilt from those stored
payloads. If the two paths disagree, a parser change silently produces a
different lake depending on which path ran. This test pins that agreement
for a clean match, a Super Over tiebreaker, a hundred match, a warn-tier
match and a source-gap match.
"""
import gzip
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import ball, details, player

from src.builder import PARSER_VERSION, References, build_match
from src.commentary import (
    bundle_sha256,
    canonical_match_payload,
    load_raw_match_payloads,
    save_raw_payload,
    write_fetch_meta,
)


def _write_raw(raw_dir: Path, match_id: str, payload: dict,
               commentary: dict) -> Path:
    """Store a raw bundle the way the live extractor stores one."""
    match_dir = raw_dir / str(match_id)
    match_dir.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    save_raw_payload(match_dir, "scorecard.json", body, "https://example.invalid/scorecard", 200)
    for key, comments in commentary.items():
        if isinstance(key, tuple):
            prefix, index = key
            name = f"commentary_super{index}_chunk0.json"
        else:
            name = f"commentary_inn{key}_chunk0.json"
        chunk = json.dumps({"comments": comments}).encode("utf-8")
        save_raw_payload(match_dir, name, chunk, "https://example.invalid/comments", 200)
    write_fetch_meta(match_dir, str(match_id))
    return match_dir


def _canonical(match: dict) -> str:
    return json.dumps(match, sort_keys=True, ensure_ascii=False)


class BuilderParityTests(unittest.TestCase):
    """The extract path and the reparse path must produce the same bytes."""

    def setUp(self):
        self.references = References()

    def _assert_paths_agree(self, match_id, payload, commentary):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_dir = root / "raw"

            # The live path: the payload is fetched and stored, then built.
            _write_raw(raw_dir, match_id, payload, commentary)
            live = build_match(match_id, payload, commentary, self.references)

            # The offline path: the stored payload is read back and built.
            match_dir = raw_dir / str(match_id)
            loaded, chunks, error = load_raw_match_payloads(match_dir)
            self.assertIsNone(error)
            offline = build_match(match_id, loaded, chunks, self.references)

            self.assertIsNotNone(live.match)
            self.assertIsNotNone(offline.match)
            self.assertEqual(live.tier, offline.tier)
            self.assertEqual(_canonical(live.match), _canonical(offline.match))

    def test_clean_match_agrees(self):
        payload = details()
        payload["content"]["innings"][0].update(
            runs=0, wickets=0, balls=1, penalties=0, wides=0, noballs=0, byes=0, legbyes=0
        )
        self._assert_paths_agree("100", payload, {1: [ball()]})

    def test_super_over_tiebreaker_agrees(self):
        payload = details()
        payload["content"]["innings"][0].update(
            runs=1, wickets=0, balls=1, penalties=0, wides=0, noballs=0, byes=0, legbyes=0
        )
        super_inn = {
            "inningNumber": 3,
            "team": {"id": 20, "name": "B"},
            "runs": 12, "wickets": 1, "balls": 8,
            "inningBatsmen": [{"player": player(12), "runs": 4, "balls": 3}],
            "inningBowlers": [{"player": player(1), "balls": 4}],
        }
        payload["content"]["superOverInnings"] = [super_inn]
        super_ball = dict(ball())
        super_ball.update(inningNumber=3, id=900, oversActual=0.1, oversUnique=0.1)
        self._assert_paths_agree("200", payload, {1: [ball()], ("super", 0): [super_ball]})

    def test_hundred_match_agrees(self):
        payload = details()
        payload["match"]["generalClassId"] = 100
        payload["match"]["internationalClassId"] = 100
        payload["match"]["format"] = "T20"
        payload["content"]["innings"][0].update(
            ballsPerOver=5, runs=0, wickets=0, balls=1,
            penalties=0, wides=0, noballs=0, byes=0, legbyes=0,
        )
        hundred_ball = dict(ball())
        hundred_ball.update(overNumber=1, oversActual=0.1, oversUnique=0.1)
        self._assert_paths_agree("300", payload, {1: [hundred_ball]})

    def test_warn_tier_match_agrees(self):
        """A running counter that disagrees with the scorecard stays warn tier."""
        payload = details()
        payload["content"]["innings"][0].update(
            runs=2, wickets=0, balls=1, penalties=0, wides=0, noballs=0, byes=0, legbyes=0
        )
        # The innings total agrees with the scorecard, but the running counter
        # ESPN supplied for the ball does not.
        drift = dict(ball())
        drift.update(batsmanRuns=2, totalRuns=2, totalInningRuns=9)
        result = build_match("400", payload, {1: [drift]}, self.references)
        self.assertEqual(result.tier, "warning")
        self._assert_paths_agree("400", payload, {1: [drift]})

    def test_source_gap_match_agrees(self):
        """A small disagreement with the scorecard stays source gap."""
        payload = details()
        payload["content"]["innings"][0].update(
            runs=1, wickets=0, balls=1, penalties=0, wides=0, noballs=0, byes=0, legbyes=0
        )
        # The scorecard claims one wicket the ball-by-ball does not have.
        payload["content"]["innings"][0]["wickets"] = 1
        result = build_match("500", payload, {1: [ball()]}, self.references)
        self.assertIn(result.tier, ("source_gap", "failed"))
        self._assert_paths_agree("500", payload, {1: [ball()]})


class BuilderContractTests(unittest.TestCase):
    """The builder's own contract."""

    def test_parser_version_is_recorded_on_every_match(self):
        result = build_match("100", details(), {1: [ball()]}, References())
        self.assertEqual(result.match["meta"]["parser_version"], PARSER_VERSION)

    def test_validation_status_is_assigned_in_one_pass(self):
        result = build_match("100", details(), {1: [ball()]}, References())
        self.assertEqual(result.match["meta"]["validation_status"], result.tier)

    def test_construction_failure_is_reported_not_raised(self):
        payload = details()
        payload.pop("content")
        result = build_match("100", payload, {}, References())
        self.assertIsNone(result.match)
        self.assertEqual(result.tier, "failed")
        self.assertIsNotNone(result.error)

    def test_incomplete_match_is_a_distinct_failure_kind(self):
        payload = details()
        payload["match"]["state"] = "PRE"
        result = build_match("100", payload, {1: [ball()]}, References())
        self.assertIsNone(result.match)
        self.assertEqual(result.failure_kind, "uncompleted_match")

    def test_completeness_is_attached_to_the_result(self):
        result = build_match("100", details(), {1: [ball()]}, References())
        self.assertIsNotNone(result.completeness)
        self.assertIn(result.completeness.maturity_state,
                      ("provisional", "settled", "settled_incomplete"))

    def test_build_is_deterministic(self):
        first = build_match("100", details(), {1: [ball()]}, References())
        second = build_match("100", details(), {1: [ball()]}, References())
        self.assertEqual(_canonical(first.match), _canonical(second.match))


class RawStoreContractTests(unittest.TestCase):
    """The raw store must record what it stored."""

    def test_fetch_meta_records_the_bundle_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw_dir = Path(tmp)
            match_dir = _write_raw(raw_dir, "100", details(), {1: [ball()]})
            meta = json.loads((match_dir / "fetch_meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["match_id"], "100")
            self.assertIn("fetched_at", meta)
            self.assertEqual(meta["bundle_sha256"], bundle_sha256(match_dir))
            self.assertIn("scorecard.json", meta["files"])

    def test_unchanged_payload_yields_an_unchanged_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw_dir = Path(tmp)
            first = _write_raw(raw_dir, "100", details(), {1: [ball()]})
            digest = bundle_sha256(first)
            shutil.rmtree(first)
            second = _write_raw(raw_dir, "100", details(), {1: [ball()]})
            self.assertEqual(digest, bundle_sha256(second))

    def test_changed_payload_yields_a_different_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw_dir = Path(tmp)
            first = _write_raw(raw_dir, "100", details(), {1: [ball()]})
            digest = bundle_sha256(first)
            shutil.rmtree(first)
            changed = details()
            changed["content"]["innings"][0]["runs"] = 42
            second = _write_raw(raw_dir, "100", changed, {1: [ball()]})
            self.assertNotEqual(digest, bundle_sha256(second))

    def test_super_over_chunks_do_not_collide_with_regulation_chunks(self):
        """A Super Over inning number must not overwrite a regulation chunk."""
        with tempfile.TemporaryDirectory() as tmp:
            raw_dir = Path(tmp)
            match_dir = raw_dir / "100"
            match_dir.mkdir(parents=True)
            body = json.dumps(details()).encode("utf-8")
            save_raw_payload(match_dir, "scorecard.json", body, "u", 200)
            regulation = json.dumps({"comments": [{"id": "reg"}]}).encode("utf-8")
            super_over = json.dumps({"comments": [{"id": "sup"}]}).encode("utf-8")
            save_raw_payload(match_dir, "commentary_inn3_chunk0.json", regulation, "u", 200)
            save_raw_payload(match_dir, "commentary_super3_chunk0.json", super_over, "u", 200)
            _details, chunks, error = load_raw_match_payloads(match_dir)
            self.assertIsNone(error)
            self.assertEqual(chunks[3], [{"id": "reg"}])
            self.assertEqual(chunks[("super", 3)], [{"id": "sup"}])


if __name__ == "__main__":
    unittest.main()
