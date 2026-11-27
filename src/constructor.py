"""
src/constructor.py
Match JSON v3.0 Constructor from ESPNcricinfo Raw Data.
"""
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from src import hundred, classification, source_facts
from src.stage import (
    derive_player_of_match,
    derive_player_of_series,
    derive_series_id,
    derive_stage,
)

from src.participants import build_participant_registry, extract_participation_events, confirmed_lineups, _id

DISMISSAL_KIND_MAP = {
    1: "caught",
    2: "bowled",
    3: "lbw",
    4: "run out",
    5: "stumped",
    6: "hit wicket",
    7: "handled the ball",
    8: "obstructing the field",
    9: "hit the ball twice",
    10: "timed out",
    11: "retired out",
    13: "retired hurt"
}


def normalize_delivery_runs(ball: dict, match_meta: dict) -> Tuple[dict, dict, Optional[dict]]:
    """Keep source totals and correct only an evidenced seasonal component rule."""
    def quantity(key):
        value = ball.get(key)
        value = 0 if value is None else value
        if type(value) is not int or value < 0:
            raise ValueError(f"Invalid run component {key}: {value!r}")
        return value

    batter = quantity("batsmanRuns")
    extras = {name: quantity(source) for name, source in (
        ("wides", "wides"), ("noballs", "noballs"), ("byes", "byes"),
        ("legbyes", "legbyes"), ("penalty", "penalties"),
    )}
    component_total = batter + sum(extras.values())
    total = ball.get("totalRuns")
    if total is None:
        total = component_total
    if type(total) is not int or total < 0:
        raise ValueError(f"Invalid total run component: {total!r}")
    note = None
    dates = match_meta.get("dates") or []
    other_extras = sum(value for name, value in extras.items() if name != "noballs")
    rule_pack = hundred.ruleset(match_meta.get("season"), dates[0] if dates else None, match_meta.get("competition"))
    if (total != component_total and extras["noballs"] == 1
            and rule_pack and rule_pack["no_ball_penalty_runs"] == 2
            and total - batter - other_extras == 2):
        extras["noballs"] = 2
        component_total += 1
        note = {"source_ball_id": ball.get("id"), "field": "extras.noballs",
                "source_value": 1, "normalized_value": 2,
                "rule_id": rule_pack["id"]}
    if total != component_total:
        raise ValueError(f"Run component mismatch at {ball.get('oversActual')}: "
                         f"source total {total} != components {component_total}")
    extras = {name: value for name, value in extras.items() if value}
    return {"batter": batter, "extras": sum(extras.values()), "total": total}, extras, note


def _optional_count(value):
    return value if type(value) is int and value >= 0 else None


def _optional_number(value):
    return float(value) if type(value) in (int, float) else None


def _optional_text(value):
    return value if type(value) is str and value else None


def _scorecard_player_id(source_player, id_map, players):
    """Resolve a scorecard-only actor to a registered participant, or None."""
    player = source_player if isinstance(source_player, dict) else {}
    internal = _id(player.get("id"))
    pid = id_map.get(internal) if internal is not None else None
    if pid is None:
        public = _id(player.get("objectId"))
        candidate = f"espn_{public}" if public is not None else None
        if candidate in players:
            pid = candidate
    return pid if pid in players else None


def scorecard_only_details(inn, id_map, players):
    """Per-actor scorecard lines for a Super Over ESPN serves without deliveries."""
    batters = []
    for entry in inn.get("inningBatsmen") or []:
        player = entry.get("player") or {}
        dismissal = entry.get("dismissalText")
        if isinstance(dismissal, dict):
            dismissal = dismissal.get("short")
        batters.append({
            "player_id": _scorecard_player_id(player, id_map, players),
            "name": _optional_text(player.get("name")),
            "runs": _optional_count(entry.get("runs")),
            "balls": _optional_count(entry.get("balls")),
            "fours": _optional_count(entry.get("fours")),
            "sixes": _optional_count(entry.get("sixes")),
            "strike_rate": _optional_number(entry.get("strikerate")),
            "is_out": entry.get("isOut") if type(entry.get("isOut")) is bool else None,
            "dismissal_text": _optional_text(dismissal),
        })
    bowlers = []
    for entry in inn.get("inningBowlers") or []:
        player = entry.get("player") or {}
        bowlers.append({
            "player_id": _scorecard_player_id(player, id_map, players),
            "name": _optional_text(player.get("name")),
            "overs": _optional_number(entry.get("overs")),
            "balls": _optional_count(entry.get("balls")),
            "maidens": _optional_count(entry.get("maidens")),
            "runs_conceded": _optional_count(entry.get("conceded")),
            "wickets": _optional_count(entry.get("wickets")),
            "economy": _optional_number(entry.get("economy")),
            "wides": _optional_count(entry.get("wides")),
            "noballs": _optional_count(entry.get("noballs")),
        })
    fow = []
    for entry in inn.get("inningFallOfWickets") or []:
        # ESPN marks the innings-end total with fowType 2, fowWicketNum 0 and no
        # dismissal batsman. It is not a wicket, so it is not stored as one.
        fow_type = entry.get("fowType")
        if fow_type is not None and fow_type != 1:
            continue
        player = entry.get("dismissalBatsman") or {}
        fow.append({
            "wicket_number": _optional_count(entry.get("fowWicketNum")),
            "runs": _optional_count(entry.get("fowRuns")),
            "overs": _optional_number(entry.get("fowOvers")),
            "batter_id": _scorecard_player_id(player, id_map, players),
            "batter_name": _optional_text(player.get("name")),
        })
    return {"batters": batters, "bowlers": bowlers, "fow": fow}


def construct_match_v3(
    mid: str,
    raw_details: dict,
    commentary_chunks_by_inn: Dict[object, List[dict]],
    player_profiles: dict,
    people_registry: dict,
    venue_map: dict
) -> Tuple[Optional[dict], Optional[str]]:
    """Assemble complete Match JSON v3.0."""
    match_info = raw_details.get("match") or {}
    content = raw_details.get("content") or {}
    if 'content' not in raw_details or 'innings' not in content:
        return None, f"Match {mid}: missing source scorecard innings"
    if match_info.get('state') is not None and match_info['state']!='POST':
        return None, f"Match {mid}: uncompleted source match state {match_info['state']}"
    series = match_info.get("series") or {}

    comp_name = series.get("name") or (match_info.get("event") or {}).get("name")
    raw_fmt = match_info.get("format")
    class_meta = classification.classify(match_info, classification.roster_gender_values(raw_details))
    meta_fmt, db_fmt_bucket = class_meta["format"], class_meta["format_bucket"]
    is_hundred = hundred.is_hundred(raw_fmt, comp_name)
    if is_hundred:
        if match_info.get("ballsPerOver") not in (None, 5):
            return None, f"Match {mid}: conflicting Hundred ballsPerOver"

    # Dates: Multi-day expansion for Tests
    start_str = (match_info.get("startDate") or "").split("T")[0]
    dates = []
    if start_str:
        if class_meta["playing_format"] == "multi_day":
            actual_days = match_info.get("actualDays") or match_info.get("scheduledDays") or 5
            try:
                base_dt = datetime.strptime(start_str, "%Y-%m-%d")
                dates = [(base_dt + timedelta(days=d)).strftime("%Y-%m-%d") for d in range(actual_days)]
            except Exception:
                dates = [start_str]
        else:
            dates = [start_str]

    hundred_meta = hundred.match_metadata(match_info, comp_name, dates) if is_hundred else {}

    ground = match_info.get("ground") or {}
    venue_name = ground.get("name") or ground.get("smallName") or "Unknown Venue"
    venue_country = venue_map.get(venue_name) or (match_info.get("country") or {}).get("name")
    city = ground.get("town", {}).get("name") if isinstance(ground.get("town"), dict) else ground.get("town")

    id_map, players, identity_issues = build_participant_registry(
        raw_details, commentary_chunks_by_inn, player_profiles, people_registry
    )
    player_map = {internal: {"objectId": record["espn_id"], "name": record["name"]}
                  for internal, pid in id_map.items() for record in [players[pid]]}
    team_name_by_id = {(t.get("team") or {}).get("id"): (t.get("team") or {}).get("name")
                       for t in (match_info.get("teams") or []) if (t.get("team") or {}).get("id")}
    try:
        participation_events = extract_participation_events(raw_details, commentary_chunks_by_inn, id_map, players)
    except ValueError as exc:
        return None, f"Match {mid}: {exc}"
    playing_xi, playing_xi_status = confirmed_lineups(raw_details, players, participation_events)
    missing_non_striker_deliveries = 0

    # Number every scorecard innings once. Super Over innings get their own
    # numbers (source inningNumber when it is beyond regulation, else a
    # monotonic slot after max(3, last regulation innings + 1)).
    regular_innings = content.get("innings") or []
    super_over_innings = content.get("superOverInnings") or []
    max_reg_num = 0
    for idx, inn in enumerate(regular_innings):
        if inn.get("isSuperOver"):
            continue
        number = inn.get("inningNumber")
        if type(number) is not int or number < 1:
            number = idx + 1
        max_reg_num = max(max_reg_num, number)
    next_super_num = max(3, max_reg_num + 1)

    numbered_innings = []
    for idx, inn in enumerate(regular_innings):
        is_so = bool(inn.get("isSuperOver"))
        number = inn.get("inningNumber")
        if is_so:
            if type(number) is not int or number < next_super_num:
                number = next_super_num
            next_super_num = number + 1
        elif type(number) is not int or number < 1:
            number = idx + 1
        numbered_innings.append({"source": inn, "index": idx, "super_listing": False,
                                 "number": number, "is_super_over": is_so,
                                 "commentary_key": idx + 1,
                                 "path": f"/content/innings/{idx}"})
    for idx, inn in enumerate(super_over_innings):
        number = inn.get("inningNumber")
        if type(number) is not int or number < next_super_num:
            number = next_super_num
        next_super_num = number + 1
        numbered_innings.append({"source": inn, "index": idx, "super_listing": True,
                                 "number": number, "is_super_over": True,
                                 "commentary_key": ("super", idx),
                                 "path": f"/content/superOverInnings/{idx}"})

    # Dismissal fielders map
    dismissal_fielders_map = {}
    retirement_kinds = {}
    retirement_refs = {}
    for item in numbered_innings:
        inn = item["source"]
        inn_num = item["number"]
        for batter_index,b in enumerate(inn.get("inningBatsmen") or []):
            b_id = _id((b.get("player") or {}).get("id"))
            if source_facts.dismissal_text(b)=='retired not out':
                retirement_kinds[(inn_num,b_id)]='retired not out'
                retirement_refs[(inn_num,b_id)]=f'{item["path"]}/inningBatsmen/{batter_index}/dismissalText'
            fielders = []
            if b.get("dismissalFielders"):
                for f in (b.get("dismissalFielders") or []):
                    if not f or not isinstance(f, dict): continue
                    fp = f.get("player")
                    if fp and isinstance(fp, dict):
                        f_name = fp.get("name") or fp.get("longName")
                        f_obj, f_internal = _id(fp.get("objectId")), _id(fp.get("id"))
                        f_pid = id_map.get(f_internal) if f_internal is not None else None
                        if f_pid is None and f_obj is not None and f"espn_{f_obj}" in players:
                            f_pid = f"espn_{f_obj}"
                        if f_pid is None and (f_internal is not None or f_obj is not None):
                            identity_issues.append({"reason": "missing_object_id", "field": "fielder",
                                                    "source_internal_id": f_internal,
                                                    "source_object_id": f_obj, "innings_number": inn_num})
                        f_cid = (players.get(f_pid) or {}).get("cricsheet_id")
                        fielders.append({
                            "name": f_name,
                            "player_id": f_pid,
                            "cricsheet_id": f_cid
                        })
            if b_id and fielders:
                dismissal_fielders_map[(inn_num, b_id)] = fielders

    teams = [(t.get("team") or {}).get("name") for t in (match_info.get("teams") or []) if t.get("team")]

    # Process Innings & Deliveries
    enriched_innings = []
    total_balls_count = 0
    total_tracked_balls = 0
    normalization_notes = []

    for item in numbered_innings:
        inn = item["source"]
        inn_num = item["number"]
        is_so = item["is_super_over"]
        if is_hundred and inn.get("ballsPerOver") not in (None, 5):
            return None, f"Match {mid}: conflicting innings ballsPerOver"

        batting_team = (inn.get("team") or {}).get("name")
        bowling_team = teams[1] if (len(teams) > 1 and batting_team == teams[0]) else (teams[0] if teams else None)

        raw_balls = commentary_chunks_by_inn.get(item["commentary_key"]) or []
        # Deduplicate balls by id if present, preserving first occurrence
        seen_ball_ids = set()
        deduped_balls = []
        for b in raw_balls:
            b_id = b.get("id")
            if b_id:
                if b_id in seen_ball_ids:
                    continue
                seen_ball_ids.add(b_id)
            deduped_balls.append(b)
        raw_balls = deduped_balls

        # Sort ascending chronologically by oversUnique, ballNumber, or oversActual
        raw_balls.sort(key=lambda b: (
            b.get("oversUnique") if b.get("oversUnique") is not None else (
                b.get("ballNumber") if b.get("ballNumber") is not None else (
                    b.get("oversActual") or 0
                )
            )
        ))

        overs_dict = {}
        for b in raw_balls:
            if is_hundred and not re.fullmatch(r"[0-9]+\.[0-9]+", str(b.get("oversActual"))):
                return None, f"Match {mid}: invalid Hundred delivery marker for ball {b.get('id')}"
            if b.get("oversActual") is None:
                if is_hundred:
                    return None, f"Match {mid}: missing Hundred delivery marker for ball {b.get('id')}"
                continue
            ov_num = (b.get("overNumber", 1) - 1) if b.get("overNumber") else int(float(b.get("oversActual", 0)))
            overs_dict.setdefault(ov_num, []).append(b)


        hundred_innings = hundred.innings_metadata(inn, raw_balls, hundred_meta.get("ruleset"), is_so) if is_hundred else {}
        legal_balls = 0
        delivery_sequence = 0
        enriched_overs = []
        seen_dismissed_batters = set()
        prev_inning_cricinfo_total = 0

        for ov_num in sorted(overs_dict.keys()):
            ov_balls = overs_dict[ov_num]

            if db_fmt_bucket in ("T20", "T20I", "IPL"):
                phase = "powerplay" if ov_num <= 5 else ("middle" if ov_num <= 14 else "death")
            elif db_fmt_bucket == "ODI":
                phase = "powerplay" if ov_num <= 9 else ("middle" if ov_num <= 39 else "death")
            else:
                phase = "neutral"

            enriched_deliveries = []
            for b_idx, ball in enumerate(ov_balls, 1):
                source_actual=str(ball.get('oversActual'))
                actual=source_actual
                actual=source_facts.canonical_marker(source_actual,ball.get('overNumber'),
                                                      ball.get('ballNumber'),ball.get('oversUnique'))
                if actual!=source_actual:
                    normalization_notes.append({'source_ball_id':ball.get('id'),'field':'actual_delivery',
                        'source_value':source_actual,'normalized_value':actual,'rule_id':'corroborated_first_ball_marker'})
                total_balls_count += 1
                b_int_id, bw_int_id = _id(ball.get("batsmanPlayerId")), _id(ball.get("bowlerPlayerId"))
                b_info, bw_info = player_map.get(b_int_id) or {}, player_map.get(bw_int_id) or {}
                b_pid, bw_pid = id_map.get(b_int_id), id_map.get(bw_int_id)
                if b_pid is None or bw_pid is None:
                    return None, f"Match {mid}: unresolved mandatory actor at ball {ball.get('id')}: {b_int_id}/{bw_int_id}"
                source_ns_id = ball.get("nonStrikerPlayerId")
                ns_int_id = _id(source_ns_id)
                ns_pid = id_map.get(ns_int_id) if ns_int_id else None
                if ns_pid is None:
                    missing_non_striker_deliveries += 1
                    if source_ns_id is not None:
                        identity_issues.append({"reason": "missing_object_id", "field": "non_striker_id",
                                                "source_internal_id": source_ns_id, "source_ball_id": ball.get("id"),
                                                "innings_number": inn_num})

                try:
                    runs, extras_dict, note = normalize_delivery_runs(
                        ball, {"competition": comp_name, "dates": dates, "season": match_info.get("season")}
                    )
                except ValueError as exc:
                    return None, f"Match {mid}: {exc}"
                if note:
                    normalization_notes.append(note)
                bat_runs, extras_total, tot_runs = runs["batter"], runs["extras"], runs["total"]
                wides, noballs = extras_dict.get("wides", 0), extras_dict.get("noballs", 0)
                penalties = extras_dict.get("penalty", 0)

                # Detect unallocated +5 penalty runs (MCC Law 28.3, e.g. ball hit helmet)
                c_tot = ball.get("totalInningRuns")
                if c_tot is not None and prev_inning_cricinfo_total > 0:
                    step = c_tot - prev_inning_cricinfo_total
                    if step == (tot_runs + 5):
                        penalties += 5
                        extras_total += 5
                        tot_runs += 5
                        extras_dict["penalty"] = penalties

                if c_tot is not None:
                    prev_inning_cricinfo_total = c_tot

                is_legal = (wides == 0 and noballs == 0)

                # Wickets
                wickets_list = []
                if ball.get("isWicket"):
                    if ball.get("outPlayerId") is not None and _id(ball.get("outPlayerId")) is None:
                        return None, f"Match {mid}: invalid source dismissal actor {ball.get('outPlayerId')!r}"
                    out_int_id = _id(ball.get("outPlayerId")) or b_int_id
                    dtype = ball.get("dismissalType")
                    kind = DISMISSAL_KIND_MAP.get(dtype, "unknown")
                    if dtype==11 and (inn_num,out_int_id) in retirement_kinds:
                        kind=retirement_kinds[(inn_num,out_int_id)]
                        normalization_notes.append({'source_ball_id':ball.get('id'),'field':'wickets.kind',
                            'source_value':dtype,'normalized_value':kind,'rule_id':'explicit_retired_not_out',
                            'source_ref':retirement_refs[(inn_num,out_int_id)]})

                    # Deduplicate accidental duplicate wicket entries for the same batter in an innings
                    if out_int_id and out_int_id in seen_dismissed_batters and kind not in {"retired hurt", "retired not out"}:
                        pass
                    else:
                        if out_int_id and kind not in {"retired hurt", "retired not out"}:
                            seen_dismissed_batters.add(out_int_id)
                        out_info = player_map.get(out_int_id) or {}
                        out_obj_id = out_info.get("objectId")
                        out_pid = id_map.get(out_int_id)
                        if out_pid is None:
                            return None, f"Match {mid}: unresolved dismissal actor {out_int_id}"
                        out_name = out_info.get("name") or b_info.get("name")

                        fielders = dismissal_fielders_map.get((inn_num, out_int_id), [])

                        if kind == "caught" and fielders:
                            if fielders[0].get("player_id") == bw_pid:
                                kind = "caught and bowled"

                        wickets_list.append({
                            "player_out_id": out_pid,
                            "player_out_name": out_name,
                            "kind": kind,
                            "fielders": fielders
                        })

                # Tactical traits (Clean sentinels: null over fake zeros)
                wx = ball.get("wagonX")
                wy = ball.get("wagonY")
                wzone = ball.get("wagonZone")
                has_wagon = (wx is not None and wy is not None and wx > 0 and wy > 0)
                if has_wagon:
                    total_tracked_balls += 1

                tactical = {
                    "wagon_coords": [wx, wy] if has_wagon else None,
                    "wagon_zone": wzone if (wzone is not None and wzone > 0) else None,
                    "pitch_line": ball.get("pitchLine"),
                    "pitch_length": ball.get("pitchLength"),
                    "shot_type": ball.get("shotType"),
                    "shot_control": ball.get("shotControl")
                }

                # DRS Review from events
                review_obj = None
                for ev in (ball.get("events") or []):
                    if ev.get("type") == "DRS_REVIEW":
                        rev_team_id = ev.get("teamId")
                        rev_team = team_name_by_id.get(rev_team_id)
                        review_obj = {
                            "by": rev_team,
                            "batter": b_info.get("name"),
                            "bowler": bw_info.get("name"),
                            "decision": "upheld" if ev.get("isSuccessful") else "struck down",
                            "type": "drs"
                        }

                deliv_obj = {
                    "ball_number": b_idx,
                    "actual_delivery": actual,
                    "source_actual_delivery": source_actual,
                    "source_over_number":ball.get('overNumber'),
                    "source_ball_number":ball.get('ballNumber'),
                    "source_unique_marker":str(ball['oversUnique']) if ball.get('oversUnique') is not None else None,
                    "is_legal": is_legal,
                    "batter_id": b_pid,
                    "bowler_id": bw_pid,
                    "non_striker_id": ns_pid,
                    "runs": {
                        "batter": bat_runs,
                        "extras": extras_total,
                        "total": tot_runs
                    },
                    "extras": extras_dict,
                    "wickets": wickets_list,
                    "total_inning_runs": ball.get("totalInningRuns"),
                    "phase": phase,
                    "tactical": tactical,
                    "review": review_obj
                }
                if is_hundred:
                    delivery_sequence += 1
                    deliv_obj.update(delivery_sequence=delivery_sequence, legal_balls_before=legal_balls,
                                     legal_balls_after=legal_balls + int(is_legal),
                                     source_over_number=ball.get("overNumber"),
                                     source_legal_ball_marker=ball.get("ballsActual"),
                                     source_delivery_sequence=ball.get("ballsUnique"),
                                     phase=hundred.delivery_phase(legal_balls, hundred_innings),
                                     nominal_phase=hundred.nominal_phase(legal_balls, hundred_innings))
                    legal_balls += int(is_legal)
                enriched_deliveries.append(deliv_obj)

            enriched_overs.append({
                "over": ov_num,
                "deliveries": enriched_deliveries
            })

        all_deliveries=[d for ov in enriched_overs for d in ov['deliveries']]
        source_snapshot=source_facts.scorecard_snapshot(inn)
        scorecard_only = is_so and not raw_balls
        if scorecard_only:
            # ESPN publishes the Super Over as scorecard lines only. The
            # declared penalty is source-backed without ball coverage.
            declared_penalty = inn.get("penalties")
            penalty_runs = int(declared_penalty or 0)
            penalty_facts = {
                "innings_penalty_runs": penalty_runs,
                "penalty_adjustment_status": (
                    "source_backed" if penalty_runs > 0
                    else "none" if declared_penalty is not None
                    else "not_reported"
                ),
            }
        else:
            penalty_facts = source_facts.innings_penalty(all_deliveries,source_snapshot)
        enriched_innings.append({
            "innings_number": inn_num,
            "batting_team": batting_team,
            "bowling_team": bowling_team,
            "target": inn.get("target"),
            "declared": bool(inn.get("isDeclared")),
            "follow_on": bool(inn.get("isFollowOn")),
            "super_over": is_so,
            "overs": enriched_overs,
            "innings_kind": ("super_five" if (is_hundred and is_so)
                             else "super_over" if is_so else "main"),
            "include_in_standard_stats": (not is_so),
            'source_scorecard':source_snapshot,'source_legal_balls':inn.get('balls'),
            'penalty_adjustment_source':f'{item["path"]}/penalties',
            **penalty_facts,
            **(dict(hundred_innings, legal_balls=legal_balls, delivered_balls=delivery_sequence,
                    source_counter_coverage={
                        "delivery_sequence":sum(type(b.get("ballsUnique")) is int for b in raw_balls),
                        "legal_position":sum(type(b.get("ballsActual")) is int for b in raw_balls)}) if is_hundred else {}),
            **({"scorecard_only_details": scorecard_only_details(inn, id_map, players)} if scorecard_only else {})
        })

    # Toss & Outcome
    toss_winner_team = match_info.get("tossWinnerTeamId")
    toss_winner_name = team_name_by_id.get(toss_winner_team)
    toss_choice = match_info.get("tossWinnerChoice")
    toss_decision = "field" if toss_choice == 2 else ("bat" if toss_choice == 1 else None)

    winner_team_id = match_info.get("winnerTeamId")
    winner_name = team_name_by_id.get(winner_team_id)
    tiebreaker_team_id = match_info.get("tiebreakerTeamId")
    tiebreaker_name = team_name_by_id.get(tiebreaker_team_id)
    status_text = match_info.get("statusText") or ""
    status_lang_key = str(
        ((match_info.get("statusData") or {}).get("statusTextLangKey")) or ""
    ).lower()

    win_by_runs = None
    win_by_wkts = None
    win_by_innings = None
    result = None
    eliminator = None
    method = None
    decider_method = None

    if "dls" in status_text.lower() or "d/l" in status_text.lower():
        method = "DLS"

    m_inns = re.search(r'won by an innings and (\d+) run', status_text, re.I)
    if m_inns:
        win_by_innings = 1
        win_by_runs = int(m_inns.group(1))
    else:
        m_runs = re.search(r'won by (\d+) run', status_text, re.I)
        if m_runs: win_by_runs = int(m_runs.group(1))
        m_wkts = re.search(r'won by (\d+) wicket', status_text, re.I)
        if m_wkts: win_by_wkts = int(m_wkts.group(1))

    if "tied" in status_text.lower():
        result = "tie"
        eliminator = tiebreaker_name or winner_name
        # Decided ties keep outcome.winner null; the deciding team lives in
        # outcome.eliminator and the method in outcome.decider_method.
        winner_name = None
        lowered = status_text.lower()
        if status_lang_key == "match_status_text_tied_tiebreaker_on_second_super_over" or "2nd super over" in lowered:
            decider_method = "second_super_over"
        elif status_lang_key == "match_status_text_tied_tiebreaker_on_boundary_count" or "boundary count" in lowered:
            decider_method = "boundary_count"
        elif status_lang_key == "match_status_text_tied_tiebreaker_won" or "one-over eliminator" in lowered:
            decider_method = "one_over_eliminator"
        elif is_hundred and eliminator:
            decider_method = "super_five"
        elif status_lang_key == "match_status_text_tied_tiebreaker_on_super_over" or (eliminator and match_info.get("isSuperOver")):
            decider_method = "super_over"
        elif eliminator and not match_info.get("isSuperOver") and "one-over eliminator" not in lowered:
            decider_method = "bowl_out"
    elif "no result" in status_text.lower() or "abandoned" in status_text.lower():
        result = "no result"
        winner_name = None
    elif "drawn" in status_text.lower():
        result = "draw"
        winner_name = None

    # Officials in DB format
    umpires = [u.get("player", {}).get("name") for u in (match_info.get("umpires") or []) if u and isinstance(u.get("player"), dict) and u.get("player", {}).get("name")]
    tv_umpire = next((u.get("player", {}).get("name") for u in (match_info.get("tvUmpires") or []) if u and isinstance(u.get("player"), dict) and u.get("player", {}).get("name")), None)
    match_referee = next((u.get("player", {}).get("name") for u in (match_info.get("matchReferees") or []) if u and isinstance(u.get("player"), dict) and u.get("player", {}).get("name")), None)

    db_playing_xi = dict(playing_xi)
    db_playing_xi["_umpires"] = umpires
    db_playing_xi["_referee"] = match_referee

    tactical_coverage = round(total_tracked_balls / total_balls_count, 3) if total_balls_count > 0 else 0.0

    raw_bpo = match_info.get("ballsPerOver")
    if raw_bpo and isinstance(raw_bpo, int) and raw_bpo > 0:
        balls_per_over = raw_bpo
    elif is_hundred:
        balls_per_over = 5
    else:
        balls_per_over = 6

    match_v3 = {
        "match_id": str(mid),
        "meta": {
            "schema_version": "3.3.0",
            "format": meta_fmt,
            "format_bucket": db_fmt_bucket,
            "competition": comp_name,
            "season": match_info.get("season"),
            "dates": dates,
            "venue": venue_name,
            "venue_country": venue_country,
            "city": city,
            "gender": "male",
            "day_night": "day/night" if match_info.get("dayNight") else "day",
            **derive_stage(match_info),
            **derive_player_of_match(content),
            **derive_player_of_series(content),
            **derive_series_id(match_info),
            "balls_per_over": balls_per_over,
            "tactical_coverage": tactical_coverage,
            "normalization_notes": normalization_notes,
            "identity_issues": identity_issues,
            "playing_xi_status": playing_xi_status,
            "missing_non_striker_deliveries": missing_non_striker_deliveries,
            "is_super_over": bool(match_info.get("isSuperOver")),
            "status_text": status_text or None,
            **hundred_meta,
            **class_meta
        },
        "outcome": {
            "winner": winner_name,
            "win_by_runs": win_by_runs,
            "win_by_wickets": win_by_wkts,
            "win_by_innings": win_by_innings,
            "result": result,
            "eliminator": eliminator,
            "method": method,
            "decider_method": decider_method
        },
        "toss": {
            "winner": toss_winner_name,
            "decision": toss_decision
        },
        "playing_xi": db_playing_xi,
        "players": players,
        "participation_events": participation_events,
        "innings": enriched_innings
    }

    return match_v3, None
