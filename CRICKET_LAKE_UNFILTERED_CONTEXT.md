# Cricket Lake Unfiltered — Project Context for Copilot & AI Agents

> Feed this file at the start of any session working on `cricket-lake-unfiltered`:
> In Chat type: `#file:CRICKET_LAKE_UNFILTERED_CONTEXT.md` then your prompt.

---

## 1. What This Repository Is

An independent, universal cricket data lake covering **all completed cricket matches** without filters. Built directly from ESPNcricinfo's stats engine and ball-by-ball APIs, outputting normalized **Match Schema v3.0 JSON**.

- **Scope**: Universal coverage across international, domestic leagues, First-Class, List A, and associate cricket (~18,400+ fixtures).
- **Tournaments Covered**:
  - All Tests (including pre-2011), ODIs (including pre-2007), T20Is.
  - Major Franchise Leagues: IPL, BBL, PSL, CPL, SA20, The Hundred, ILT20, MLC, BPL, Super Smash.
  - Domestic Competitions: Sheffield Shield, County Championship, Ranji Trophy, Duleep Trophy, Irani Cup, Syed Mushtaq Ali Trophy, Marsh One-Day Cup, CSA Pro20 Cup.
  - Multi-Sport Games & Regional Events: Asian Games, Africa Continental Cup, ICC World Cup Qualifiers, Canada Super60.
- **Scale**: **16,456 valid matches**, **7,711,074 deliveries**, **4,690,422 wagon wheel vectors (60.83%)**, **2,495,144 pitch coordinates (32.36%)**, **5,476,021 shot types (71.02%)**.
- **Player Cross-Mapping**: 10,084 mapped directly to canonical Cricsheet IDs (`people.csv`) with an **89.07%** mapping rate.
- **Date Range**: December 19, 2001 to October 06, 2026.

---

## 2. Architecture & Delta Strategy

### A. Delta Extraction Strategy
Rather than re-scraping 18,400+ matches from scratch (which would take ~10+ hours and risk Akamai rate-limits), this repository uses a smart **delta extraction approach**:
1. Merges the **5,503 curated matches** already extracted and validated in `iam-Hemanth/cricket-extractor`.
2. Extracts only the newly uncovered fixtures in modular batches.
3. Historical Delta: **12,945 matches** across 52 batches (Batches 0 to 51).
4. October Freshness Delta: **188 matches** across 5 batches (Batches 52 to 56).
5. Master Consolidation: Merges all 57 batch archives with `cricket_lake_v3.tar.gz` to produce the monolithic universal master lake.

### B. Two-Layer Architecture
- **Layer 1 (Immutable Raw Payloads)**: Gzipped raw HTTP responses stored byte-for-byte (`scorecard.json.gz`, `commentary_innN_chunkM.json.gz`, `meta.json`). 57 batch archives uploaded to GitHub Releases totaling **~1.85 GB**.
- **Layer 2 (Normalized Match Schema v3.0)**: Strict JSON contract (`schema/schema.json`) with unified field names, integer over/ball numbers, coordinates, and canonical player mappings.

### C. Validation & Resilience Rules (`src/validator.py`, `src/constructor.py`)
- **Over Length Realities**: Completed overs tolerate 5 to 8 balls to accommodate umpire miscounts (e.g. Ben Laughlin 7-ball over in IPL 2018 match `1136564`) and mid-over declarations.
- **The Hundred 5-Ball Overs**: Dynamic over length support (`balls_per_over = 5`).
- **MCC Law 28.3 Helmet Penalties**: Detects 5-run jumps in running scores when ball strikes fielder helmet behind keeper and attributes `penalties = 5`.
- **Duplicate Dismissals**: Deduplicates duplicate commentary dismissal events by `player_out_id` per innings.
- **Non-Delivery Dismissals**: Subtracts `timed_out` dismissals from expected delivery wickets.
- **Defensive Data Handling**: Handles `None` values safely in `matchPlayers`, `teamPlayers`, and match officials (`_umpires`, `_referee`) without raising exceptions.
- **Exception Boundaries**: `extract.py` wraps match construction in a try/except block, logging problematic fixtures to `failed_matches.json` while allowing batch workflows to complete 100% of remaining matches.

---

## 3. Releases & Distribution Packages

### A. Master Distribution: Release [`data-unfiltered-master`](https://github.com/iam-Hemanth/cricket-lake-unfiltered/releases/tag/data-unfiltered-master)

| Asset | Size | Purpose |
| :--- | :--- | :--- |
| **`cricket_lake_unfiltered_master.zip`** | **220.12 MB** | Monolithic standalone zip with all 16,456 match JSON files + `README.txt` + `matches_index.csv` + `lake_manifest.json`. |
| **`cricket_lake_unfiltered_master.tar.gz`** | **180.04 MB** | Compressed Linux/macOS release tarball. |
| **`matches_index.csv`** | **3.21 MB** | Tabular spreadsheet catalog indexing every match file for Pandas (`pd.read_csv`). |
| **`README.txt`** | **2.04 MB** | Human-readable ASCII table catalog mapping every `<match_id>.json` file. |
| **`lake_manifest.json`** | **0.01 MB** | Full global telemetry metrics and manifest breakdown. |

### B. Modular Batch Archives: Release [`data-unfiltered-batches`](https://github.com/iam-Hemanth/cricket-lake-unfiltered/releases/tag/data-unfiltered-batches)
- **114 Total Assets Live**:
  - 57 Match JSON archives (`matches_batch_0..56.tar.gz`, ~130 MB).
  - 57 Raw HTTP Layer 1 archives (`raw_batch_0..56.tar.gz`, ~1.85 GB).

---

## 4. Key CLI Commands & Workflows

```bash
# 1. Run local extraction on specific matches
python extract.py --match-ids 1529230,1551854

# 2. Extract an entire manifest batch
python extract.py --manifest data/manifest.json --batch-index 0 --batch-size 250 --delay 0.2

# 3. 100% Offline re-parse from raw archives
python reparse.py --raw-dir data/raw --output-dir data/matches --strict

# 4. Consolidate full lake and generate manifest
python consolidate.py --input-dirs data/master_all_matches --output-dir data/master_lake/matches --manifest-path data/master_lake/lake_manifest.json

# 5. Build distribution catalog (ZIP, CSV, README.txt)
python scripts/build_lake_catalog.py --matches-dir data/master_lake/matches --output-dir data/master_lake/dist --archive-prefix cricket_lake_unfiltered_master

# 6. Discover recent completed matches directly from Cricinfo
python scripts/discover_recent_matches.py --since 2026-09-18
```

---

## 5. Chronological Development Log

### Tue Oct 06 18:55:00 IST 2026
- **Architecture & Setup of Independent Unfiltered Data Lake**:
  - Created remote GitHub repository `iam-Hemanth/cricket-lake-unfiltered`.
  - Configured repository secret `AKAMAI_KEY` via encrypted public-key libsodium payload.
  - Cloned local repository to `/Users/hemanth/cricket-lake-unfiltered` and ported normalized engine (`src/`, `extract.py`, `reparse.py`, `consolidate.py`).
  - Extracted chronological delta manifest of 12,945 matches into `data/manifest.json`.
  - Configured `.github/workflows/parallel_extract_unfiltered.yml` (52 batches, max-parallel: 20).
  - Configured `.github/workflows/consolidate_full_lake.yml` to merge the 52 delta batches with the 5,483 curated matches.

### Tue Oct 06 18:57:00 IST 2026
- **Dispatched 52-Batch Overnight Extraction**:
  - Launched `parallel_extract_unfiltered.yml` (Run ID: `37470848378`) with 20 concurrent Azure VM workers.

### Wed Oct 07 14:03:00 IST 2026
- **Overnight Extraction Audit — 100% SUCCESS**:
  - All 52 batches completed cleanly in GitHub Actions.
  - Uploaded 104 assets to release `data-unfiltered-batches` (52 Match archives [125.84 MB] + 52 Raw Layer 1 archives [1.84 GB]).

### Wed Oct 07 14:19:00 IST 2026
- **Master Consolidation & Release**:
  - Workflow `consolidate_full_lake.yml` (Run ID: `37594544200`) completed 100% SUCCESS.
  - Published initial master release [`data-unfiltered-master`](https://github.com/iam-Hemanth/cricket-lake-unfiltered/releases/tag/data-unfiltered-master):
    - `cricket_lake_unfiltered_master.zip` (218.73 MB, 16,281 valid matches).
    - `cricket_lake_unfiltered_master.tar.gz` (178.91 MB).
    - `matches_index.csv` (3.18 MB) and `README.txt` (2.02 MB).

### Wed Oct 07 14:26:00 IST 2026
- **Discovered Cricsheet 20-Day Dormancy**:
  - Verified upstream Cricsheet has been frozen since September 17, 2026.
  - Discovered 188 missing fixtures from Cricinfo (CSA Pro20 Cup, CPL, Asian Games, Canada Super60, T20 WC Qualifiers).

### Wed Oct 07 16:42:00 IST 2026
- **Launched October Delta Extraction (Batches 52 to 56)**:
  - Built `scripts/discover_recent_matches.py` and output `data/manifest_unfiltered_delta.json` (188 matches).
  - Created `.github/workflows/extract_october_delta.yml` (Batches 52 to 56, 40 matches each).
  - Dispatched workflow in GitHub Actions (Run ID: `37612455219`).

### Wed Oct 07 17:03:00 IST 2026
- **Defensive Hardening in Constructor & Extractor**:
  - Resolved `AttributeError: 'NoneType' object has no attribute 'get'` when `matchPlayers` was null on rainouts.
  - Updated `src/constructor.py` to use `(content.get("matchPlayers") or {}).get("teamPlayers") or []`.
  - Defensively handled unassigned officials (`umpires`, `tv_umpire`, `match_referee`).
  - Added try/except error boundary in `extract.py` around `construct_match_v3`.

### Wed Oct 07 17:21:00 IST 2026
- **October Delta Extraction Completed (100% SUCCESS)**:
  - Workflow `extract_october_delta.yml` Run #2 (`37614912564`) completed with success across all 5 matrix runners.
  - Uploaded 10 assets (`matches_batch_52..56.tar.gz` and `raw_batch_52..56.tar.gz`) to release `data-unfiltered-batches` (114 assets total across 57 batches).

### Wed Oct 07 17:23:00 IST 2026
- **Master Lake Consolidation Completed (100% SUCCESS)**:
  - Workflow `consolidate_full_lake.yml` (Run ID: [`37617101314`](https://github.com/iam-Hemanth/cricket-lake-unfiltered/actions/runs/37617101314)) completed with 100% success.
  - Aggregated all 57 delta batches with the refreshed 5,503-match curated lake.
  - Published updated master release [`data-unfiltered-master`](https://github.com/iam-Hemanth/cricket-lake-unfiltered/releases/tag/data-unfiltered-master):
    - `cricket_lake_unfiltered_master.zip` (**220.12 MB**, 16,456 valid matches).
    - `cricket_lake_unfiltered_master.tar.gz` (**180.04 MB**).
    - `matches_index.csv` (**3.21 MB**).
    - `README.txt` (**2.04 MB**).
    - `lake_manifest.json` (**0.01 MB**): 16,456 matches, 7.711M deliveries, 60.83% wagon wheel coverage, 71.02% shot types, only 53 unreconciled edge cases (99.68% factual perfection across cricket history).
