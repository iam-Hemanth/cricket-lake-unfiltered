# Quarantine audit — 2,169 matches (2026-11-09)

Source: `matches_batch_recovery_20261025.tar.gz` on the `data-unfiltered-batches`
release. Every match below carries `meta.validation_status = "failed"` and is
excluded from the public catalog; none of the ids appear in the published
`matches_index.csv` (16,856 rows). The archive also contains 217 clean matches
that were already admitted.

## Who is excluded

| Slice | Matches |
| --- | --- |
| Male | 2,152 |
| Female | 6 |
| Gender unknown | 11 |
| **Total** | **2,169** |

## Why — match-level causes (a match can have several)

| Cause | Matches |
| --- | ---: |
| Running-score mismatch against the commentary feed | 1,748 |
| Source exact legal-ball total mismatch | 645 |
| Source exact runs mismatch | 566 |
| Source scorecard legal-ball coverage mismatch | 561 |
| Source scorecard exact runs mismatch | 476 |
| Scorecard runs mismatch (>2) | 427 |
| Scorecard wickets mismatch | 387 |
| Over has an impossible legal-ball count | 281 |
| Missing non-striker identity | 260 |
| Extras total mismatch | 137 |
| Penalty component needs review | 87 |
| Incomplete deliveries for a nonzero scorecard innings | 28 |
| Unrecognised dismissal kind | 15 |
| Non-monotonic delivery sequence | 1 |
| Uncorroborated delivery marker | 1 |

## The important split

- **1,288 matches fail only on the running-score check.** In 1,273 of them the
  final computed innings totals equal the scorecard exactly; only an
  intermediate `total_inning_runs` value disagrees (most often at 19.6, by 4
  runs — the boundary-at-end-of-over pattern). These deliveries are internally
  consistent and reconcile to the scorecard; the commentary running total is
  the outlier.
- **15 of the running-score-only matches also disagree with the scorecard final
  total** and are not safe to admit without source review.
- **76 matches fail only on a missing non-striker id.** The published corpus
  already tolerates missing non-striker ids elsewhere (4,607 deliveries in the
  clean set); these are being rejected because the extraction recorded the
  absence in `meta.identity_issues` instead of leaving the field null and
  counting it.
- **The remaining ~805 matches are mixed defects:** scorecard runs/wickets,
  legal-ball coverage, extras, penalty review, incomplete innings, unknown
  dismissals. Some are parser bugs (penalty runs, over grouping); some are
  genuine upstream defects.

## Latest window is also missing 9 matches

The 2026-11-09 05:01 UTC `Update lake (6-hourly)` run discovered 9 matches and
all 9 failed validation, so no delta was published. They are dated 7–8 Nov and
are absent from the catalog (which otherwise covers through 2026-11-08):

| Match | Date | Competition | Failure |
| --- | --- | --- | --- |
| 1554097 | 2026-11-07 | CSA Women Pro50 | legal balls + exact runs + no deliveries |
| 1554098 | 2026-11-07 | CSA Women Pro50 | legal balls + exact runs + no deliveries |
| 1558435 | 2026-11-07 | U19 Asia Cup | running score only |
| 1558599 | 2026-11-07 | Nepal Premier League | running score only |
| 1561358 | 2026-11-07 | England tour of Australia | legal balls + running score + wickets |
| 1554068 | 2026-11-08 | CSA Women Pro20 | legal balls + exact runs + no deliveries |
| 1555414 | 2026-11-08 | Hong Kong Premier League | legal balls + exact runs + 2-ball over |
| 1558436 | 2026-11-08 | U19 Asia Cup | missing non-striker id only |
| 1559692 | 2026-11-08 | Pro50 Championship | exact runs + running score (DLS) |

The failed run does not upload raw payloads, so these must be re-fetched before
they can be repaired. The 3 women's matches are withheld from downloads by the
v1 policy regardless.

## Repair plan, in order

1. **Admit scorecard-consistent running-total discrepancies.** In
   `src/validator.py`, demote the cumulative `total_inning_runs` comparison to a
   warning when the innings' final runs, wickets, legal balls and extras all
   reconcile. Annotate `meta.validation_warnings`; do not rewrite the source
   field. Revalidate the recovery archive. Expected clean gain: **1,273**
   (catalog 16,856 → 18,129 before dedupe).
2. **Normalise missing non-striker ids.** Leave `non_striker_id` null, increment
   `missing_non_striker_deliveries`, and do not turn the absence into a hard
   `identity_issue`. Expected gain: **+76** pure matches (plus 1558436).
3. **Penalty and grouping audit (next ~150–250).** Recompute the 87
   `needs_review` penalties and the 281 impossible-over cases against the
   scorecard. Penalty runs (5 runs, no delivery) explain a meaningful share of
   the exact-runs and extras mismatches. Verify each against the source before
   changing an adjustment.
4. **Targeted re-fetch for the rest.** Generate a work queue with match id,
   competition, computed vs scorecard totals and error text; re-extract the 28
   incomplete innings and the scorecard mismatches, then revalidate. Do not
   backfill by hand.
5. **Optional flagged tier.** If the residual defects cannot be repaired,
   publish a clearly labelled opt-in group (not mixed into clean slices) with
   `validation_status` and `validation_errors` intact. This is the only honest
   way to expose all 2,169; it does not make them clean.

## Rebuild and verification

- Revalidate recovered files with `validate_match_data` after each change; keep
  the original archive as the before state.
- Re-run `consolidate.py --published-only` and check
  `coverage.quarantined_matches` before/after.
- Rebuild the master, then the catalog; verify the new ids appear in
  `matches_index.csv` and that each slice's manifest count adds up.
- Expected receipts: quarantined 2,169 → 820 (after steps 1–2, before step 3),
  and the clean lake 16,856 → 18,205. Do not claim these until the rerun
  reports them.
