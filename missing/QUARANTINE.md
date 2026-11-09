# Quarantine audit — 2,169 matches (2026-11-09)

Source: `matches_batch_recovery_20261025.tar.gz` on the `data-unfiltered-batches`
release. Every match below carried `meta.validation_status = "failed"` in that
batch. As of 09 Nov 2026, **1,289 have been admitted to the published lake** as
`validation_status = "warning"` (their recorded warnings are preserved), **230
more as `validation_status = "source_gap"`**, and the master grew from 16,856 to
**18,381 matches**. **650 remain quarantined.** This document's first half is
the original pool analysis; the second half is the measured state of the **880
that still fail**, followed by the source-gap tier and the two re-fetch passes.
The archive also contains 217 clean matches that were already admitted.

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

## Re-fetch run 42630708983 and the 77 admits (09 Nov 2026)

The 20-slot re-fetch (`Re-fetch quarantined matches`, run 42630708983) finished
at 14:59 UTC; its merge job published `refetch-results` with **552 pass / 328
still-failing** files. One measurement matters before reading those numbers:

- The workflow's "List quarantined ids" step selected on the **stored**
  `validation_status == "failed"` in the recovery batch (2,169 ids), not on a
  fresh validation. The 20 x 44 matrix therefore covered only the **first 880
  ids in sorted order**: 552 of them are warn-tier matches already published in
  `matches_batch_warned_*`, and 328 are genuinely still-quarantined.
- The other **552 still-quarantined ids were not re-fetched**: they are the
  quarantined subset of the 1,289 stored-failed ids after position 880. The
  552 "passes" are fresh copies of already-published matches and add **zero**
  new ids to the lake.
- The id filter in `refetch_quarantine.yml` now revalidates each stored-failed
  file and selects only the still-failing ones: on this batch it yields exactly
  **880**, agreeing with `failed - warned`. A guard fails the run if the count
  exceeds the 20 x 44 matrix.

### Source-gap admission (run 42642702549)

`apply_source_gap.yml` with `refetch_run_id=42630708983` admitted **77 of the
328** still-failing files. Rejections: 176 disallowed error kinds, 67 summed
delta above the 2-ball / 2-run / 1-wicket threshold, 8 over-length overs with
no adjacent pair.

- Asset: `matches_batch_sourcegap_20261109T151527Z-42642702549.tar.gz`
  (2,632,935 bytes; sha256 verified; 77 files).
- Every admitted file carries `validation_status = "source_gap"`,
  `meta.validation_warnings` and `meta.source_gap`; `meta.validation_errors` is
  cleared.
- Format mix: FC 40, T20 22, List A 6, other T20 4, T20I 3, other multi-day 1,
  other one-day 1.
- Delta shapes: 33 one legal ball; 27 one wicket; 4 one ball + one wicket; 2
  zero-delta (extras split / dismissal label only); the rest at most 2 balls /
  2 runs / 1 wicket.

### Final counts (consolidate run 42643051903, catalog run 42646699210)

| Measure | Count |
| --- | ---: |
| Master matches | **18,227** (18,145 + 5 delta + 77 source-gap) |
| `coverage.quarantined_matches` | **803** (880 - 77) |
| `coverage.warning_matches` | **1,289** |
| `coverage.source_gap_matches` | **77** |
| `matches_index.csv` statuses | 16,861 clean / 1,289 warning / 77 source-gap |
| Date range | 2001-12-19 to 2026-11-09 |

The catalog adds deltas on top of the master, so run 42646699210 publishes
**18,228** records: the 18,227 above plus match 1558601 from the 15:30 UTC
6-hourly delta (run 42644785493), which landed after the consolidate had
already downloaded its deltas. The next consolidate folds it into the master.

### Residual after this pass

- **552 still-quarantined ids were never re-fetched** by run 42630708983
  (the id-filter bug above). They need one corrected re-fetch pass.
- **251 of the 328** re-fetched files still fail the source-gap policy (176
  disallowed error kinds, 67 over threshold, 8 over-length).
- The 5 delta-window ids that failed on 7-8 Nov (1561358, 1554068, 1555414,
  1558436, 1559692) were never in the recovery batch and remain missing.

## Corrected re-fetch (run 42649214897) and the 230 admits (09 Nov 2026)

The corrected filter selected exactly **880** still-failing ids (verified from
`quarantine_ids.txt`), and all 20 chunks ran in parallel. The merge job
published `refetch-results` with **0 pass / 880 still-failing**.

- A fresh extraction of a genuinely failing match reproduces the failure:
  these are source defects, not truncated original parses. Every "pass" in the
  previous run was an already-published warn-tier match; no quarantined match
  has been recovered by re-fetch.
- The source-gap gate admits **230 of the 880** — 97 FC, 59 T20, 31 List A,
  25 T20I, 18 other — with 475 disallowed error kinds, 156 summed deltas above
  threshold and 19 over-length overs. This is exactly the count the pre-refetch
  estimate predicted.
- 77 of the 230 were already published in run 42642702549; **153 are new**.
- Asset: `matches_batch_sourcegap_20261109T164604Z-42657718454.tar.gz`
  (6,767,900 bytes; sha256 verified; 230 files, all with the status, warning
  list and delta record). It sorts after the 77-asset, so it wins for the
  overlapping ids.

### Final counts after the second pass

| Measure | Count |
| --- | ---: |
| Master matches | **18,381** (18,227 + 1 delta + 153 new source-gap) |
| `coverage.quarantined_matches` | **650** (880 - 230) |
| `coverage.warning_matches` | **1,289** |
| `coverage.source_gap_matches` | **230** |
| `matches_index.csv` statuses | 16,862 clean / 1,289 warning / 230 source-gap |
| Date range | 2001-12-19 to 2026-11-09 |

### Residual after the second pass

- **650 quarantined**, all re-fetched today and still failing: 475 carry a
  disallowed error kind, 156 exceed the small-delta threshold, 19 have
  over-length overs with no adjacent pair.
- The 5 delta-window ids that failed on 7-8 Nov (1561358, 1554068, 1555414,
  1558436, 1559692) remain missing; they were never in the recovery batch.
- The four abandoned ids 1554097/1554098 (women's) and 1558435/1558599
  (running-score only) still need source data.

## Latest window is also missing 9 matches

The 2026-11-09 05:01 UTC `Update lake (6-hourly)` run discovered 9 matches and
all 9 failed validation, so no delta was published. Five were rediscovered by
the 12:58 run and failed again; the catalog now covers through 2026-11-09, but
these 9 ids are still absent:

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
3. **Re-fetch the 880 (done).** `refetch_quarantine.yml` re-downloads the
   still-quarantined ids with the production extractor, publishes only the
   files that validate cleanly as `matches_batch_refetch_*.tar.gz`, and leaves
   the rest in quarantine with a failure artifact. Run 42630708983 covered only
   the first 880 stored-failed ids (552 of its passes were already-published
   warn-tier files and 552 quarantined ids were missed); the corrected filter
   then covered the true 880 in run 42649214897 with 0 pass / 880 fail.
4. **Decide the small-delta tier from the re-fetch evidence (done).** The gate
   admitted 230 of the 880 as `source_gap` (runs 42642702549 and 42657718454)
   with `validation_status`, the explicit warning list and `meta.source_gap`;
   650 rejected. The 153 new admits are published.
5. **Leave the large-gap residual quarantined.** The current residual is **650**
   matches, all re-fetched today and still failing. Missing overs or innings
   cannot be repaired by a waiver; keep them in the recovery batch and list
   them in the audit rather than publishing partial games as if complete.

## Rebuild and verification

- Revalidate recovered files with `validate_match_data` after each change; keep
  the original archive as the before state.
- Re-run `consolidate.py --published-only` and check
  `coverage.quarantined_matches` before/after.
- Rebuild the master, then the catalog; verify the new ids appear in
  `matches_index.csv` and that each slice's manifest count adds up.
- Measured receipts: quarantined 2,169 → **880** (current validator after the
  warning policy) → **650** after the 230 source-gap admits; the clean lake
  16,856 → **18,145** (warn tier) → **18,381** (plus deltas and the 230
  source-gap admits). Confirmed in `lake_manifest.json` (run 42658234882).
