# Missing matches — audit of 2026-10-25

Reference universe: every completed men's match Statsguru lists for 2001–2026
(classes 1–6, de-duplicated — class 4/5/6 are supersets of 1/2/3).

| | Count |
| --- | --- |
| Statsguru universe, 2001–2026 | 54,390 |
| In the lake (`matches_index.csv`) | 16,456 |
| **Missing** | **38,378 (70.6%)** |

By format of the missing set: First-Class 14,986 · List A 14,661 · T20 7,002 ·
ODI 778 · T20I 727 · Test 224.

By year: 1,120 (2001) … 1,974 (2013) … 839 (2026). Full table in
`missing_by_year.csv`; the id list is `missing_matches.json`.

## Second gap: women's cricket

Cross-checked against Cricsheet (23,132 matches, ids align with ESPN ids):

| | Cricsheet | lake |
| --- | --- | --- |
| Female matches | 4,686 | **7** |
| Male matches | 18,446 | 16,216 |

So "unfiltered, universal" holds for neither axis. The 7 female matches are the
reason 4,677 of the Cricsheet gap is female.

## Why the repo reported 18,428

```
5,483 curated (cricket-extractor) + 12,945 planned delta = 18,428
10,973 delta actually extracted
5,483 + 10,973 = 16,456  <- what shipped
```

18,428 − 16,456 = 1,972 = **2,160 validation failures − 188 October-delta matches
that did land**. The headline counted the plan, not the output; `failed_matches.json`
was shipped inside every batch archive and never read by anything.

## Recovery tiers

| Tier | Meaning | Matches | Recoverable how |
| --- | --- | --- | --- |
| T1 | fetched, then dropped by validation | 2,160 | offline reparse of Layer-1 raw |
| T2 | in Cricsheet, not in lake (parity) | 4,758 | network extraction |
| T3 | rest of the Statsguru universe | 36,291 | network extraction (bulk backlog) |

Queue (T1 first, then T2 by date, then T3): `recovery_queue.json` — 43,209 ids.
`recovery_summary.json` has the counts per tier and gender.

## What "dropped" means (T1)

Every T1 match was fetched, raw payloads were archived, construction succeeded,
and then `validate_match_data()` rejected it. Nine distinct causes:

| Cause | Shape |
| --- | --- |
| Running-score mismatch | off by 4/6 at one ball, usually 19.6 — a boundary or no-ball-plus-four attributed twice by the commentary parser |
| Wicket-count mismatch | computed N vs scorecard N+1 — a dismissal outside the delivery stream (timed out, retired, absent hurt) |
| Source exact-total mismatch | legal-ball or run total differs from the scorecard header |
| Non-monotonic delivery sequence | repeated `oversActual` around a re-bowled ball |
| Unrecognised dismissal kind | `dismissalType` not in `DISMISSAL_KIND_MAP` |

They are *data defects*, not fetch failures: 2,158 of 2,160 reconstruct offline
from the existing raw archives, which is why recovery is cheap.

## Recovery performed

| Step | Result |
| --- | --- |
| Offline reparse of 2,160 T1 ids from the 57 `raw_batch_*.tar.gz` | 2,158 recovered — 38 clean, 2,120 quarantined (`meta.validation_status="failed"`) |
| 240 matches discovered since 2026-10-07 (never scheduled) | 228 extracted — 179 clean, 49 quarantined; 12 fetch failures |
| **Total recovered** | **2,386** → `recovered_matches.tar.gz` (68 MB) — 217 clean, 2,169 quarantined, 0 corrupt |
| Still outstanding | 2 T1 ids (missing raw dir / parse failure) + 12 new ids |

Quarantine keeps the match and publishes the defect instead of dropping it:

```json
"meta": {
  "schema_version": "3.3.0",
  "validation_status": "failed",
  "validation_errors": ["Running score mismatch at Ball 19.6: calculated 160 != Cricinfo 156"]
}
```

Consumers can filter on that key; the alternative — silently dropping 13% of the
corpus — is what happened last time.

## Next steps for T2 / T3

- **T2 (4,758)** — one `Backfill missed matches` dispatch. At 8 runners this is
  roughly 90 minutes.
- **T3 (36,291)** — bulk backlog. At the observed ~55 s/match and 8 runners it is
  ~70 runner-hours; either run it as a multi-day CI sweep with `limit`, or narrow
  the scope (e.g. only competitions that matter) rather than ingesting everything.

Regenerate:

```bash
python scripts/audit_missing.py --known-index matches_index.csv --out-dir missing
python scripts/build_recovery_queue.py --cricsheet-dir /path/to/cricsheet/json
```
