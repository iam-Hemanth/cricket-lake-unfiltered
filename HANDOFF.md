# Lake handoff — 9 Nov 2026 (re-fetch still in flight)

This is the handoff for **this repository only** (`cricket-lake-unfiltered`).
The CricStats app/API lives in a different repo (`/Users/hemanth/cstest`) and
has its own handoff; do not apply its runbooks, ports or database rules here.

## Current published state

- Catalog: https://iam-hemanth.github.io/cricket-lake-unfiltered/
- **18,145 matches / 197 slices / 3.0 GB** (master rebuilt 11:45 UTC).
- **1,289 warning-tier** matches are included; their caveats are in
  `meta.validation_warnings`. **880 quarantined** matches remain excluded.
- A 6-hourly delta at **12:58 UTC** added **5 matches** (two Tests through
  9 Nov and three 9 Nov matches). The master and catalog do **not** include
  those 5 yet; the next consolidate will.
- The source-gap tier is implemented and documented in
  `missing/QUARANTINE.md`: structurally complete matches with at most 2 legal
  balls, 2 runs or 1 wicket of source disagreement are admitted as
  `validation_status = "source_gap"` with the exact warnings and a
  `meta.source_gap` delta record.

## In flight — do not cancel

- Workflow: **Re-fetch quarantined matches**, run **42630708983**, started
  14:01 UTC. It is a 20-slot matrix, 44 matches per slot, delay 0.15 s.
- Purpose: re-download the 880 still-quarantined ids and distinguish a
  truncated original parse (now passes) from a genuine source gap (still
  fails). The merge job produces the **`refetch-results`** artifact, which is
  the input for the source-gap admission.
- Two earlier (cancelled) chunks completed and uploaded clean assets, kept:
  - `matches_batch_refetch_20261109T133829Z-42620150637_c3.tar.gz` — 104 pass
  - `matches_batch_refetch_20261109T140012Z-42620150637_c1.tar.gz` — 106 pass
  That is **210 clean matches already recovered**, independent of the new run.

## Exact next steps

1. **Wait for run 42630708983**:
   `gh run view 42630708983 --repo iam-Hemanth/cricket-lake-unfiltered`
   The `merge` job must finish; its `refetch-results` artifact contains `fail/`
   (still-failing fresh files), `refetch_summary.json`, `quarantine_ids.txt`.
2. **Admit the source-gap tier** (must run on the refetch `fail/` files, never
   on the original recovery batch — that could overwrite a clean recovery):
   ```
   gh workflow run apply_source_gap.yml \
     --repo iam-Hemanth/cricket-lake-unfiltered \
     -f refetch_run_id=42630708983 -f publish=true
   ```
   Verify the run's admitted count and that every file has
   `validation_status=source_gap`, warnings, and `meta.source_gap`.
3. **Rebuild the master**:
   `gh workflow run consolidate_full_lake.yml --repo iam-Hemanth/cricket-lake-unfiltered`
   Verify `lake_manifest.json` (`data-unfiltered-master` release):
   - `total_matches` = 18,145 + 5 (new delta) + clean refetch additions + gap admits
   - `coverage.quarantined_matches` = 880 − clean refetch additions − gap admits
   - `coverage.warning_matches` = 1,289
   - `coverage.source_gap_matches` = gap admits
4. **Rebuild the catalog**:
   `gh workflow run build_downloads.yml --repo iam-Hemanth/cricket-lake-unfiltered`
   Verify the live `downloads.json` has the new totals and that
   `warning_matches` / `source_gap_matches` appear in the page notes; check one
   recovered id in a slice README and `matches_index.csv`.
5. **Record receipts** in `missing/QUARANTINE.md` (counts, run ids, asset
   names) and commit/push this repo.

## Rules specific to this repo

- Pushes to `iam-Hemanth/cricket-lake-unfiltered` are expected; **never push
  `/Users/hemanth/cstest`** (local only) and do not touch
  `/Users/hemanth/cricket-stats`.
- The gap batch is named `matches_batch_sourcegap_*` so it unpacks after the
  recovery and refetch batches. The lexicographic order in
  `consolidate_full_lake.yml` is the conflict policy:
  `recovery` < `refetch` < `sourcegap` < `warned`.
- No third-party storage, no card, no database writes are involved here.

## Residual to explain, not hide

- After the gap admission, the remaining quarantined set is mostly the
  **large source gaps** (missing overs/innings), the 28 incomplete innings and
  the penalty/running-total cases that do not reconcile.
- The latest window still misses some 7–8 Nov matches: the update retry queue
  holds 1561358, 1554068, 1555414, 1558436, 1559692; four were abandoned
  before the validator fix (1554097/1554098 women's; 1558435/1558599 men's
  running-score only). Re-queueing the two men's ids is the cheapest next
  follow-up; the others need source data.
- The live catalog's date range is **2001-12-19 to 2026-11-08** until the next
  master/catalog rebuild includes the 12:58 delta and the recovered matches.
