"""
src/builder.py
The single build path for a Match JSON.

Live ingestion and the offline re-parser call the same function. There is no
second implementation, so a parser change cannot produce two different
readings of the same raw payload.

``build_match`` takes the raw ESPN payload and the commentary chunks that were
stored alongside it, constructs the match, assigns its validation tier in one
pass, measures its completeness, and returns the result. It performs no I/O
and no network access: the caller decides where the raw payload came from and
where the built JSON goes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.completeness import CompletenessReport, evaluate_completeness
from src.constructor import construct_match_v3
from src.validator import MatchAssessment, assess_match

#: Bumped whenever the output of the constructor, the stage metadata, the
#: classification, the hundred rules or the validator changes. The catalog
#: compares it against the stored value to find matches that need a rebuild.
PARSER_VERSION = "3.3.1-unified"

#: The schema version this builder emits.
SCHEMA_VERSION = "3.3.0"


@dataclass
class References:
    """The reference data the constructor needs.

    Keeping these in one object lets the builder stay free of file access, so
    the same call works in a test, a local run and a CI job.
    """

    player_profiles: dict = field(default_factory=dict)
    people_registry: dict = field(default_factory=dict)
    venue_map: dict = field(default_factory=dict)


@dataclass
class BuildResult:
    """The outcome of building one match."""

    match_id: str
    match: Optional[dict] = None
    tier: str = "failed"
    assessment: Optional[MatchAssessment] = None
    completeness: Optional[CompletenessReport] = None
    error: Optional[str] = None
    failure_kind: Optional[str] = None

    @property
    def ok(self) -> bool:
        """Whether a match JSON was produced."""
        return self.match is not None

    @property
    def is_publishable(self) -> bool:
        """Whether the tier is admitted to the published lake."""
        return self.assessment is not None and self.assessment.is_publishable

    def to_dict(self) -> dict:
        payload = {
            "match_id": self.match_id,
            "tier": self.tier,
            "failure_kind": self.failure_kind,
        }
        if self.error:
            payload["error"] = self.error
        if self.assessment is not None:
            payload["validation_errors"] = self.assessment.errors
            payload["validation_warnings"] = self.assessment.warnings
        if self.completeness is not None:
            payload["completeness"] = self.completeness.to_dict()
        return payload


def _scorecard_innings(raw_details: dict) -> List[dict]:
    """The scorecard innings the match is validated against.

    A Super Over decider is part of the match, so its innings are validated
    alongside the regulation innings.
    """
    content = (raw_details or {}).get("content") or {}
    return list(content.get("innings") or []) + list(content.get("superOverInnings") or [])


def build_match(
    mid: str,
    raw_details: dict,
    commentary_chunks_by_inn: Dict[object, List[dict]],
    references: Optional[References] = None,
    now_utc: Optional[datetime] = None,
    refresh_attempts: int = 0,
) -> BuildResult:
    """Build one Match JSON from a stored raw payload.

    The function is pure: it reads no files, opens no sockets and writes no
    files. The caller supplies the raw payload it already stored, and the
    result is the match JSON plus the tier and completeness it was given.
    """
    references = references or References()
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    try:
        match_v3, construction_error = construct_match_v3(
            mid=str(mid),
            raw_details=raw_details,
            commentary_chunks_by_inn=commentary_chunks_by_inn or {},
            player_profiles=references.player_profiles,
            people_registry=references.people_registry,
            venue_map=references.venue_map,
        )
    except Exception as exc:  # a construction defect is a data verdict, not a crash
        return BuildResult(
            match_id=str(mid),
            tier="failed",
            error=f"Construction exception: {exc}",
            failure_kind="construction_error",
        )

    if construction_error or not match_v3:
        return BuildResult(
            match_id=str(mid),
            tier="failed",
            error=f"Construction error: {construction_error}",
            failure_kind="uncompleted_match"
            if "uncompleted" in str(construction_error).lower()
            else "construction_error",
        )

    meta = match_v3.setdefault("meta", {})
    meta["parser_version"] = PARSER_VERSION

    assessment = assess_match(match_v3, _scorecard_innings(raw_details))
    meta["validation_status"] = assessment.tier
    if assessment.warnings:
        meta["validation_warnings"] = list(assessment.warnings)
    if assessment.errors:
        meta["validation_errors"] = list(assessment.errors)
    if assessment.source_gap is not None:
        meta["source_gap"] = assessment.source_gap

    completeness = evaluate_completeness(
        raw_details=raw_details,
        match_v3=match_v3,
        assessment=assessment,
        now_utc=now_utc,
        refresh_attempts=refresh_attempts,
    )

    return BuildResult(
        match_id=str(mid),
        match=match_v3,
        tier=assessment.tier,
        assessment=assessment,
        completeness=completeness,
        failure_kind=assessment.failure_kind,
    )
