#!/usr/bin/env python3
"""Admit source-gap matches: complete ball-by-ball, small scorecard disagreement.

The strict validator is right to reject a match whose ball-by-ball totals do
not agree with the source scorecard. But a match that is otherwise complete and
disagrees by one wicket, one or two legal balls, or one or two runs is not
missing data: the two sources describe the same game slightly differently.

This policy admits only that narrow shape:
  * no structural error (incomplete innings/deliveries, bad sequence/marker);
  * no running-total, penalty, identity or snapshot error;
  * every innings has a source snapshot;
  * summed absolute deltas <= 2 legal balls, <= 2 runs, <= 1 wicket;
  * over-length errors only where adjacent overs pair back to 2x balls_per_over
    (or a single over holds exactly 2x, i.e. two source overs merged).

The admitted file keeps validation_status="source_gap" and the exact error
list in meta.validation_warnings. Nothing is rewritten or backfilled.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.validator import validate_match_data

SKIP_NAMES = {"lake_manifest.json", "manifest.json", "meta.json", "failed_matches.json"}


def _allowed_error(message: str) -> bool:
    prefixes = (
        "Scorecard wickets mismatch:",
        "Source scorecard legal-ball coverage mismatch",
        "Source scorecard exact runs mismatch",
        "Total extras mismatch:",
        "Unrecognized dismissal kind:",
        "Scorecard runs mismatch:",
    )
    if message.startswith(prefixes):
        return True
    return message.startswith("Inn ") and "legal balls (expected" in message


def _over_lengths_ok(innings: list[dict], balls_per_over: int) -> tuple[bool, str]:
    for inn in innings:
        counts = [
            sum(1 for delivery in over.get("deliveries") or [] if delivery.get("is_legal"))
            for over in inn.get("overs") or []
        ]
        if not counts:
            continue
        for index, count in enumerate(counts):
            if count == balls_per_over:
                continue
            if index == len(counts) - 1:
                continue  # a short final over is normal
            if balls_per_over - 1 <= count <= balls_per_over + 2:
                continue  # the validator tolerates this range for a non-final over
            if count == 2 * balls_per_over:
                continue  # two source overs merged under one number
            paired = (
                (index > 0 and counts[index - 1] + count == 2 * balls_per_over)
                or (index + 1 < len(counts) and count + counts[index + 1] == 2 * balls_per_over)
            )
            if not paired:
                return False, f"inn {inn.get('innings_number')} over {index} has {count} legal balls with no adjacent pair"
    return True, ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--summary", type=Path, default=None)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    admitted: list[str] = []
    reasons: Counter[str] = Counter()
    skipped = 0

    for path in sorted(args.input_dir.glob("*.json")):
        if path.name in SKIP_NAMES:
            continue
        try:
            match = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            reasons["unreadable"] += 1
            continue
        meta = match.get("meta") or {}
        if meta.get("validation_status") != "failed":
            skipped += 1
            continue
        errors = validate_match_data(match)
        if not errors:
            reasons["passes_strict_validator"] += 1
            continue
        if any(not _allowed_error(error) for error in errors):
            reasons["disallowed_error"] += 1
            continue

        total_legal = total_runs = total_wickets = 0
        usable = True
        for inn in match.get("innings") or []:
            snapshot = inn.get("source_scorecard") or {}
            if snapshot.get("balls") is None or snapshot.get("runs") is None or snapshot.get("wickets") is None:
                usable = False
                break
            deliveries = [d for over in inn.get("overs") or [] for d in over.get("deliveries") or []]
            legal = sum(1 for d in deliveries if d.get("is_legal"))
            runs = sum(d.get("runs", {}).get("total", 0) for d in deliveries) + inn.get("innings_penalty_runs", 0)
            wickets = sum(len(d.get("wickets") or []) for d in deliveries)
            total_legal += abs(legal - snapshot["balls"])
            total_runs += abs(runs - snapshot["runs"])
            total_wickets += abs(wickets - snapshot["wickets"])
        if not usable:
            reasons["missing_source_snapshot"] += 1
            continue
        if total_legal > 2 or total_runs > 2 or total_wickets > 1:
            reasons["delta_above_threshold"] += 1
            continue
        over_ok, over_reason = _over_lengths_ok(match.get("innings") or [], int(match["meta"]["balls_per_over"]))
        if not over_ok:
            reasons[over_reason] += 1
            continue

        match_id = str(match.get("match_id") or meta.get("match_id") or path.stem)
        meta["validation_status"] = "source_gap"
        meta["validation_warnings"] = [str(error) for error in errors]
        meta["validation_errors"] = []
        meta["source_gap"] = {
            "legal_ball_delta": total_legal,
            "runs_delta": total_runs,
            "wicket_delta": total_wickets,
            "error_count": len(errors),
        }
        (args.out_dir / f"{match_id}.json").write_text(
            json.dumps(match, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        admitted.append(match_id)

    summary = {
        "admitted": len(admitted),
        "rejected": sum(reasons.values()),
        "skipped_non_failed": skipped,
        "rejected_reasons": dict(reasons.most_common()),
        "admitted_ids": admitted,
    }
    if args.summary:
        args.summary.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items() if key != "admitted_ids"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
