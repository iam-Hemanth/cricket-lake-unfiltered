# Agent Operating Rules — cricket-lake-unfiltered

This repository holds a data lake of ESPNcricinfo match records. It has one
published output and one rule that matters more than any other: **the raw
payload of every fetch is stored, and the lake is rebuilt from the raw
payloads, never re-fetched to fix a bug.**

If you are an agent or a person working on this repository, read this file
before you run anything.

## The shape of the lake

```
ESPNcricinfo
     │
     ▼  src/commentary.py  fetch_match_bundle()   (network; always stores raw)
┌──────────────────────────────────────────────────────────┐
│ Layer 1: raw store  (release: lake-raw)                 │
│   raw_shard_NN.tar.gz  ·  raw_pack_<stamp>.tar.gz        │
│   <match_id>/scorecard.json.gz, commentary chunks,       │
│   fetch_meta.json (fetched_at, bundle_sha256)            │
└──────────────────────────┬───────────────────────────────┘
                           │
                           ▼  src/builder.py  build_match()  (pure, offline)
┌──────────────────────────────────────────────────────────┐
│ Layer 2: built store  (release: lake-built)              │
│   built_shard_NN.tar.gz  ·  built_pack_<stamp>.tar.gz    │
│   <match_id>.json  (schema 3.3.0)                        │
└──────────────────────────┬───────────────────────────────┘
                           │
                           ▼  scripts/pipeline_publish.py
                  published snapshot  (release: data-unfiltered-master)
```

`lake_catalog.sqlite.gz` (release: `lake-catalog`) names, for every match,
the one archive that holds its raw payload, the one archive that holds its
built match, and the digest of each. The catalog is the only authority for
which file wins. Nothing resolves precedence by file name.

## Mandatory Triage Decision Tree

**Whenever data is missing, incomplete, outdated or buggy, work through this
tree in order. Do not skip step 1.**

### 1. Inspect the catalog first — never blindly re-scrape or rebuild

```bash
python scripts/inspect_catalog.py --summary
python scripts/inspect_catalog.py --match-ids 1529281,1561358
python scripts/inspect_catalog.py --stale-parser
python scripts/inspect_catalog.py --provisional
python scripts/inspect_catalog.py --quarantined
```

This is read-only and takes one second. It tells you what the lake knows
about a match, which archive holds it, whether its raw payload exists, and
whether it is waiting for a refresh.

### 2. Diagnose which of the four cases applies

**Case A — the match is not in the catalog.**
It was never fetched. Trigger `ingest.yml` (with `match_ids` if you know
them, or wait for the 6-hourly cron). **Do not create a queue file in git.**

**Case B — the raw payload exists (`raw_available = 1`) and either the
output JSON is wrong, or you want to extract new/extra fields already
present in the stored ESPN payload.**
This is a **parser change, not a fetch problem.**

1. Inspect a stored raw bundle from `lake-raw` (`scorecard.json.gz`,
   commentary chunks) to confirm ESPN already sent the data, and save a
   minimal raw fixture under `tests/fixtures/` with a failing test.
2. Edit the single shared parser (`src/constructor.py`, `src/stage.py`,
   `src/validator.py`, `src/builder.py`).
3. Bump `PARSER_VERSION` in `src/builder.py` (so stale built rows are
   detected automatically in the existing catalog).
4. Run `pytest` locally, push/merge to `main`, then run **`reparse.yml`**,
   which is 100% offline.

**Never re-fetch from ESPN and never recreate `lake_catalog.sqlite.gz` from
scratch to fix a bug or add new fields.** Both **existing** matches (updated
in place in the existing catalog by `reparse.yml` from `lake-raw`, then
auto-published via `publish.yml`) and **future** matches (built by the exact
same `src/builder.py:build_match()` inside `ingest.yml`) get the change from
this single edit. Only touch `src/commentary.py` + `ingest.yml` if a brand-new
unfetched ESPN API endpoint must be captured going forward.

**Case C — the raw payload exists, the match finished recently (under 14
days), and ESPN had not published the telemetry or awards yet
(`maturity_state = 'provisional'`).**
This is normal, not a defect. Wait for the scheduled refresh inside
`ingest.yml`, or dispatch `ingest.yml` with `match_ids` and
`force_refresh = true`. Only a changed `raw_sha256` triggers a rebuild; an
unchanged fetch costs nothing but an attempt counter.

**Case D — the match is `settled_incomplete`, `source_gap` or quarantined
after a refresh.**
The source genuinely omits or contradicts the data. Leave the catalog status
as it is. **Never fabricate deliveries, never weaken a validation rule
silently, and never add a manual override file.** A disclosed gap is a
correct outcome.

### 3. Only then act

| Situation | Action |
|---|---|
| New matches to fetch | `ingest.yml` (`workflow_dispatch`, or the cron) |
| Parser changed or new fields added from stored raw payloads | Edit parser + bump `PARSER_VERSION` → `reparse.yml` (offline, updates existing catalog in place; future `ingest.yml` runs get it via `build_match()`) |
| New built matches to publish | `publish.yml` |
| Too many packs in a release | `publish.yml` (it runs `compact`) |

## Hard prohibitions

These are enforced by `tests/test_architecture_guardrails.py`, which runs on
every pull request. A change that breaks one of them will fail CI.

- **No `queues/`, `corrections/` or ad-hoc JSON patch lists committed to
  git.** A match that needs another attempt is recorded in the catalog as
  provisional. The next run picks it up.
- **No new one-off `.github/workflows/*.yml` repair, backfill or patch
  workflows.** All data runs go through `ingest.yml`, `reparse.yml` and
  `publish.yml` on `main`.
- **No push-triggered data workflows on side branches** (for example
  `repair/*`). `main` is the single operational branch.
- **No discarding of raw payloads on any fetch path.** `raw_available` must
  become `1` whenever an HTTP request succeeds, even if the build then
  fails.
- **No downstream mutation of built match JSONs** in `consolidate.py` or
  `scripts/downloads/*`. No sidecar stage index, no tarball-order overlay
  logic. The published snapshot is a copy.
- **No error classification by reading English error text.** Use the
  explicit `failure_kind` enum: `transient_fetch`, `uncompleted_match`,
  `construction_error`, `validation_failed`.

## Running the tests

```bash
.venv/bin/python -m pytest -q
```

The suite includes the architectural guardrails, so a change that breaks one
of the rules above fails here before it fails in production.

## Credentials

The pipeline needs `GITHUB_TOKEN` (to read and write the releases) and
`AKAMAI_KEY` (to reach ESPN). Nothing else. Do not add a dependency that
needs a key.

## Scope limits

- `scripts/scrape_schedule.py` (release tag `lake-schedule`) is the series
  schedule feed. It is **strictly isolated** from match ingestion. Do not
  couple the two.
- The lake's data comes from ESPN only. Do not join another provider's data
  into a lake record. If ESPN does not have a value, leave it null.
- This repository is the lake. Consumer applications live elsewhere; report
  consumer-side work rather than making it here.
