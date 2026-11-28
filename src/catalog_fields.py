"""
src/catalog_fields.py
The row a built match contributes to the catalog and the published index.

One extraction function keeps ingestion, the offline re-parser, the migration
and the published ``matches_index.csv`` from computing different teams,
totals or result text for the same match.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from src.source_facts import source_only_timed_out
from src.validator import NON_DISMISSAL_KINDS, count_team_wickets

#: The columns of the published ``matches_index.csv``.
CATALOG_FIELDS = [
    "filename", "match_id", "date", "format", "format_bucket", "schema_version",
    "record_class", "playing_format", "international_status", "classification_status",
    "record_stats_eligible", "validation_status", "gender", "competition", "season",
    "team1", "team2", "winner", "result", "toss_winner", "toss_decision", "venue",
    "city", "venue_country", "total_balls", "total_runs", "total_wickets",
    "wagon_wheel_balls", "pitch_line_balls", "shot_type_balls", "match_stage",
    "match_number", "match_group", "match_title",
]

#: The catalog-only columns appended to the published index.
INDEX_CATALOG_FIELDS = [
    "parser_version", "first_published_at", "built_updated_at",
    "content_updated_at",
]

#: Every column of the published index, in order.
INDEX_FIELDS = CATALOG_FIELDS + INDEX_CATALOG_FIELDS


def _count_wickets(deliveries: List[dict]) -> int:
    """Count dismissal events, without failing on an unknown source label."""
    try:
        return count_team_wickets(deliveries)
    except ValueError:
        return sum(
            1
            for delivery in deliveries
            for wicket in delivery.get("wickets") or []
            if str(wicket.get("kind") or "").strip().lower() not in NON_DISMISSAL_KINDS
        )


def teams(match: dict) -> Tuple[str, str]:
    """The two teams, in the order the match's first innings established."""
    innings = match.get("innings") or []
    if innings:
        first = innings[0]
        team1 = first.get("batting_team") or ""
        team2 = first.get("bowling_team") or ""
        if team1 or team2:
            return team1, team2
    xi = match.get("playing_xi") or {}
    names = [name for name in xi if not str(name).startswith("_") and isinstance(xi[name], list)]
    names.sort()
    return (names[0] if names else ""), (names[1] if len(names) > 1 else "")


def result_text(outcome: dict) -> str:
    """The result line the published index uses."""
    winner = outcome.get("winner") or ""
    by_runs = outcome.get("win_by_runs")
    by_wickets = outcome.get("win_by_wickets")
    if by_runs:
        return f"{winner} won by {by_runs} runs"
    if by_wickets:
        return f"{winner} won by {by_wickets} wickets"
    if outcome.get("result"):
        return str(outcome["result"])
    if winner:
        return f"{winner} won"
    return "No result"


def _metrics(match: dict) -> Dict[str, int]:
    total_balls = total_runs = total_wickets = 0
    wagon = pitch = shot = 0
    for innings in match.get("innings") or []:
        total_runs += innings.get("innings_penalty_runs", 0) or 0
        deliveries = [
            delivery
            for over in innings.get("overs") or []
            for delivery in over.get("deliveries") or []
        ]
        total_wickets += _count_wickets(deliveries)
        total_wickets += source_only_timed_out(
            deliveries, innings.get("source_scorecard") or {}
        )
        for delivery in deliveries:
            total_balls += 1
            total_runs += (delivery.get("runs") or {}).get("total", 0)
            tactical = delivery.get("tactical") or {}
            if tactical.get("wagon_coords") is not None:
                wagon += 1
            if tactical.get("pitch_line") is not None:
                pitch += 1
            if tactical.get("shot_type") is not None:
                shot += 1
    return {
        "total_balls": total_balls,
        "total_runs": total_runs,
        "total_wickets": total_wickets,
        "wagon_wheel_balls": wagon,
        "pitch_line_balls": pitch,
        "shot_type_balls": shot,
    }


def extract_catalog_metadata(match: dict) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """The catalog identity columns and the published index row of a match."""
    meta = match.get("meta") or {}
    outcome = match.get("outcome") or {}
    toss = match.get("toss") or {}
    dates = meta.get("dates") or []
    match_id = str(match.get("match_id") or "")
    date = str(dates[0]) if dates else ""
    team1, team2 = teams(match)
    result = result_text(outcome)
    metrics = _metrics(match)

    identity = {
        "match_date": date or None,
        "format": meta.get("format"),
        "format_bucket": meta.get("format_bucket"),
        "competition": meta.get("competition"),
        "season": str(meta.get("season")) if meta.get("season") is not None else None,
        "team1": team1,
        "team2": team2,
        "winner": outcome.get("winner"),
        "result": result,
        "gender": meta.get("gender"),
    }
    index_meta = {
        "filename": f"{match_id}.json",
        "match_id": match_id,
        "date": date,
        "format": meta.get("format") or "",
        "format_bucket": meta.get("format_bucket") or meta.get("format") or "",
        "schema_version": meta.get("schema_version") or "unknown",
        "record_class": meta.get("record_class") or "unknown",
        "playing_format": meta.get("playing_format") or "unknown",
        "international_status": meta.get("international_status") or "unknown",
        "classification_status": meta.get("classification_status") or "legacy_unverified",
        "record_stats_eligible": meta.get("record_stats_eligible"),
        "validation_status": meta.get("validation_status") or "clean",
        "gender": meta.get("gender") or "unknown",
        "competition": meta.get("competition") or "",
        "season": str(meta.get("season") or ""),
        "team1": team1,
        "team2": team2,
        "winner": outcome.get("winner") or "",
        "result": result,
        "toss_winner": toss.get("winner") or "",
        "toss_decision": toss.get("decision") or "",
        "venue": meta.get("venue") or "",
        "city": meta.get("city") or "",
        "venue_country": meta.get("venue_country") or "",
        "match_stage": meta.get("match_stage") or "",
        "match_number": meta.get("match_number") if meta.get("match_number") is not None else "",
        "match_group": meta.get("match_group") or "",
        "match_title": meta.get("match_title") or "",
        **metrics,
    }
    return identity, index_meta
