# Lake handoff — 9 Nov 2026 (source-gap tier admitted)

This is the handoff for **this repository only** (`cricket-lake-unfiltered`).
The CricStats app/API lives in a different repo (`/Users/hemanth/cstest`) and
has its own handoff; do not apply its runbooks, ports or database rules here.

## Current published state

- Catalog: https://iam-hemanth.github.io/cricket-lake-unfiltered/
- **18,228 matches / 197 slices / 3.0 GB** (catalog run 42646699210, generated
  15:38 UTC). The master release is at **18,227** (run 42643051903); the one
  extra record is delta match 1558601 published at 15:30 UTC, after the
  consolidate had downloaded its deltas. The next consolidate folds it in.
- **1,289 warning-tier** and **77 source-gap-tier** matches are included; their
  caveats are in `meta.validation_warnings` (source-gap files also carry
  `meta.source_gap`). **803 quarantined** remain excluded.
- `matches_index.csv` now exposes `validation_status`: 16,861 clean / 1,289
  warning / 77 source-gap.
- Date range **2001-12-19 to 2026-11-09**.

## What ran (09 Nov)

1. **Re-fetch 42630708983** (14:01–14:59 UTC, 20 x 44): the merge job published
   `refetch-results` with **552 pass / 328 still-failing**. The id filter
   selected the first 880 *stored*-failed ids, so all 552 passes were warn-tier
   matches already published and **552 quarantined ids were missed**. The pass
   files add **zero** new ids.
2. **Source-gap admission 42642702549**: 77 of the 328 admitted; asset
   `matches_batch_sourcegap_20261109T151527Z-42642702549.tar.gz` (sha256
   verified; every file carries the status, warning list and delta record).
3. **Consolidate 42643051903**: master 18,227; quarantined 803; warning 1,289;
   source_gap 77; the rebuilt index carries the status column.
4. **Catalog 42646699210**: 18,228 records (adds the 15:30 delta 1558601), with
   the warning/source-gap/quarantined counts in the page notes and slice
   READMEs.

## Next steps

1. **Dispatch the corrected re-fetch.** `refetch_quarantine.yml` now
   revalidates each stored-failed file and selects only the still-failing ids
   (verified locally: exactly 880 for the recovery batch). Run it to cover the
   552 quarantined ids that run 42630708983 missed. Then run
   `apply_source_gap.yml` with the new run id, consolidate, rebuild the catalog.
2. The 251 of 328 re-fetched failures stay quarantined (large source gaps).
3. The 5 delta-window ids from 7–8 Nov (1561358, 1554068, 1555414, 1558436,
   1559692) remain missing; they were never in the recovery batch. The four
   older ids 1554097/1554098 (women's) and 1558435/1558599 (running-score
   only) still need source data.

## Rules specific to this repo

- Pushes to `iam-Hemanth/cricket-lake-unfiltered` are expected; **never push
  `/Users/hemanth/cstest`** (local only) and do not touch
  `/Users/hemanth/cricket-stats`.
- The gap batch is named `matches_batch_sourcegap_*` so it unpacks after the
  recovery and refetch batches. The lexicographic order in
  `consolidate_full_lake.yml` is the conflict policy:
  `recovery` < `refetch` < `sourcegap` < `warned`. The warned batch wins for
  the 552 re-fetched warn-tier ids, so the published warning status is intact.
- The 552 refetch pass assets from run 42630708983 duplicate already-published
  matches; do not count them as recoveries.
- No third-party storage, no card, no database writes are involved here.

## Residual to explain, not hide

- 803 quarantined = 552 never re-fetched + 251 re-fetched but still failing.
- Two undated records (1535685, 1551344) make the master `README.txt` print
  "Date Range: 2001-12-19 to Unknown"; the manifest and catalog date ranges are
  correct.
- The catalog is master + deltas by design, so a delta landing between the
  consolidate and the catalog build makes the catalog slightly ahead.
