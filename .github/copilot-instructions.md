# Copilot instructions — cricket-lake-unfiltered

The full rules live in [`AGENTS.md`](../AGENTS.md). This file is the short
version.

## Before you touch anything

Run the catalog inspector first. It is read-only and answers in one second
what the lake already knows:

```bash
python scripts/inspect_catalog.py --summary
python scripts/inspect_catalog.py --match-ids <ids>
```

**Never re-scrape or recreate the catalog from scratch to fix a parser bug or
add new fields from existing raw data.** The raw payload of every fetch is
stored in `lake-raw`, and the lake is rebuilt offline from those payloads. Edit
the parser + bump `PARSER_VERSION`, then run `reparse.yml` (updates existing
matches in place in the catalog and auto-publishes; all future `ingest.yml`
matches automatically inherit the change via `build_match()`).

## The four cases

| Case | Symptom | Action |
|---|---|---|
| A | Not in the catalog | `ingest.yml`. No queue file. |
| B | Raw exists, output wrong or needs new fields already in raw | Edit parser (`src/constructor.py` / `src/stage.py` / `src/builder.py`), bump `PARSER_VERSION`, write a test, run `reparse.yml` offline |
| C | `provisional`, recent | Wait for the scheduled refresh, or dispatch `ingest.yml --force_refresh` |
| D | `source_gap` / `settled_incomplete` / quarantined | Leave it. The source contradicts itself. |

## Hard bans (enforced by `tests/test_architecture_guardrails.py`)

- No `queues/`, no `corrections/`, no ad-hoc JSON patch lists in git.
- No new one-off repair/backfill workflows. Only `ingest.yml`,
  `reparse.yml`, `publish.yml`, `ci.yml`.
- No push-triggered data workflows on branches other than `main`.
- Never discard a raw payload. `raw_available` becomes `1` whenever the
  HTTP request succeeded.
- Never mutate a built match JSON downstream. No stage index, no overlay
  ordering.
- Never classify a failure by matching English error text. Use
  `failure_kind`.

## The build path

`src/builder.py:build_match()` is the only way a Match JSON is produced.
Live ingestion and the offline re-parser both call it, so they cannot
disagree. Bump `PARSER_VERSION` there whenever the output changes.

Tests: `.venv/bin/python -m pytest -q`
