"""ESPN-backed match stage, award and series metadata.

The ESPN details payload carries the round in ``match.title`` and
``match.finalType``, the match and series awards in ``content.supportInfo``,
and the series id in ``match.series``; it has no ``event`` object (that shape
belongs to Cricsheet). This module is the single derivation used by the
constructor and the offline stage-index backfill so both emit identical
values.
"""
from __future__ import annotations

import gzip
import json
import re
from pathlib import Path
from typing import Iterable

STAGE_FIELDS = ("match_stage", "match_number", "match_group", "match_title")
PLAYER_OF_MATCH_FIELDS = (
    "player_of_match_id",
    "player_of_match_name",
    "player_of_match_team",
)
PLAYER_OF_SERIES_FIELDS = (
    "player_of_series_id",
    "player_of_series_name",
    "player_of_series_team",
    "player_of_series_type",
)
SERIES_FIELDS = ("source_series_id",)
META_FIELDS = (
    STAGE_FIELDS
    + PLAYER_OF_MATCH_FIELDS
    + PLAYER_OF_SERIES_FIELDS
    + SERIES_FIELDS
)
_SKIP_JSON_NAMES = {"failed_matches.json", "manifest.json", "lake_manifest.json", "meta.json"}

_FINAL_TYPE_STAGE = {
    1: "Final",
    3: "Semi Final",
    4: "Quarter Final",
    7: "Qualifier",
    8: "Eliminator",
}

_TIER_PREFIXES = {
    "elite", "plate", "championship", "consolation", "cup", "trophy",
    "division", "pool", "zone", "region", "central", "north", "south",
    "east", "west", "central & west",
}


def _ordinal(value: int) -> str:
    if 10 <= value % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


def _clean_title(title) -> str | None:
    if title is None:
        return None
    text = re.sub(r"\s+", " ", str(title)).strip()
    if not text:
        return None
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r"(?:,\s*)+", ", ", text)
    return text.strip(" ,") or None


def _prefix_segment(prefix: str) -> str | None:
    if not prefix:
        return None
    segment = re.split(r",\s*", prefix.strip(" ,"))[-1].strip()
    if not segment:
        return None
    if re.search(r"\d", segment) or re.search(r"\bmatch\b", segment, re.I):
        return None
    if re.search(r"\bsuper\b", segment, re.I):
        return None
    return segment if segment.lower() in _TIER_PREFIXES else None


def _parse_group(title: str | None) -> str | None:
    if not title:
        return None
    match = re.search(r"\bgroup\s+([A-Za-z0-9]+)(?:\s*[/&]\s*([A-Za-z0-9]+))?", title, re.I)
    if match:
        label = match.group(1)
        if match.group(2):
            label = f"{label}/{match.group(2)}"
        prefix = _prefix_segment(title[: match.start()])
        if label.lower() == "stage":
            return None
        return f"{prefix} {label}" if prefix else label
    trailing = re.search(r"\b([A-Za-z][A-Za-z &]+?)\s+group\b", title, re.I)
    if trailing:
        return trailing.group(1).strip()
    return None


def _stage_from_title(title: str | None) -> str | None:
    if not title:
        return None
    lower = title.lower()

    place = re.search(r"\b(\d+)(?:st|nd|rd|th)\s+place\s+play[\s-]?off\b", lower)
    if place:
        return f"{_ordinal(int(place.group(1)))} Place Play-off"
    if re.search(r"\bplay[\s-]?offs?\b", lower):
        return "Play-off"
    if re.search(r"\beliminat(?:ion|or)\s+final\b", lower):
        return "Eliminator"
    if re.search(r"\b(?:preliminary|minor)\s+final\b", lower):
        return "Semi Final"
    if re.search(r"\bqualifying\s+final\b", lower):
        return "Qualifier"
    if re.search(r"\bgrand\s+final\b", lower):
        return "Final"
    if re.search(r"\bquarter[\s-]*finals?\b", lower):
        return "Quarter Final"
    if re.search(r"\bsemi[\s-]*finals?\b", lower):
        return "Semi Final"

    qualifier = re.search(r"\bqualifier\s*(\d+)\b", lower)
    if qualifier:
        return f"Qualifier {qualifier.group(1)}"
    qualifier = re.search(r"\b(\d+)(?:st|nd|rd|th)\s+qualifier\b", lower)
    if qualifier:
        return f"Qualifier {qualifier.group(1)}"
    if re.search(r"\bqualifier\b", lower):
        return "Qualifier"
    if re.search(r"\beliminator\b", lower):
        return "Eliminator"
    if re.search(r"\bchallenger\b", lower):
        return "Challenger"
    if re.search(r"\bknock[\s-]?out\b", lower):
        return "Knockout"

    if re.search(r"\bsuper\s+league\b", lower):
        return "Super League"
    if re.search(r"\bsuper\s+(?:eights?|8)\b", lower):
        return "Super Eights"
    if re.search(r"\bsuper\s+(?:sixes?|six|6)\b", lower):
        return "Super Sixes"
    if re.search(r"\bsuper\s+(?:fours?|4)\b", lower):
        return "Super Four"
    if re.search(r"\bsuper\s+(?:ten|10)\b", lower):
        return "Super 10"
    if re.search(r"\bsuper\s+(?:twelve|12)\b", lower):
        return "Super 12"
    if re.search(r"\bfirst\s+round\b", lower):
        return "First Round"
    if re.search(r"\bsuper\b", lower):
        return "Super"

    if re.search(r"\bfinals?\b", lower):
        return "Final"
    return None


def _parse_number(title: str | None) -> int | None:
    if not title:
        return None
    lower = title.lower()

    if re.search(r"\b\d+(?:st|nd|rd|th)\s+place\s+play[\s-]?off\b", lower):
        return None

    match = re.search(r"\bqualifier\s+(\d+)\b", lower)
    if match:
        return int(match.group(1))
    match = re.search(r"\b(\d+)(?:st|nd|rd|th)\s+qualifier\b", lower)
    if match:
        return int(match.group(1))

    match = re.match(r"^(\d+)(?:st|nd|rd|th)\b", lower)
    if match:
        return int(match.group(1))
    match = re.match(r"^match\s+(\d+)\b", lower)
    if match:
        return int(match.group(1))
    if re.match(r"^(?:only|one[\s-]?off)\b", lower):
        return 1

    for pattern in (
        r"\bsemi[\s-]*final\s+(\d+)\b",
        r"\bquarter[\s-]*final\s+(\d+)\b",
        r"\bfinal\s+(\d+)\b",
    ):
        match = re.search(pattern, lower)
        if match:
            return int(match.group(1))
    return None


def derive_stage(match_info: dict | None) -> dict:
    """Return the four stage fields for an ESPN ``match`` object.

    ``finalType`` is authoritative for the standard knockout rounds; the title
    supplies the round number, group and the stages ESPN does not encode in
    ``finalType`` (play-offs, super rounds, challengers).
    """
    info = match_info if isinstance(match_info, dict) else {}
    raw_title = info.get("title")
    title = _clean_title(raw_title)
    stage = _stage_from_title(title)

    final_type = info.get("finalType")
    if not isinstance(final_type, int) or isinstance(final_type, bool):
        try:
            final_type = int(final_type)
        except (TypeError, ValueError):
            final_type = None
    if stage is None and final_type in _FINAL_TYPE_STAGE:
        stage = _FINAL_TYPE_STAGE[final_type]

    return {
        "match_stage": stage,
        "match_number": _parse_number(title),
        "match_group": _parse_group(title),
        "match_title": str(raw_title).strip() if raw_title is not None and str(raw_title).strip() else None,
    }


def unwrap_match_payload(payload):
    """Normalize the two ESPN shapes to the flat ``{match, content}`` form."""
    if not isinstance(payload, dict):
        return payload
    if isinstance(payload.get("match"), dict):
        return payload
    nested = payload.get("data")
    if isinstance(nested, dict) and isinstance(nested.get("match"), dict):
        return nested
    return payload


def _as_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_text(value):
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _award_entries(content, keys) -> list:
    """Official award entries from an ESPN ``content`` object.

    ``content.matchPlayerAwards`` is the recorded awards list; ``supportInfo``
    mirrors the same entries (``playersOfTheMatch`` for the match award,
    ``playersOfTheSeries`` for the series award). Both are official.
    ``supportInfo.mostValuedPlayerOfTheMatch`` is ESPN's computed impact
    metric (it carries ``smartRuns``/``totalImpact``), not the award, and is
    never read here.
    """
    info = content if isinstance(content, dict) else {}
    support = info.get("supportInfo")
    if not isinstance(support, dict):
        support = {}
    entries = []
    for source in (info, support):
        for key in keys:
            value = source.get(key)
            if isinstance(value, list):
                entries.extend(item for item in value if isinstance(item, dict))
    return entries


def derive_player_of_match(content) -> dict:
    """Return the Player of the Match fields from an ESPN ``content`` object.

    The official award is the ``PLAYER_OF_MATCH`` entry in
    ``content.matchPlayerAwards`` (mirrored in
    ``supportInfo.playersOfTheMatch``). The player id recorded is the ESPN
    ``objectId``, which is the served player identity; ``id`` is only a
    fallback. Absent or malformed data yields all-null fields; nothing is
    inferred. A rare shared award keeps the first entry in recorded order.
    """
    for award in _award_entries(content, ("matchPlayerAwards", "playersOfTheMatch")):
        if _as_text(award.get("type")) != "PLAYER_OF_MATCH":
            continue
        player = award.get("player")
        if not isinstance(player, dict):
            player = {}
        team = award.get("team")
        if not isinstance(team, dict):
            team = {}
        player_id = _as_int(player.get("objectId"))
        if player_id is None:
            player_id = _as_int(player.get("id"))
        player_name = _as_text(player.get("name")) or _as_text(player.get("longName"))
        if player_id is None and player_name is None:
            continue
        return {
            "player_of_match_id": player_id,
            "player_of_match_name": player_name,
            "player_of_match_team": _as_text(team.get("name")) or _as_text(team.get("longName")),
        }
    return {field: None for field in PLAYER_OF_MATCH_FIELDS}


_SERIES_AWARD_TYPES = ("PLAYER_OF_SERIES", "PLAYER_OF_TOURNAMENT", "PLAYER_OF_THE_TOURNAMENT")


def derive_player_of_series(content) -> dict:
    """Return the Player of the Series/Tournament fields from an ESPN content object.

    The official award is a ``PLAYER_OF_SERIES``/``PLAYER_OF_TOURNAMENT``
    entry in ``content.matchPlayerAwards`` (mirrored in
    ``supportInfo.playersOfTheSeries``). A ``PLAYER_OF_SERIES`` entry wins
    over a tournament entry; the recorded type is preserved. Absent data
    yields all-null fields.
    """
    for wanted in _SERIES_AWARD_TYPES:
        for award in _award_entries(content, ("matchPlayerAwards", "playersOfTheSeries")):
            if _as_text(award.get("type")) != wanted:
                continue
            player = award.get("player")
            if not isinstance(player, dict):
                player = {}
            team = award.get("team")
            if not isinstance(team, dict):
                team = {}
            player_id = _as_int(player.get("objectId"))
            if player_id is None:
                player_id = _as_int(player.get("id"))
            player_name = _as_text(player.get("name")) or _as_text(player.get("longName"))
            if player_id is None and player_name is None:
                continue
            return {
                "player_of_series_id": player_id,
                "player_of_series_name": player_name,
                "player_of_series_team": _as_text(team.get("name")) or _as_text(team.get("longName")),
                "player_of_series_type": wanted,
            }
    return {field: None for field in PLAYER_OF_SERIES_FIELDS}


def derive_series_id(match_info) -> dict:
    """Return the ESPN series id recorded on a match object, if any."""
    info = match_info if isinstance(match_info, dict) else {}
    series = info.get("series")
    if not isinstance(series, dict):
        return {"source_series_id": None}
    return {"source_series_id": _as_int(series.get("id"))}


def load_stage_index(path: str | Path) -> dict[str, dict]:
    path = Path(path)
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        matches = payload.get("matches")
        if not isinstance(matches, dict) and all(isinstance(value, dict) for value in payload.values()):
            matches = payload
    else:
        matches = payload
    if not isinstance(matches, dict):
        raise ValueError(f"{path}: expected an object with a 'matches' map")
    return {str(key): value for key, value in matches.items() if isinstance(value, dict)}

def apply_stage_entry(meta: dict, entry: dict) -> bool:
    """Copy non-null index values into ``meta``; never erase existing data."""
    changed = False
    for key in META_FIELDS:
        value = entry.get(key)
        if value is None:
            continue
        if meta.get(key) != value:
            meta[key] = value
            changed = True
    return changed


def apply_stage_payload(payload: bytes, entry: dict) -> bytes:
    data = json.loads(payload.decode("utf-8"))
    meta = data.get("meta")
    if not isinstance(meta, dict) or not apply_stage_entry(meta, entry):
        return payload
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def apply_stage_index_to_dir(
    matches_dir: str | Path,
    index_path: str | Path | None = None,
    *,
    index: dict[str, dict] | None = None,
    only_ids: Iterable[str] | None = None,
    dry_run: bool = False,
) -> dict:
    if index is None:
        if index_path is None:
            raise ValueError("index or index_path is required")
        index = load_stage_index(index_path)
    wanted = {str(mid) for mid in only_ids} if only_ids is not None else None

    stats = {"considered": 0, "matched": 0, "changed": 0, "skipped_no_index": 0}
    for path in sorted(Path(matches_dir).glob("*.json")):
        if path.name in _SKIP_JSON_NAMES:
            continue
        mid = path.stem
        if not mid.isdigit():
            continue
        if wanted is not None and mid not in wanted:
            continue
        stats["considered"] += 1
        entry = index.get(mid)
        if not entry:
            stats["skipped_no_index"] += 1
            continue
        stats["matched"] += 1
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        meta = data.get("meta")
        if not isinstance(meta, dict):
            continue
        if not apply_stage_entry(meta, entry):
            continue
        stats["changed"] += 1
        if dry_run:
            continue
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
    return stats
