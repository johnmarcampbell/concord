"""Scrape Run ledger storage helpers (ADR 0021)."""

import json
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from typing import Any

from concord.models import RunEvent, RunRecord

RUNS_SCHEMA = """
-- runs + run_events are RECORD tables (ADR 0019), not mirrors: each row is
-- the durable ledger of one Stage-0 Scrape Run (ADR 0021). They are
-- Concord-originated, not rebuildable from upstream JSONL, and an entity
-- re-derivation (re-run scrape/load/index) must NOT touch them — ensure_schema
-- is CREATE IF NOT EXISTS so they survive a rebuild. runs.jsonl is written
-- alongside as a cold backup; this table is the queried source of truth.
-- success_counts / throttle_counts / unmatched_sample hold JSON; throttle_counts
-- is reserved for a later throttle-aware metric. status is ok/partial/error.
CREATE TABLE IF NOT EXISTS runs (
    run_id             TEXT PRIMARY KEY,
    entity             TEXT NOT NULL,
    command            TEXT NOT NULL,
    started_at         TEXT NOT NULL,
    ended_at           TEXT,
    status             TEXT NOT NULL,
    success_counts     TEXT NOT NULL,
    throttle_counts    TEXT,
    unmatched_sample   TEXT,
    error_event_count  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS run_events (
    run_id           TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    seq              INTEGER NOT NULL,
    endpoint_bucket  TEXT NOT NULL,
    attempts         TEXT NOT NULL,
    overflow_count   INTEGER NOT NULL DEFAULT 0,
    final_status     TEXT NOT NULL,
    ts               TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
"""

_RUN_COLUMNS: tuple[str, ...] = (
    "run_id",
    "entity",
    "command",
    "started_at",
    "ended_at",
    "status",
    "success_counts",
    "throttle_counts",
    "unmatched_sample",
    "error_event_count",
)

_RUN_EVENT_COLUMNS: tuple[str, ...] = (
    "run_id",
    "seq",
    "endpoint_bucket",
    "attempts",
    "overflow_count",
    "final_status",
    "ts",
)


def _insert_sql(table: str, columns: tuple[str, ...]) -> str:
    placeholders = ", ".join("?" for _ in columns)
    return f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"  # noqa: S608 - static table/column tuples


_RUN_INSERT_SQL = _insert_sql("runs", _RUN_COLUMNS)
_RUN_EVENT_INSERT_SQL = _insert_sql("run_events", _RUN_EVENT_COLUMNS)


def m003_add_runs_tables(conn: sqlite3.Connection) -> None:
    """ADR 0021: the ``runs`` + ``run_events`` Scrape Run ledger tables.

    ``CREATE TABLE IF NOT EXISTS`` is idempotent against fresh installs
    (whose ``_BASE_SCHEMA`` already declares both tables; this is a no-op)
    and against pre-0021 DBs (which gain the tables here). The DDL must stay
    byte-equivalent to the ``_BASE_SCHEMA`` declaration — the
    schema-equivalence test (ADR 0017) fails if they drift.
    """
    conn.executescript(RUNS_SCHEMA)


def insert_run(
    conn: sqlite3.Connection,
    maybe_transaction: Callable[[], AbstractContextManager[None]],
    run: RunRecord,
) -> None:
    """INSERT one ``runs`` ledger row from a :class:`RunRecord` (ADR 0021)."""
    with maybe_transaction():
        conn.execute(_RUN_INSERT_SQL, _row_from_run(run))


def insert_run_events(
    conn: sqlite3.Connection,
    maybe_transaction: Callable[[], AbstractContextManager[None]],
    run_id: str,
    events: Sequence[RunEvent],
) -> None:
    """Bulk-INSERT the :class:`RunEvent` rows for one Scrape Run (ADR 0021)."""
    if not events:
        return
    rows = [_row_from_run_event(run_id, seq, event) for seq, event in enumerate(events)]
    with maybe_transaction():
        conn.executemany(_RUN_EVENT_INSERT_SQL, rows)


def get_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    """Return the ``runs`` row for ``run_id``, or ``None`` if absent."""
    cursor = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    row: sqlite3.Row | None = cursor.fetchone()
    return row


def list_run_events(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    """Return every ``run_events`` row for ``run_id``, ordered by ``seq``."""
    cursor = conn.execute(
        "SELECT * FROM run_events WHERE run_id = ? ORDER BY seq ASC",
        (run_id,),
    )
    return cursor.fetchall()


def _row_from_run(run: RunRecord) -> tuple[Any, ...]:
    """Project a :class:`RunRecord` into the ``runs`` column tuple (ADR 0021).

    Owns the JSON-column serialization: ``success_counts`` /
    ``throttle_counts`` / ``unmatched_sample`` are dumped with sorted keys for
    a byte-stable row, and an empty ``unmatched_sample`` or absent
    ``throttle_counts`` collapses to SQL ``NULL``.
    """
    values: dict[str, Any] = {
        "run_id": run.run_id,
        "entity": run.entity,
        "command": run.command,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "status": run.status,
        "success_counts": json.dumps(run.success_counts, sort_keys=True),
        "throttle_counts": (
            json.dumps(run.throttle_counts, sort_keys=True)
            if run.throttle_counts is not None
            else None
        ),
        "unmatched_sample": (
            json.dumps(run.unmatched_sample, sort_keys=True) if run.unmatched_sample else None
        ),
        "error_event_count": run.error_event_count,
    }
    return tuple(values[col] for col in _RUN_COLUMNS)


def _row_from_run_event(run_id: str, seq: int, event: RunEvent) -> tuple[Any, ...]:
    """Project one :class:`RunEvent` into the ``run_events`` column tuple.

    ``run_id`` / ``seq`` are supplied by the caller (the parent run and the
    event's position); ``attempts`` is dumped with sorted keys to match
    :func:`_row_from_run`'s byte-stable policy.
    """
    values: dict[str, Any] = {
        "run_id": run_id,
        "seq": seq,
        "endpoint_bucket": event.endpoint_bucket,
        "attempts": json.dumps(
            [attempt.model_dump() for attempt in event.attempts], sort_keys=True
        ),
        "overflow_count": event.overflow_count,
        "final_status": event.final_status,
        "ts": event.ts,
    }
    return tuple(values[col] for col in _RUN_EVENT_COLUMNS)
