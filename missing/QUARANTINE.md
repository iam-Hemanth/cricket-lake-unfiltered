# Quarantine audit — 2,169 matches (2026-11-09)

Source: `matches_batch_recovery_20261025.tar.gz` on the `data-unfiltered-batches`
release. Every match below carried `meta.validation_status = "failed"` in that
batch. As of 09 Nov 2026, **1,289 have been admitted to the published lake** as
`validation_status = "warning"` (their recorded warnings are preserved), and the
master grew from 16,856 to **18,145 matches**. This document's first half is the
original pool analysis; the second half is the measured state of the **880 that
still fail**, followed by the source-gap tier. The archive also contains 217
clean matches that were already admitted.

## Who was in the quarantine pool

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

Re-run of the current validator over the recovery batch (09 Nov 2026). The
earlier primary-cause table is kept conceptually but is superseded by this
measurement: a match usually fails several checks, so grouping by "first"
blocker hid how small the underlying gap is.

| Measured shape | Matches | What it means |
| --- | ---: | --- |
| 1 root cause only | **196** | 74 wickets, 72 legal balls, 18 extras, 16 runs, 9 unknown dismissals, 6 over boundaries, 1 sequence |
| All deltas small | **270** | every innings within 2 legal balls / 2 runs / 1 wicket; no structural or running-score error |
| Large source gap | **610** | at least one innings short by 3+ balls or 3+ runs; includes the 28 incomplete innings and 1 non-monotonic sequence |

Overlapping totals: 721 matches fail source legal-ball coverage, 646 source
exact runs, 533 scorecard runs (>2), 493 scorecard wickets, 371 over grouping,
199 extras, 179 penalty-component review. Those are check counts, not match
causes — the 270 small-delta matches are the only ones that can be considered
for admission without new source data. The 610 large-gap matches need a
re-fetch; an offline waiver would hide missing overs.

The 196 single-root matches are the cheapest tranche: 74 have exactly one
missing scorecard wicket, 72 are one legal ball out, 18 disagree only in the
extras split (final runs reconcile), 16 differ by 1–2 final runs, 9 carry an
`unknown` dismissal label, and 6 have a shifted over boundary (e.g. 3+9 legal
balls across two overs).

## Source-gap tier (09 Nov 2026)

A still-failing match is admitted as `validation_status = "source_gap"` only
when the validator's every error is one of: scorecard wickets, legal-ball
coverage, exact runs, extras breakdown, unknown dismissal kind, or an over
length explained by an adjacent pair. In addition, summed per-innings deltas
must be at most 2 legal balls, 2 runs and 1 wicket, every innings must have a
source snapshot, and there must be no structural, running-total, penalty or
identity error. The file keeps the exact error list in
`meta.validation_warnings` and the deltas in `meta.source_gap`; nothing is
backfilled. On the pre-refetch batch this admits **230 of the 880** (97 FC,
59 T20, 31 List A, 25 T20I, 18 other formats). The remainder is larger than a
source disagreement and stays quarantined.

The admission batch is `matches_batch_sourcegap_*.tar.gz`. Its name sorts after
the recovery and refetch batches, so the consolidated master takes these files
for the same match id.

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
3. **Re-fetch the 880 (in progress).** `refetch_quarantine.yml` re-downloads
   every still-quarantined id with the production extractor, publishes only the
   files that validate cleanly as `matches_batch_refetch_*.tar.gz`, and leaves
   the rest in quarantine with a failure artifact. This is the only honest fix
   for the 610 large-gap matches; it may also close some of the 270 small-delta
   matches without a policy change.
4. **Decide the small-delta tier from the re-fetch evidence.** If a match still
   fails with all deltas within 2 legal balls / 2 runs / 1 wicket and no
   structural or running-score error, it is a source disagreement, not missing
   data. Admit it only with `validation_status` plus the explicit warning list,
   and label the count in the slice READMEs — never in the clean tier silently.
5. **Leave the large-gap residual quarantined.** 610 matches with missing overs
   or innings cannot be repaired by a waiver. Re-fetch first; if the source
   still lacks the deliveries, keep them in the recovery batch and list them in
   the audit rather than publishing partial games as if complete.

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
