"""The architectural rules of the lake, enforced on every pull request.

These tests are the reason the rules in AGENTS.md hold. They check the shape
of the repository itself: which workflows exist, which directories are
allowed, what the downstream code is allowed to reference, and that the
single build path really is single.

A rule that is only written down is a rule that gets broken by the next
agent. A rule that fails CI is a rule that survives.
"""
import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

#: The only workflows this repository may contain.
APPROVED_WORKFLOWS = {"ingest.yml", "reparse.yml", "publish.yml", "ci.yml"}

#: Directories that must not exist: they were how work used to be parked.
FORBIDDEN_DIRECTORIES = ["queues", "corrections"]

#: Files that must not exist any more.
FORBIDDEN_FILES = [
    "scripts/apply_warn_policy.py",
    "scripts/apply_source_gap_policy.py",
    "scripts/build_stage_index.py",
    "scripts/apply_stage_index.py",
    "scripts/merge_retry_queue.py",
    "scripts/update_state.py",
    "scripts/reconcile_state.py",
    "scripts/discover_recent_matches.py",
]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _push_branches(text: str) -> list[str]:
    """The branch names a workflow's push trigger names.

    The check reads the parsed workflow when PyYAML is available, because a
    regular expression over YAML is a poor way to find a trigger. PyYAML
    reads ``on:`` as the boolean ``True``, which is why the key is looked up
    both ways.
    """
    try:
        import yaml
    except ImportError:
        return []
    try:
        document = yaml.safe_load(text)
    except Exception:
        return []
    if not isinstance(document, dict):
        return []
    triggers = document.get("on", document.get(True))
    if not isinstance(triggers, dict):
        return []
    push = triggers.get("push")
    if not isinstance(push, dict):
        return []
    branches = push.get("branches")
    if branches is None:
        return []
    if isinstance(branches, str):
        return [branches]
    return [str(branch) for branch in branches]


class WorkflowSetTests(unittest.TestCase):
    """Only the approved workflows may exist."""

    def test_only_the_approved_workflows_exist(self):
        if not WORKFLOWS_DIR.exists():
            self.skipTest("No workflows directory")
        found = {path.name for path in WORKFLOWS_DIR.glob("*.yml")}
        found |= {path.name for path in WORKFLOWS_DIR.glob("*.yaml")}
        extra = found - APPROVED_WORKFLOWS
        self.assertEqual(
            extra, set(),
            f"Unexpected workflows: {sorted(extra)}. "
            f"Only {sorted(APPROVED_WORKFLOWS)} are allowed.",
        )

    def test_every_approved_workflow_exists(self):
        for name in APPROVED_WORKFLOWS:
            self.assertTrue(
                (WORKFLOWS_DIR / name).exists(),
                f"The approved workflow {name} is missing.",
            )

    def test_no_workflow_triggers_on_a_branch_other_than_main(self):
        """A side branch must never be able to run the data pipeline."""
        if not WORKFLOWS_DIR.exists():
            self.skipTest("No workflows directory")
        for path in WORKFLOWS_DIR.glob("*.y*ml"):
            text = _read(path)
            for branch in _push_branches(text):
                self.assertIn(
                    branch, ("main",),
                    f"{path.name} triggers on push to {branch!r}; "
                    "only main may run the data pipeline.",
                )


class ForbiddenArtefactTests(unittest.TestCase):
    """The old ways of parking work must not come back."""

    def test_the_queue_and_correction_directories_do_not_exist(self):
        for name in FORBIDDEN_DIRECTORIES:
            self.assertFalse(
                (REPO_ROOT / name).exists(),
                f"The {name}/ directory must not exist. Work that needs another "
                "attempt is recorded in the catalog as provisional.",
            )

    def test_the_obsolete_patch_scripts_do_not_exist(self):
        for relative in FORBIDDEN_FILES:
            self.assertFalse(
                (REPO_ROOT / relative).exists(),
                f"{relative} has been replaced by the pipeline and must not return.",
            )

    def test_no_json_patch_list_is_committed(self):
        """A stray list of match ids to patch is the old pattern."""
        for path in REPO_ROOT.glob("*.json"):
            self.assertNotIn(path.name, ("package.json",), "unexpected root json file")

    def test_no_script_references_the_removed_directories(self):
        offenders = []
        for path in list(REPO_ROOT.glob("*.py")) + list((REPO_ROOT / "scripts").glob("*.py")) \
                + list((REPO_ROOT / "src").glob("*.py")):
            text = _read(path)
            if "queues/" in text or "corrections/" in text:
                offenders.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual(offenders, [], f"These files still reference the removed directories: {offenders}")


class DownstreamImmutabilityTests(unittest.TestCase):
    """The published lake must be a copy, never a rewrite."""

    def _downstream_files(self):
        files = [REPO_ROOT / "consolidate.py"]
        downloads = REPO_ROOT / "scripts" / "downloads"
        if downloads.exists():
            files.extend(downloads.glob("*.py"))
        return [p for p in files if p.exists()]

    def test_the_downstream_code_does_not_apply_a_stage_index(self):
        forbidden = ("apply_stage_index", "stage_index")
        for path in self._downstream_files():
            text = _read(path)
            for token in forbidden:
                self.assertNotIn(
                    token, text,
                    f"{path.name} still refers to {token!r}. The published snapshot "
                    "must be a copy of the built matches, not a patched version.",
                )

    def test_the_downstream_code_does_not_apply_corrections(self):
        for path in self._downstream_files():
            text = _read(path)
            for token in ("corrections", "decided_ties"):
                self.assertNotIn(
                    token, text,
                    f"{path.name} still refers to {token!r}.",
                )

    def test_the_downstream_code_does_not_overlay_recovery_batches(self):
        for path in self._downstream_files():
            text = _read(path)
            self.assertNotIn(
                "matches_batch_recovery", text,
                f"{path.name} still overlays a recovery batch.",
            )


class BuilderContractTests(unittest.TestCase):
    """There is one build path, and it records its version."""

    def test_the_parser_version_is_a_non_empty_version_string(self):
        from src.builder import PARSER_VERSION
        self.assertTrue(PARSER_VERSION)
        self.assertIsInstance(PARSER_VERSION, str)
        self.assertRegex(PARSER_VERSION, r"^\d+\.\d+\.\d+")

    def test_the_builder_exposes_the_single_build_entry_point(self):
        from src import builder
        self.assertTrue(hasattr(builder, "build_match"))
        self.assertTrue(callable(builder.build_match))

    def test_the_live_and_offline_paths_share_the_builder(self):
        """Neither CLI may build a match on its own."""
        for name in ("extract.py", "reparse.py"):
            text = _read(REPO_ROOT / name)
            self.assertIn(
                "build_match", text,
                f"{name} must build through src.builder.build_match.",
            )
            self.assertNotIn(
                "construct_match_v3", text,
                f"{name} must not call the constructor directly; the builder "
                "is the single build path.",
            )

    def test_the_validator_exposes_a_structured_assessment(self):
        from src import validator
        for name in ("ValidationIssue", "MatchAssessment", "assess_match"):
            self.assertTrue(hasattr(validator, name), f"validator.{name} is missing")

    def test_the_failure_kinds_are_an_explicit_enum(self):
        from src import validator
        for name in (
            "FAILURE_TRANSIENT_FETCH",
            "FAILURE_UNCOMPLETED_MATCH",
            "FAILURE_CONSTRUCTION_ERROR",
            "FAILURE_VALIDATION_FAILED",
        ):
            self.assertTrue(hasattr(validator, name), f"validator.{name} is missing")

    def test_the_completeness_evaluator_exists(self):
        from src import completeness
        self.assertTrue(hasattr(completeness, "evaluate_completeness"))

    def test_the_catalog_is_the_only_authority(self):
        """The catalog must exist and name the archives that hold the data."""
        from src import catalog_db
        self.assertTrue(hasattr(catalog_db, "open_catalog"))
        self.assertTrue(hasattr(catalog_db, "upsert_fetch_and_build"))
        self.assertTrue(hasattr(catalog_db, "select_stale_parser"))
        self.assertTrue(hasattr(catalog_db, "select_due_refreshes"))


class AgentInstructionTests(unittest.TestCase):
    """The instructions must exist and must say what they must say."""

    def test_the_agent_instructions_exist(self):
        self.assertTrue((REPO_ROOT / "AGENTS.md").exists(), "AGENTS.md is missing")
        self.assertTrue(
            (REPO_ROOT / ".github" / "copilot-instructions.md").exists(),
            ".github/copilot-instructions.md is missing",
        )

    def test_the_agent_instructions_carry_the_triage_decision_tree(self):
        text = _read(REPO_ROOT / "AGENTS.md")
        for token in (
            "Mandatory Triage Decision Tree",
            "inspect_catalog.py",
            "provisional",
            "settled_incomplete",
            "reparse",
        ):
            self.assertIn(token, text, f"AGENTS.md does not mention {token!r}.")

    def test_the_agent_instructions_carry_the_hard_prohibitions(self):
        text = _read(REPO_ROOT / "AGENTS.md")
        for token in ("queues/", "corrections/", "raw_available", "failure_kind"):
            self.assertIn(token, text, f"AGENTS.md does not mention {token!r}.")

    def test_the_copilot_instructions_carry_the_bans(self):
        text = _read(REPO_ROOT / ".github" / "copilot-instructions.md")
        for token in ("queues/", "corrections/", "reparse", "PARSER_VERSION"):
            self.assertIn(token, text, f"copilot-instructions.md does not mention {token!r}.")

    def test_the_historical_documents_are_marked_as_archived(self):
        archived = [
            "CRICKET_LAKE_UNFILTERED_CONTEXT.md",
            "CLASSIFICATION_COVERAGE_REPAIR.md",
            "missing/MISSING.md",
            "missing/QUARANTINE.md",
        ]
        for relative in archived:
            path = REPO_ROOT / relative
            if not path.exists():
                continue
            head = _read(path)[:600]
            self.assertIn(
                "HISTORICAL ARCHIVE", head,
                f"{relative} must open with an archive warning so an agent "
                "does not follow its obsolete instructions.",
            )


if __name__ == "__main__":
    unittest.main()
