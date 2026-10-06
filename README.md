# 🏏 Cricket Lake Unfiltered (18,400+ Matches)

An independent, hardened cricket data lake repository dedicated to **unfiltered, universal cricket match telemetry** directly extracted from ESPNcricinfo.

This dataset expands beyond core international and franchise competitions to encompass **100% of historical domestic first-class, List A, franchise T20 leagues, and associate tournaments** (1877–present day).

---

## 📊 Scope & Universe

- **Total Matches**: **18,428 matches** (5,483 curated international/IPL matches + 12,945 domestic & associate matches)
- **Competitions Covered**:
  - **Domestic T20 Leagues**: Big Bash League (BBL), Pakistan Super League (PSL), Caribbean Premier League (CPL), Bangladesh Premier League (BPL), Vitality Blast / T20 Blast, Syed Mushtaq Ali Trophy, Super Smash, CSA T20.
  - **First-Class & Multi-Day**: County Championship (Div 1 & 2), Sheffield Shield, Ranji Trophy, Duleep Trophy.
  - **List A One-Day**: Royal London One-Day Cup, Marsh Cup, Vijay Hazare Trophy, Ford Trophy.
  - **Historical Cricket**: Pre-2011 Test matches, Pre-2007 ODIs, Associate ICC tournaments & World Cup Qualifiers.
- **Delivery-Level Telemetry**:
  - 2D Wagon Wheel polar coordinates (`wagon_coords: [x, y]`, `wagon_zone`)
  - Pitch map pitch line & length (`pitch_line`, `pitch_length`)
  - Shot classification (`shot_type`, `shot_control`)
  - Canonical cross-source player mapping (`cricsheet_id` + `espn_id`)

---

## ⚡ Architecture

1. **Layer 1 (Immutable Raw Payloads)**: Byte-for-byte gzipped HTTP response bodies (`raw_batch_*.tar.gz`) stored permanently on release assets.
2. **Layer 2 (Normalized Match JSON v3.0)**: Fully validated ball-by-ball JSON with pitch reality tolerance bounds, MCC Law 28.3 penalty rules, and DRS tracking (`matches_batch_*.tar.gz`).
3. **Consolidated Master Lake**: Unified release archive `cricket_lake_unfiltered_master.tar.gz` and complete manifest `lake_manifest.json`.

---

## 🚀 Workflows

- **`parallel_extract_unfiltered.yml`**: Parallel matrix extractor running 52 batches (250 matches each) with 20 concurrent runners on GitHub Actions.
- **`consolidate_full_lake.yml`**: Master aggregator merging all 52 delta batches with the 5,483 curated matches from `cricket-extractor`.
