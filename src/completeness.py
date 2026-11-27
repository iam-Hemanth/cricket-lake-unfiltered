"""
src/completeness.py
Raw and telemetry completeness evaluator.

A completed match can still be missing metadata that ESPN publishes later:
the player of the match, the series id, the awards table, the wagon wheel and
pitch-tracking lines. A match fetched the moment it finishes therefore looks
incomplete even though nothing is wrong with it.

This module measures that gap and drives the maturity state machine:

``provisional``
    The match is recent and at least one completeness check fails. It is
    eligible for a scheduled refresh.
``settled``
    The match is complete.
``settled_incomplete``
    The match is past the maturity window (or has exhausted its refresh
    attempts) and still carries a gap. The gap is recorded and disclosed
    rather than repaired.

The evaluator never fabricates a value. If ESPN has not published something,
the corresponding flag stays false and the match stays provisional until the
window closes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

#: How long a match may stay provisional before it settles as it is.
MATURITY_WINDOW_DAYS = 14

#: Refresh backoff schedule, in hours after the previous attempt.
REFRESH_BACKOFF_HOURS = (12, 24, 72, 168)

#: The number of refresh attempts after which a match stops being refreshed.
MAX_REFRESH_ATTEMPTS = 4


@dataclass
class CompletenessReport:
    """The completeness of one match and the maturity it implies."""

    match_date: Optional[str] = None
    age_days: Optional[float] = None
    has_series_id: bool = False
    has_match_title: bool = False
    has_player_of_match: bool = False
    has_deliveries_for_nonzero_innings: bool = True
    has_tactical: bool = False
    has_awards: bool = False
    needs_refresh: bool = False
    refresh_reasons: List[str] = field(default_factory=list)
    maturity_state: str = "settled"
    next_refresh_at: Optional[str] = None

    @property
    def gaps(self) -> List[str]:
        """The names of the completeness checks that failed."""
        return list(self.refresh_reasons)

    def to_dict(self) -> dict:
        return {
            "match_date": self.match_date,
            "age_days": self.age_days,
            "has_series_id": self.has_series_id,
            "has_match_title": self.has_match_title,
            "has_player_of_match": self.has_player_of_match,
            "has_deliveries_for_nonzero_innings": self.has_deliveries_for_nonzero_innings,
            "has_tactical": self.has_tactical,
            "has_awards": self.has_awards,
            "needs_refresh": self.needs_refresh,
            "refresh_reasons": list(self.refresh_reasons),
            "maturity_state": self.maturity_state,
            "next_refresh_at": self.next_refresh_at,
        }


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _match_date(meta: dict) -> Optional[str]:
    """The first match date, as recorded in the metadata."""
    dates = meta.get("dates") or []
    for value in dates:
        if value:
            return str(value)[:10]
    return None


def _age_days(match_date: Optional[str], now_utc: datetime) -> Optional[float]:
    parsed = _parse_date(match_date)
    if parsed is None:
        return None
    return (now_utc - parsed).total_seconds() / 86400.0


def _has_awards(match_v3: Optional[dict]) -> bool:
    """Whether the match carries an awards table."""
    if not isinstance(match_v3, dict):
        return False
    for key in ("awards", "match_awards", "player_awards"):
        value = match_v3.get(key)
        if value:
            return True
    meta = match_v3.get("meta") or {}
    for key in ("awards", "awards_count", "has_awards"):
        if meta.get(key):
            return True
    return False


def _deliveries_present_for_nonzero_innings(match_v3: Optional[dict]) -> bool:
    """Whether every innings that scored has a ball-by-ball record."""
    if not isinstance(match_v3, dict):
        return True
    for inn in match_v3.get("innings") or []:
        snapshot = inn.get("source_scorecard") or {}
        runs = snapshot.get("runs")
        balls = snapshot.get("balls")
        scored = bool(runs) or bool(balls)
        if not scored:
            continue
        deliveries = [
            d for ov in inn.get("overs") or [] for d in ov.get("deliveries") or []
        ]
        if not deliveries:
            # A scorecard-only Super Over innings carries no ball-by-ball
            # coverage by design and is excluded from the check.
            if inn.get("super_over"):
                continue
            return False
    return True


def _next_refresh_at(attempts: int, now_utc: datetime) -> Optional[str]:
    """The next refresh time for a match that is still provisional."""
    index = max(0, min(attempts, len(REFRESH_BACKOFF_HOURS) - 1))
    hours = REFRESH_BACKOFF_HOURS[index]
    from datetime import timedelta
    return (now_utc + timedelta(hours=hours)).isoformat()


def evaluate_completeness(
    raw_details: dict,
    match_v3: Optional[dict],
    assessment=None,
    now_utc: Optional[datetime] = None,
    refresh_attempts: int = 0,
) -> CompletenessReport:
    """Measure the completeness of a match and the maturity it implies.

    ``raw_details`` is the stored ESPN payload, ``match_v3`` the built match
    (may be None when the build failed) and ``assessment`` the tier that was
    assigned (may be None when the build failed).
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    meta = (match_v3 or {}).get("meta") or {}
    match_date = _match_date(meta)
    age_days = _age_days(match_date, now_utc)

    report = CompletenessReport(match_date=match_date, age_days=age_days)
    report.has_series_id = meta.get("source_series_id") is not None
    report.has_match_title = bool(meta.get("match_title"))
    report.has_player_of_match = meta.get("player_of_match_id") is not None
    report.has_deliveries_for_nonzero_innings = _deliveries_present_for_nonzero_innings(match_v3)
    report.has_tactical = bool((meta.get("tactical_coverage") or 0) > 0)
    report.has_awards = _has_awards(match_v3)

    reasons: List[str] = []
    if not report.has_series_id:
        reasons.append("missing_series_id")
    if not report.has_deliveries_for_nonzero_innings:
        reasons.append("missing_deliveries_for_nonzero_innings")
    if not report.has_player_of_match:
        # A player of the match is only expected when the match produced a
        # result and at least one ball was bowled.
        outcome = (match_v3 or {}).get("outcome") or {}
        result_text = str(outcome.get("result") or "").strip()
        has_result = bool(result_text) and "no result" not in result_text.lower()
        has_balls = any(
            [d for ov in inn.get("overs") or [] for d in ov.get("deliveries") or []]
            for inn in (match_v3 or {}).get("innings") or []
        )
        if has_result and has_balls:
            reasons.append("missing_player_of_match")
    if assessment is not None and getattr(assessment, "tier", None) == "failed":
        reasons.append("validation_failed")

    report.refresh_reasons = reasons
    report.needs_refresh = bool(reasons)

    within_window = age_days is not None and age_days <= MATURITY_WINDOW_DAYS
    attempts_exhausted = refresh_attempts >= MAX_REFRESH_ATTEMPTS

    if not reasons:
        report.maturity_state = "settled"
    elif within_window and not attempts_exhausted:
        report.maturity_state = "provisional"
        report.next_refresh_at = _next_refresh_at(refresh_attempts, now_utc)
    else:
        report.maturity_state = "settled_incomplete"

    return report
