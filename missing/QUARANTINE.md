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

## Disjoint groups, by primary cause

Each match is assigned once, in this order. "Single-cause" is the count whose
error list contains only that cause.

| Group | Matches | Single-cause | Why it is withheld | Best way to allow |
| --- | ---: | ---: | --- | --- |
| A. Commentary running total only | 1,273 | 1,273 | The per-ball `total_inning_runs` disagrees mid-innings, but every innings' final total equals the scorecard; all other checks pass | Admit with a warning; keep the source field, add `validation_warnings` |
| B. Missing non-striker id only | 76 | 76 | `identity_issues` records a missing `non_striker_id`; a fresh revalidation also finds over/legal-ball defects in these files | Null the field and count it; repair the over grouping before admitting (they fall into G/H) |
| C. Commentary + non-striker id | 1 | 0 | Both A and B | Same two fixes |
| D. Missing deliveries | 28 | 0 | A scorecard innings with runs/wickets has no or too few deliveries | Re-fetch the innings; not repairable from the parsed archive |
| E. Unknown dismissal | 15 | 8 | `dismissalType=unknown` is not in the map | Map from the scorecard/commentary, then revalidate |
| F. Penalty review | 85 | 0 | `source_facts` cannot settle a penalty component | Apply the scorecard-proven penalty adjustment, then revalidate |
| G. Over / legal-ball grouping | 247 | 6 | An over contains an impossible legal-ball count | Re-bucket deliveries by the source markers; most also carry H |
| H. Scorecard totals / coverage | 428 | 79 | Runs, wickets, legal balls or extras disagree with the scorecard | Separate penalty/parse errors from genuine commentary defects; repair or leave flagged |
| I. Sequence / marker | 16 | 15 | Non-monotonic sequence or an uncorroborated delivery marker | Manual review against the source |

The recorded error lists were often truncated, so the authoritative test is a
fresh run of the current validator. That run admits **1,289 matches (A 1,288 +
C 1)** and leaves **880** quarantined. The 76 "identity-only" matches also fail
over/legal-ball checks that were absent from their stored error lists, so they
belong with G/H until those are repaired.

## Recommended publication policy

1. **Warned, not failed.** Add `validation_warnings` and set
   `validation_status="warning"` for A–C. Keep every source field unchanged;
   do not silently rewrite the commentary total. Publish them in the normal
   slices with the warning visible in `lake_manifest.json` and README.
2. **Repair what the source proves.** Work E → F → G, in that order, with a
   before/after validation report per group. H is the residual bucket; only the
   penalty/parse subset should move.
3. **Re-fetch D and manually review I.** These 44 matches are the least likely
   to recover; do not guess at missing deliveries.
4. **Flagged tier for the rest.** If H still contains genuine source defects,
   publish an explicitly labelled opt-in group with `validation_status` and
   errors intact. Never mix it into the clean slices.
5. **Rebuild and verify.** Revalidate, run `consolidate.py --published-only`,
   rebuild the master and then the catalog. Compare `quarantined_matches`,
   `matches_index.csv` row count and each slice's manifest before/after.


## The 880 that still fail after the warn tier

This grouping comes from the current validator (stored error lists were often
truncated). Each match is counted once by its first blocker; single-cause is
the count whose only failure is that blocker.

| Primary blocker | Matches | Single-cause | Next repair |
| --- | ---: | ---: | --- |
| Missing deliveries | 28 | 0 | Re-fetch the innings; no offline repair |
| Unknown dismissal | 15 | 9 | Map `dismissalType` from the scorecard/commentary |
| Penalty component review | 177 | 0 | Apply only a scorecard-proven penalty adjustment |
| Over / legal-ball grouping | 245 | 6 | Re-bucket deliveries by source markers |
| Scorecard wickets | 178 | 74 | Verify non-delivery dismissals and wicket attribution |
| Scorecard runs | 91 | 0 | Separate penalty/parse errors from commentary defects |
| Source legal-ball coverage | 108 | 72 | Recompute legal balls; check wides/no-balls |
| Source exact runs | 19 | 12 | Recompute runs; check penalties and byes |
| Extras breakdown | 18 | 18 | Recompute wides/no-balls/byes/legbyes |
| Sequence / marker | 1 | 0 | Manual review |

Some causes overlap heavily: 721 matches have a source legal-ball coverage
failure, 646 a source exact-runs failure, 533 a scorecard-runs failure, 493 a
scorecard-wickets failure, 387 a remaining running-score failure, 371 an over
grouping failure, 199 extras and 179 penalties. The **191 single-cause matches**
(74 wickets, 72 legal coverage, 18 extras, 12 exact runs, 9 dismissals, 6 over)
are the cheapest next tranche because only one check has to be repaired.

The deep multi-cause signatures — for example
`over+scorecard_runs+scorecard_wkts+source_legal+source_runs` — are the least
likely to recover and should be the flagged tier if the targeted pass does not
close them.

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
   field. A fresh run of the modified validator over the archive admits
   **1,289 matches** and leaves **880** quarantined. The stored error lists are
   not a reliable admission test.
2. **Normalise missing non-striker ids.** Leave `non_striker_id` null, increment
   `missing_non_striker_deliveries`, and do not turn the absence into a hard
   `identity_issue`. This is already tolerated in the clean corpus (4,607
   deliveries), so it is a consistency fix, not a new allowance.
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
- Expected receipts: quarantined 2,169 → **880** (current validator after the
  warning policy), and the clean lake 16,856 → **18,145**. Do not claim these
  until the rerun reports them.
