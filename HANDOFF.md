# Lake handoff — 9 Nov 2026 (source-gap tier complete; dated calendar live)

This is the handoff for **this repository only** (`cricket-lake-unfiltered`).
The CricStats app/API lives in a different repo (`/Users/hemanth/cstest`) and
has its own handoff; do not apply its runbooks, ports or database rules here.

## Current published state

- Catalog: https://iam-hemanth.github.io/cricket-lake-unfiltered/
- Calendar: https://iam-hemanth.github.io/cricket-lake-unfiltered/calendar.html
- **18,381 matches / 3.0 GB** in the master (run 42658234882) and the catalog
  (run 42665379973). The date range is **2001-12-19 to 2026-11-09**.
- **1,289 warning-tier** and **230 source-gap-tier** matches are included; their
  caveats are in `meta.validation_warnings` (source-gap files also carry
  `meta.source_gap`). **650 quarantined** remain excluded.
- `matches_index.csv` exposes `validation_status`: 16,862 clean / 1,289 warning
  / 230 source-gap.
- The dated calendar lists every published match by year, month and day. Each
  match opens its innings and validation tier and downloads as a single
  `m/<match_id>.json.gz`, decompressed in the browser. The per-match store is
  18,381 files / 275 MB, rebuilt with the catalog.

## What ran (09 Nov)

1. **Re-fetch 42630708983** (14:01–14:59 UTC, 20 x 44): the merge job published
   **552 pass / 328 fail**. The id filter had selected the first 880 *stored*-
   failed ids, so all 552 passes were warn-tier matches already published and
   552 quarantined ids were missed. The pass files added zero new ids.
2. **Source-gap admission 42642702549**: 77 of the 328 admitted (asset
   `matches_batch_sourcegap_20261109T151527Z-42642702549.tar.gz`).
3. **Corrected re-fetch 42649214897** (15:53–16:40 UTC): the fixed filter
   selected the true **880** still-failing ids and all 20 chunks ran in
   parallel. Result: **0 pass / 880 fail** — a fresh parse reproduces every
   failure, so these are source defects, not truncated originals.
4. **Source-gap admission 42657718454**: **230 of the 880** admitted (97 FC,
   59 T20, 31 List A, 25 T20I, 18 other). 77 were already published; **153 are
   new**. Asset `matches_batch_sourcegap_20261109T164604Z-42657718454.tar.gz`
   (sha256 verified, 230 files, all with status + warnings + delta record).
5. **Consolidate 42658234882**: master **18,381**; quarantined **650**;
   warning **1,289**; source-gap **230**; index carries the status column.
6. **Catalog 42665379973**: 18,381 records, 197 slices, plus `calendar.html`,
   `calendar.json` and the 18,381-file `m/` per-match store (275 MB). The
   preflight now shares the master and deltas as one `source-archives`
   artifact; 20 shards pulling the 233 MB master from the release CDN
   concurrently had returned HTTP 500s on random shards.

## Next steps

1. No repair is pending: every quarantined match was re-fetched today and still
   fails. The residual needs new source data or a larger waiver, which is not
   recommended.
2. The 5 delta-window ids from 7–8 Nov (1561358, 1554068, 1555414, 1558436,
   1559692) were never in the recovery batch and remain missing. The four older
   ids 1554097/1554098 (women's) and 1558435/1558599 (running-score only) still
   need source data.
3. The nightly consolidate (02:30 UTC) and catalog (04:30 UTC) fold new deltas
   into the master and refresh the per-match store. A delta landing between the
   two runs makes the catalog one or two matches ahead of the master until the
   next consolidate.

## Rules specific to this repo

- Pushes to `iam-Hemanth/cricket-lake-unfiltered` are expected; **never push
  `/Users/hemanth/cstest`** (local only) and do not touch
  `/Users/hemanth/cricket-stats`.
- The gap batch is named `matches_batch_sourcegap_*` so it unpacks after the
  recovery and refetch batches. The lexicographic order in
  `consolidate_full_lake.yml` is the conflict policy:
  `recovery` < `refetch` < `sourcegap` < `warned`. The newest sourcegap asset
  wins for overlapping ids.
- The 552 refetch pass assets from run 42630708983 duplicate already-published
  matches; do not count them as recoveries.
- The per-match store must stay under 700 MB (`scripts/downloads/calendar.py`
  guard) because GitHub Pages limits a site to 1 GB. The store is rebuilt on
  every catalog run; it is not a release asset.
- No third-party storage, no card, no database writes are involved here.

## Residual to explain, not hide

- **650 quarantined**, all re-fetched today: 475 carry a disallowed error kind,
  156 exceed the small-delta threshold, 19 have over-length overs with no
  adjacent pair.
- Two undated records (1535685, 1551344) make the master `README.txt` print
  "Date Range: 2001-12-19 to Unknown"; the manifest and catalog date ranges are
  correct.
- The catalog is master + deltas by design, so a delta landing between the
  consolidate and the catalog build makes the catalog slightly ahead.
