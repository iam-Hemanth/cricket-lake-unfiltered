"""
src/catalog_db.py
The single catalog of the lake.

Every match in the lake has exactly one row here. The row names the raw
archive that holds its source payload and the built archive that holds its
Match JSON, together with the digest of each. Nothing else in the lake
decides which file wins: the catalog does, by lookup.

That replaces the previous arrangement, in which ``known_ids.json``,
``lake_state.json``, the ad-hoc work lists and the order of the tarballs in a
release decided the content of the lake. Those are all derivable from this
one table, and so is the answer to "why does this match look like this?".
"""
from __future__ import annotations

import gzip
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

#: Bumped when the table layout changes in a way that needs a migration.
SCHEMA_VERSION = 1

#: The number of shards the lake is split into. A match belongs to shard
#: ``int(match_id) % NUM_SHARDS``, so every shard stays roughly the same size
#: and a match always lands in the same place.
NUM_SHARDS = 64

#: The tiers a match can hold.
TIERS = ("clean", "warning", "source_gap", "failed", "fetch_failed")

#: The maturity states a match can hold.
MATURITY_STATES = ("provisional", "settled", "settled_incomplete", "legacy_unbacked")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
  match_id TEXT PRIMARY KEY,
  match_date TEXT,
  format TEXT,
  format_bucket TEXT,
  competition TEXT,
  season TEXT,
  team1 TEXT,
  team2 TEXT,
  winner TEXT,
  result TEXT,
  gender TEXT,
  raw_available INTEGER NOT NULL DEFAULT 0,
  raw_archive TEXT,
  raw_sha256 TEXT,
  raw_fetched_at TEXT,
  parser_version TEXT,
  schema_version TEXT,
  built_archive TEXT,
  built_sha256 TEXT,
  built_at TEXT,
  validation_tier TEXT NOT NULL DEFAULT 'failed',
  completeness_gaps_json TEXT NOT NULL DEFAULT '[]',
  maturity_state TEXT NOT NULL DEFAULT 'provisional',
  fetch_attempts INTEGER NOT NULL DEFAULT 0,
  last_attempt_at TEXT,
  next_refresh_at TEXT,
  last_failure_kind TEXT,
  last_error TEXT,
  index_meta_json TEXT,
  first_published_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_matches_maturity ON matches(maturity_state, next_refresh_at);
CREATE INDEX IF NOT EXISTS idx_matches_parser ON matches(raw_available, parser_version);
CREATE INDEX IF NOT EXISTS idx_matches_tier ON matches(validation_tier);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  workflow TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  summary_json TEXT
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


def utc_now_iso() -> str:
    """The current time, as an ISO-8601 string in UTC."""
    return datetime.now(timezone.utc).isoformat()


def open_catalog(path: str | Path) -> sqlite3.Connection:
    """Open (and if necessary create) the catalog.

    The connection is configured for a single writer with concurrent readers:
    the write-ahead log lets a reader work while a writer is active, and the
    busy timeout keeps a concurrent writer waiting rather than failing.
    """
    target = Path(path)
    if target.parent and str(target.parent):
        target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(target), timeout=60.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=60000")
    connection.executescript(_SCHEMA)
    connection.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    connection.commit()
    return connection


@contextmanager
def catalog_transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block as one transaction."""
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def save_catalog_gz(connection: sqlite3.Connection, path: str | Path) -> Path:
    """Write the catalog to a gzipped file atomically.

    The catalog is uploaded as one release asset, so a partial write would
    corrupt it for every reader. The file is written beside its destination
    and renamed into place.

    The copy uses SQLite's own backup interface. A connection in write-ahead
    log mode cannot be serialized directly, because the serialized image would
    leave out the pages that still live in the log.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection.commit()

    handle, temp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    os.close(handle)
    temp_path = Path(temp_name)
    try:
        snapshot_path = temp_path.with_suffix(".snapshot")
        snapshot = sqlite3.connect(str(snapshot_path))
        try:
            connection.backup(snapshot)
            snapshot.commit()
            snapshot.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            snapshot.close()
        with open(snapshot_path, "rb") as source:
            with gzip.open(temp_path, "wb") as out:
                out.write(source.read())
        snapshot_path.unlink(missing_ok=True)
        temp_path.replace(target)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return target


def load_catalog_gz(path: str | Path) -> sqlite3.Connection:
    """Read a gzipped catalog back into an in-memory connection."""
    handle, temp_name = tempfile.mkstemp(suffix=".sqlite")
    os.close(handle)
    temp_path = Path(temp_name)
    try:
        with gzip.open(path, "rb") as source:
            temp_path.write_bytes(source.read())
        disk = sqlite3.connect(str(temp_path))
        disk.row_factory = sqlite3.Row
        memory = sqlite3.connect(":memory:")
        memory.row_factory = sqlite3.Row
        disk.backup(memory)
        memory.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        memory.commit()
        disk.close()
        return memory
    finally:
        temp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------

def upsert_match(connection: sqlite3.Connection, match_id: str, **fields: Any) -> None:
    """Insert or update one match row.

    Only the supplied columns are written; the rest keep their current value.
    """
    if not fields:
        return
    fields = dict(fields)
    fields["updated_at"] = fields.get("updated_at") or utc_now_iso()
    columns = ["match_id"] + sorted(fields)
    placeholders = ", ".join("?" for _ in columns)
    assignments = ", ".join(f"{name} = excluded.{name}" for name in sorted(fields))
    sql = (
        f"INSERT INTO matches ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(match_id) DO UPDATE SET {assignments}"
    )
    connection.execute(sql, [str(match_id)] + [fields[name] for name in sorted(fields)])


def get_match(connection: sqlite3.Connection, match_id: str) -> Optional[sqlite3.Row]:
    """One match row, or None."""
    cursor = connection.execute("SELECT * FROM matches WHERE match_id = ?", (str(match_id),))
    return cursor.fetchone()


def all_match_ids(connection: sqlite3.Connection) -> List[str]:
    """Every match id in the catalog, in order."""
    cursor = connection.execute("SELECT match_id FROM matches ORDER BY match_id")
    return [str(row["match_id"]) for row in cursor]


def count_matches(connection: sqlite3.Connection) -> int:
    """How many matches the catalog holds."""
    return int(connection.execute("SELECT COUNT(*) AS n FROM matches").fetchone()["n"])


def select_due_refreshes(
    connection: sqlite3.Connection,
    limit: int = 200,
    now_utc: Optional[datetime] = None,
) -> List[str]:
    """Match ids whose scheduled refresh is due.

    A match is due when it is still provisional and its next refresh time has
    passed. The order puts the matches most likely to be fixed first: those
    that failed to fetch, then those that failed validation, then those that
    are merely missing metadata.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    now_text = now_utc.astimezone(timezone.utc).isoformat()
    sql = """
        SELECT match_id FROM matches
         WHERE maturity_state = 'provisional'
           AND next_refresh_at IS NOT NULL
           AND next_refresh_at <= ?
         ORDER BY CASE
                    WHEN last_failure_kind = 'transient_fetch' THEN 0
                    WHEN validation_tier = 'failed' THEN 1
                    WHEN last_failure_kind = 'validation_failed' THEN 2
                    ELSE 3
                  END,
                  next_refresh_at ASC
         LIMIT ?
    """
    cursor = connection.execute(sql, (now_text, int(limit)))
    return [str(row["match_id"]) for row in cursor]


def select_stale_parser(
    connection: sqlite3.Connection,
    target_parser_version: str,
    limit: Optional[int] = None,
) -> List[str]:
    """Match ids whose stored build used a different parser version.

    Only matches with a stored raw payload qualify: without the raw payload
    there is nothing to rebuild from.
    """
    sql = """
        SELECT match_id FROM matches
         WHERE raw_available = 1
           AND (parser_version IS NULL OR parser_version != ?)
         ORDER BY match_id
    """
    params: List[Any] = [str(target_parser_version)]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    cursor = connection.execute(sql, params)
    return [str(row["match_id"]) for row in cursor]


def select_legacy_unbacked(connection: sqlite3.Connection) -> List[str]:
    """Match ids that are published but have no raw payload behind them."""
    cursor = connection.execute(
        "SELECT match_id FROM matches WHERE raw_available = 0 AND maturity_state = 'legacy_unbacked'"
        " ORDER BY match_id"
    )
    return [str(row["match_id"]) for row in cursor]


def select_by_tier(connection: sqlite3.Connection, tier: str) -> List[str]:
    """Match ids in one validation tier."""
    cursor = connection.execute(
        "SELECT match_id FROM matches WHERE validation_tier = ? ORDER BY match_id", (str(tier),)
    )
    return [str(row["match_id"]) for row in cursor]


def select_publishable(connection: sqlite3.Connection) -> List[str]:
    """Match ids admitted to the published lake."""
    cursor = connection.execute(
        "SELECT match_id FROM matches WHERE validation_tier IN ('clean', 'warning', 'source_gap')"
        " ORDER BY match_id"
    )
    return [str(row["match_id"]) for row in cursor]


def summary(connection: sqlite3.Connection) -> Dict[str, Any]:
    """Counts by tier, maturity state, parser version and raw availability."""
    def counts_by(column: str) -> Dict[str, int]:
        cursor = connection.execute(
            f"SELECT {column} AS key, COUNT(*) AS n FROM matches GROUP BY {column} ORDER BY n DESC"
        )
        return {str(row["key"]): int(row["n"]) for row in cursor}

    return {
        "total_matches": count_matches(connection),
        "by_validation_tier": counts_by("validation_tier"),
        "by_maturity_state": counts_by("maturity_state"),
        "by_parser_version": counts_by("parser_version"),
        "by_schema_version": counts_by("schema_version"),
        "raw_available": counts_by("raw_available"),
        "by_failure_kind": counts_by("last_failure_kind"),
    }


# ---------------------------------------------------------------------------
# The write path
# ---------------------------------------------------------------------------

def upsert_fetch_and_build(
    connection: sqlite3.Connection,
    match_id: str,
    *,
    raw_archive: Optional[str] = None,
    raw_sha256: Optional[str] = None,
    raw_fetched_at: Optional[str] = None,
    built_archive: Optional[str] = None,
    built_sha256: Optional[str] = None,
    parser_version: Optional[str] = None,
    schema_version: Optional[str] = None,
    validation_tier: Optional[str] = None,
    completeness_gaps: Optional[Sequence[str]] = None,
    maturity_state: Optional[str] = None,
    next_refresh_at: Optional[str] = None,
    failure_kind: Optional[str] = None,
    error: Optional[str] = None,
    index_meta: Optional[Dict[str, Any]] = None,
    identity: Optional[Dict[str, Any]] = None,
    now_utc: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Record the outcome of a fetch and its build.

    The idempotency rule lives here: when the raw payload is unchanged and
    the parser version is unchanged, there is nothing new to publish. The
    attempt counters still move, because the match is still being watched,
    but no built artifact is emitted and the built digest is left alone.

    Returns a summary of what changed.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    now_text = now_utc.astimezone(timezone.utc).isoformat()
    match_id = str(match_id)

    existing = get_match(connection, match_id)
    raw_changed = existing is None or raw_sha256 != existing["raw_sha256"]
    parser_changed = (
        existing is None or parser_version is None
        or existing["parser_version"] != parser_version
    )
    raw_unchanged = existing is not None and raw_sha256 == existing["raw_sha256"]
    same_parser = (
        existing is not None and parser_version is not None
        and existing["parser_version"] == parser_version
    )
    rebuild_required = raw_changed or parser_changed or existing is None

    fields: Dict[str, Any] = {
        "fetch_attempts": int(existing["fetch_attempts"]) + 1 if existing is not None else 1,
        "last_attempt_at": now_text,
        "updated_at": now_text,
    }

    if raw_available_fields_present(raw_archive, raw_sha256):
        fields.update({
            "raw_available": 1,
            "raw_archive": raw_archive,
            "raw_sha256": raw_sha256,
            "raw_fetched_at": raw_fetched_at or now_text,
        })

    if rebuild_required:
        if built_archive is not None:
            fields["built_archive"] = built_archive
        if built_sha256 is not None:
            fields["built_sha256"] = built_sha256
        if parser_version is not None:
            fields["parser_version"] = parser_version
        if schema_version is not None:
            fields["schema_version"] = schema_version
        if validation_tier is not None:
            fields["validation_tier"] = validation_tier
        if completeness_gaps is not None:
            fields["completeness_gaps_json"] = json.dumps(list(completeness_gaps))
        if maturity_state is not None:
            fields["maturity_state"] = maturity_state
        if next_refresh_at is not None:
            fields["next_refresh_at"] = next_refresh_at
        if identity:
            fields.update(identity)
        if index_meta is not None:
            fields["index_meta_json"] = json.dumps(index_meta, ensure_ascii=False)
        if existing is None or not existing["first_published_at"]:
            fields["first_published_at"] = now_text
        fields["last_failure_kind"] = failure_kind
        fields["last_error"] = error
    else:
        # Nothing changed. The attempt counter moved, and so does the
        # maturity state: a match that has been refreshed often enough is
        # settled with whatever gaps remain.
        if maturity_state is not None:
            fields["maturity_state"] = maturity_state
        if next_refresh_at is not None:
            fields["next_refresh_at"] = next_refresh_at
        if failure_kind is not None:
            fields["last_failure_kind"] = failure_kind
        if error is not None:
            fields["last_error"] = error

    upsert_match(connection, match_id, **fields)
    return {
        "match_id": match_id,
        "rebuild_required": rebuild_required,
        "raw_changed": raw_changed,
        "raw_unchanged": raw_unchanged,
        "same_parser": same_parser,
    }


def raw_available_fields_present(raw_archive: Optional[str], raw_sha256: Optional[str]) -> bool:
    """Whether the caller supplied a raw payload to record."""
    return bool(raw_archive) and bool(raw_sha256)


def record_run(
    connection: sqlite3.Connection,
    run_id: str,
    workflow: str,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    summary: Optional[Dict[str, Any]] = None,
) -> None:
    """Record one pipeline run."""
    connection.execute(
        "INSERT INTO runs (run_id, workflow, started_at, finished_at, summary_json)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(run_id) DO UPDATE SET"
        "   workflow = excluded.workflow, finished_at = excluded.finished_at,"
        "   summary_json = excluded.summary_json",
        (
            str(run_id),
            str(workflow),
            started_at or utc_now_iso(),
            finished_at,
            json.dumps(summary, ensure_ascii=False) if summary is not None else None,
        ),
    )


def export_matches_index_rows(connection: sqlite3.Connection) -> List[Dict[str, Any]]:
    """The rows of ``matches_index.csv``, taken straight from the catalog.

    The catalog stores a compact summary of every match, so the index can be
    written without opening 18,000 match JSONs.
    """
    rows: List[Dict[str, Any]] = []
    cursor = connection.execute("SELECT * FROM matches ORDER BY match_date, match_id")
    for row in cursor:
        meta = {}
        if row["index_meta_json"]:
            try:
                meta = json.loads(row["index_meta_json"])
            except json.JSONDecodeError:
                meta = {}
        record = {
            "match_id": row["match_id"],
            "date": row["match_date"],
            "format": row["format"],
            "format_bucket": row["format_bucket"],
            "competition": row["competition"],
            "season": row["season"],
            "team1": row["team1"],
            "team2": row["team2"],
            "winner": row["winner"],
            "result": row["result"],
            "gender": row["gender"],
            "schema_version": row["schema_version"],
            "validation_status": row["validation_tier"],
            "parser_version": row["parser_version"],
        }
        record.update(meta)
        rows.append(record)
    return rows
