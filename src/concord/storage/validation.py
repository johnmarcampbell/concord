"""Load Validation Failure mirror-table storage helpers (ADR 0023)."""

import json
import sqlite3
from collections.abc import Sequence
from typing import Any

from concord.models.validation import ValidationFailure
from concord.storage._sql import insert_sql

VALIDATION_FAILURES_SCHEMA = """
-- validation_failures is a MIRROR table (ADR 0019/0023): one row per Stage-1
-- model-contract rejection (X.from_congress_api raised). Rebuildable from the
-- canonical JSONL, so a re-load CONVERGES it via replace-on-load (DELETE the
-- scope, INSERT the current set) rather than appending. No run_id and no
-- load timestamp, on purpose -- a wall-clock column would break byte-identical
-- rebuild. entity_key is the PARENT natural key for child rows (bill_id for
-- bill sections, vote_id for positions). Contrast runs/run_events, which are
-- record tables (ADR 0021). payload holds the offending value as sorted JSON.
CREATE TABLE IF NOT EXISTS validation_failures (
    entity       TEXT NOT NULL,
    entity_key   TEXT NOT NULL,
    source_file  TEXT NOT NULL,
    exc_type     TEXT NOT NULL,
    exc_msg      TEXT NOT NULL,
    field_path   TEXT,
    payload      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_validation_failures_entity_key
    ON validation_failures (entity, entity_key);
"""

_VALIDATION_FAILURE_COLUMNS: tuple[str, ...] = (
    "entity",
    "entity_key",
    "source_file",
    "exc_type",
    "exc_msg",
    "field_path",
    "payload",
)

_VF_INSERT_SQL = insert_sql("validation_failures", _VALIDATION_FAILURE_COLUMNS)


def m004_add_validation_failures(conn: sqlite3.Connection) -> None:
    """ADR 0023: the ``validation_failures`` mirror table.

    ``CREATE TABLE IF NOT EXISTS`` — a no-op on fresh installs (whose
    ``_BASE_SCHEMA`` already declares it) and a creator on pre-0023 DBs. The DDL
    must stay byte-equivalent to the ``_BASE_SCHEMA`` declaration or the
    schema-equivalence test (ADR 0017) fails.
    """
    conn.executescript(VALIDATION_FAILURES_SCHEMA)


def replace_validation_failures(
    conn: sqlite3.Connection,
    failures: Sequence[ValidationFailure],
    *,
    entities: Sequence[str],
    entity_key: str | None = None,
) -> None:
    """Replace-on-load: DELETE the scope, then INSERT ``failures`` (ADR 0023).

    ``entities`` is the family being re-loaded (the delete is ``entity IN (...)``).
    ``entity_key`` narrows the delete for the single-entity ``load_one`` path;
    ``None`` clears the whole family (the full-load path). Always call this — even
    with an empty ``failures`` — so a load that now parses cleanly clears stale
    rows. Pure SQL; the caller owns the transaction.
    """
    placeholders = ", ".join("?" for _ in entities)
    if entity_key is None:
        conn.execute(
            f"DELETE FROM validation_failures WHERE entity IN ({placeholders})",  # noqa: S608 - static placeholders, parameterized values
            tuple(entities),
        )
    else:
        conn.execute(
            f"DELETE FROM validation_failures WHERE entity IN ({placeholders}) AND entity_key = ?",  # noqa: S608 - static placeholders, parameterized values
            (*entities, entity_key),
        )
    if failures:
        conn.executemany(_VF_INSERT_SQL, [_row_from_failure(f) for f in failures])


def count_validation_failures(conn: sqlite3.Connection, *, entity: str | None = None) -> int:
    """Row count, optionally filtered to one ``entity`` — for tests/diagnostics."""
    if entity is None:
        return int(conn.execute("SELECT COUNT(*) FROM validation_failures").fetchone()[0])
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM validation_failures WHERE entity = ?", (entity,)
        ).fetchone()[0]
    )


def _row_from_failure(f: ValidationFailure) -> tuple[Any, ...]:
    """Project a :class:`ValidationFailure` into the column tuple; payload is
    dumped with sorted keys for a byte-stable row (matches runs storage)."""
    values: dict[str, Any] = {
        "entity": f.entity,
        "entity_key": f.entity_key,
        "source_file": f.source_file,
        "exc_type": f.exc_type,
        "exc_msg": f.exc_msg,
        "field_path": f.field_path,
        "payload": json.dumps(f.payload, sort_keys=True),
    }
    return tuple(values[col] for col in _VALIDATION_FAILURE_COLUMNS)
