# Lake handoff

This is the handoff for **this repository only** (`cricket-lake-unfiltered`).
The consumer app and its API live in a different repository; do not apply
their runbooks, ports or database rules here.

Read [`AGENTS.md`](AGENTS.md) first. It holds the operating rules, the triage
decision tree and the hard prohibitions. This file describes the state of
the lake and how to operate it.

## The architecture in one paragraph

Every match the lake holds was fetched once and its **raw payload stored**.
The lake is rebuilt from those stored payloads, never re-fetched to fix a
bug. One function, `src/builder.py:build_match()`, produces every Match JSON,
so the live extractor and the offline re-parser cannot disagree. A single
SQLite catalog, `lake_catalog.sqlite.gz`, names for every match the archive
that holds its raw payload, the archive that holds its built match, and the
digest of each. The catalog is the only authority: nothing decides which
file wins by looking at file names.

```
ESPNcricinfo → raw layer (lake-raw) → build_match() → built layer (lake-built)
                                                           → publish → published lake
                        ↑                                              ↑
                  lake_catalog.sqlite.gz decides everything
```

## The releases

| Tag | Holds | Written by |
|---|---|---|
| `lake-raw` | `raw_shard_NN.tar.gz`, `raw_pack_<stamp>.tar.gz` | `ingest.yml` |
| `lake-built` | `built_shard_NN.tar.gz`, `built_pack_<stamp>.tar.gz` | `ingest.yml`, `reparse.yml` |
| `lake-catalog` | `lake_catalog.sqlite.gz` | `ingest.yml`, `reparse.yml`, `pipeline_compact.py` |
| `data-unfiltered-master` | the published snapshot: master archive, `matches_index.csv`, `lake_manifest.json` | `publish.yml` |
| `lake-schedule` | the series schedule feed | `ingest.yml` (schedule job) |

A match belongs to shard `int(match_id) % 64`. Both layers use the same rule,
so a match always lands in the same place.

## The workflows

There are four, and only four.

| Workflow | Trigger | What it does |
|---|---|---|
| `ingest.yml` | every 6 hours, or manual | Plans from the catalog, fetches in a matrix of 4-match chunks (20 chunks at a time), stores the raw payloads, builds, commits what changed |
| `reparse.yml` | manual | Rebuilds from the stored raw payloads, **with no network access at all** |
| `publish.yml` | nightly, or manual | Copies the built matches into the published snapshot, builds the master archive, compacts the packs |
| `ci.yml` | every pull request and push to `main` | The test suite, including the architectural guardrails |

## The commands

```bash
# What does the lake know? (read-only, one second)
python scripts/inspect_catalog.py --summary
python scripts/inspect_catalog.py --match-ids 1529281,1561358
python scripts/inspect_catalog.py --stale-parser
python scripts/inspect_catalog.py --provisional
python scripts/inspect_catalog.py --quarantined

# The tests
.venv/bin/python -m pytest -q
```

## The pipeline stages

Each stage is a script under `scripts/` and can be run on its own.

```bash
# Plan: decide what to fetch. Nothing is written to the repository.
python scripts/pipeline_ingest.py --plan --workdir ingest_work

# Worker: fetch a chunk, store the raw payloads, build the matches.
python scripts/pipeline_ingest.py --worker --chunk 0 --workdir ingest_work

# Commit: keep what changed, upload it, update the catalog.
python scripts/pipeline_ingest.py --commit --workdir ingest_work --upload

# Rebuild offline, from the raw payloads.
python scripts/pipeline_reparse.py --workdir reparse_work --upload

# Publish the snapshot.
python scripts/pipeline_publish.py --workdir publish_work --out published

# Fold the packs back into the shards.
python scripts/pipeline_compact.py --workdir compact_work --upload
```

## The published snapshot

`publish.yml` builds one snapshot from the catalog: the built matches the
catalog names, the master archive, `matches_index.csv` and
`lake_manifest.json`. The download catalog, the slices and the dated
calendar are all built **from that snapshot alone**.

The previous arrangement extracted the master archive and then overlaid the
delta archives on top of it, in three places with three different orderings.
A match could end up with a different body depending on which archives
happened to be present. The snapshot is already resolved, so there is no
ordering to get wrong.

## The validation tiers

A match's tier is assigned in one pass by `src/validator.py:assess_match()`.

| Tier | Meaning | Published |
|---|---|---|
| `clean` | No issues | Yes |
| `warning` | The per-ball running counter disagrees with the scorecard, but the innings total reconciles | Yes, with `meta.validation_warnings` |
| `source_gap` | The ball-by-ball and the scorecard differ by a small, disclosed amount | Yes, with `meta.validation_warnings` and `meta.source_gap` |
| `scorecard_only` | ESPN published the scorecard but no ball-by-ball commentary for an innings | Yes, with `meta.validation_warnings` |
| `coverage_gap` | The commentary is a truncated prefix of the scorecard | Yes, with `meta.validation_warnings` |
| `failed` | A real contradiction | No |

Two kinds of record made the old arrangement's silence necessary. A quarter
of the raw bundles carry a scorecard but no commentary at all, and a few
hundred more carry commentary that stops before the end of the innings. Both
are admitted with the gap disclosed, because rejecting them would drop
matches ESPN really did publish.

The distinction that matters is between a record that is *short* and one that
*disagrees*. A truncated commentary never has more balls, runs or wickets
than the scorecard describes; it is a prefix of it. A record that exceeds
the scorecard is a contradiction, and it still fails.

The tier is decided **per innings**, because the scorecard and the ball-by-ball
can disagree about different innings in different ways. A running-counter
drift in an innings whose total reconciles is advisory; a disagreement in an
innings whose total does not reconcile is a contradiction.

This matters when you change the validator. The warn tier exists because the
running-total tolerance was relaxed from 2 runs to 50, so a warn-tier match
**passes** the strict validator. A tier that is derived from the validator's
error list alone would therefore be unreproducible. The drift is measured
independently, and the tier is re-derived from that.

**Verified against the published master:** all 2,309 schema 3.3.0 matches
reproduce their published tier exactly (791 clean, 1,288 warning, 230 source
gap). If you change the validator, re-run that check before publishing.

## The maturity states

A completed match can still be missing metadata ESPN publishes later.

| State | Meaning |
|---|---|
| `provisional` | Recent, and something is missing. A refresh is scheduled |
| `settled` | Complete |
| `settled_incomplete` | Past the 14-day window (or out of refresh attempts) and still missing something. The gap is disclosed, never filled in |

The refresh schedule is `+12h`, `+24h`, `+72h`, `+7d`. A refresh that finds
an unchanged `raw_sha256` rebuilds nothing and only moves the attempt
counter.

## Failure kinds

Failures carry an explicit kind. They are never inferred from the wording of
an error message.

`transient_fetch` · `uncompleted_match` · `construction_error` · `validation_failed`

## What was removed, and why

The previous arrangement kept its state in `known_ids.json`,
`lake_state.json`, work lists in the repository, and a sidecar stage index
that was merged into published matches after the fact. Ten workflows
reimplemented the same overlay logic in slightly different orders, and four
of them parsed human-readable validator error strings to patch matches
after extraction.

That produced two classes of silent damage:

- The warn tier could not be re-derived, because the validator's tolerance
  had moved past the threshold that created the tier.
- A source-gap match could be revoked by an unrelated error appearing later.

Both are gone. The tier is one function of one reading of the match, and
`tests/test_architecture_guardrails.py` keeps the rest of the arrangement
from coming back.

## Constraints worth knowing

- **Credentials.** `GITHUB_TOKEN` and `AKAMAI_KEY`. Nothing else. No new
  dependency needs a key.
- **Release limits.** GitHub Releases allow 1,000 assets per release and
  2 GiB per asset. Pages allows a 1 GB site. The per-match store under `m/`
  is capped at 700 MB.
- **Scale.** The lake holds 19,348 matches. Past about 40,000 the
  `m/` store will not fit in the Pages limit, and the calendar will have to
  drop it or move to object storage.
- **Scope.** The lake's data comes from ESPN only. Do not join another
  provider's data into a lake record; if ESPN has no value, leave it null.
  The schedule feed (`lake-schedule`) is strictly separate from match
  ingestion.

## The migration (done 28 November 2026)

The lake was moved behind the catalog on 28 November 2026. What it produced:

| | |
|---|---|
| Raw batch assets read | 83 (59 local, 24 from the external `cricket-extractor` repository) |
| Matches rebuilt | 18,663, from the stored raw payloads, offline |
| Raw bundles packed | 18,657: six scorecards were corrupt (`1417822`, `1496587`, `1496589`, `1525655`, `1530204`, `1552777`), so their built matches are kept as `legacy_unbacked` |
| Shards | 64 `raw_shard_NN` + 64 `built_shard_NN`, 2.2 GB, largest shard 36.6 MB |
| Catalog | 19,348 rows, 3 MB |
| Published matches lost | **0 absent**; `1120137` now fails validation on contradictory source data and is not republished |

At the end of the migration the catalog held 19,348 rows: 18,657 had a raw
payload and 691 were `legacy_unbacked` — 685 published matches that were
never fetched raw, and six rebuilt matches whose only scorecard was corrupt.
All 691 were backfilled on 28 November; the catalog now has a raw payload
for every row (see below).

The rebuild reclassified the legacy schema-3.0.0 records: 15,216 `clean`,
1,288 `warning`, 1,182 `scorecard_only`, 899 `coverage_gap`, 716 `source_gap`
and 47 `failed`. The 46 failed rows that were never published stay excluded.
`1120137` was in the published lake, but its scorecard-only first innings
does not excuse the excess deliveries in its other innings, so it is
disclosed as failed rather than republished.

The 661 matches the migrated lake has and the published lake did not are
matches the raw batches held but that had never been published.

The migration workflow and `scripts/migrate_to_catalog.py` were retired on
28 November 2026 after this run. All future data work goes through
`ingest.yml`, `reparse.yml` and `publish.yml`.

## The migration timestamp correction (done 28 November 2026)

The migration stamped all 18,657 rebuilt matches with its own build instant,
`2026-11-28T07:01:29Z`. That put every match inside the 30-day window, so
`updated_30d_male_all.zip` was 435 MB instead of an incremental delta.

Those 18,657 rows were corrected once in `lake_catalog.sqlite.gz`: `built_at`
was reset to each match's raw fetch time. The backfill rows, built later by
`ingest.yml`, were not touched. The 30-day update selection fell from 19,299
to 730 matches: the 688 rows the raw backfill built, plus 42 rows rebuilt or
fetched inside the window. The migration's 18,657 format-only rebuilds left
the feed. `built_at` keeps its normal meaning from here, so a later
`reparse.yml` rebuild still moves it and a parser fix remains in the update
feed.

The correction is in the catalog; the next `publish.yml` rewrites
`matches_index.csv` and the update slices. A consumer that must reconcile a
re-bundled raw layer (as this migration did) has to seed from the full
archive, because the corrected update feed no longer carries that epoch.

The correction was verified end to end by the 12:30 UTC publish. The catalog
window held 730 publishable rows after the correction and 785 after the
12:00 UTC ingest added 56 matches (624 male, 114 female, 47 unknown). The
male-only `updated_30d_male_all.zip` was rebuilt at 12:30 UTC with 624
matches and 13.5 MB, down from 434.7 MB.

## The legacy raw backfill (done 28 November 2026)

All 691 `legacy_unbacked` matches were fetched once through the standard
`ingest.yml` path with `force_refresh`, in three explicit-id batches: a
50-match canary, then 323 and 320. Every row now has a raw payload and
`legacy_unbacked` is zero.

- **No fetch or build failures.** The six corrupt-scorecard matches
  (`1417822`, `1496587`, `1496589`, `1525655`, `1530204`, `1552777`) were
  re-fetched successfully and are ordinary raw-backed rows.
- **Fourteen tier changes, none to `failed`:** `clean` → `coverage_gap` (11),
  `clean` → `scorecard_only` (1), `clean` → `source_gap` (1), and
  `source_gap` → `coverage_gap` (1). All remain publishable.
- **Final tiers (19,348 rows):** 15,203 `clean` · 1,288 `warning` ·
  1,183 `scorecard_only` · 911 `coverage_gap` · 716 `source_gap` ·
  47 `failed`. The 47 failed rows stay excluded, so the master has 19,301.
- **Maturity:** 19,334 `settled`, 10 `settled_incomplete`, 4 `provisional`.
  The unfinished rows are matches ESPN has not yet given a player of the
  match; they refresh on the normal schedule.

Three fixes landed while the backfill ran:

1. `build_plan()` chunking used floor division, so a 50-id plan produced
   three chunks of 16 and silently dropped the last two. Every selected id
   now reaches exactly one chunk.
2. `ingest.yml` and `reparse.yml` declared `contents: write` only; the
   `gh workflow run publish.yml` dispatch needs `actions: write`, and without
   it the commit step failed with HTTP 403 after its assets were uploaded.
3. Chunks are now four matches, not twenty, so one slow match cannot hold a
   worker for forty minutes. The 320-match batch fetched in about nine
   minutes of worker time (80 chunks over four 20-job waves); the run
   totalled 16 minutes, the rest being runner contention with a concurrent
   publish.

## Where things are

| Path | Holds |
|---|---|
| `src/builder.py` | `build_match()` — the single build path, and `PARSER_VERSION` |
| `src/validator.py` | the strict checks, and `assess_match()` — the tier |
| `src/completeness.py` | the completeness evaluator and the maturity state machine |
| `src/catalog_db.py` | the catalog: schema, queries, the idempotency rule |
| `src/shard_store.py` | packing, materializing and compacting the two layers |
| `src/commentary.py` | the only place that talks to ESPN; stores every raw payload |
| `src/release_store.py` | the only place that talks to GitHub releases |
| `scripts/inspect_catalog.py` | the read-only diagnostic |
| `scripts/pipeline_*.py` | the four pipeline stages |
| `tests/test_architecture_guardrails.py` | the rules, enforced |
