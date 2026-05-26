"""Stage 2 — Bills indexer.

Populates the ``bills_fts`` FTS5 virtual table from the ``bills`` table.
Truncate-then-repopulate keeps the index in lockstep with the current
projection without triggers.

Bill search is FTS5-only in Phase 2a; embeddings come in Phase 5 when
the ``chunks`` table generalizes to ``source_type='bill'`` per ADR 0008.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from concord.storage.sqlite import SqliteStorage


class IndexStats(NamedTuple):
    """Outcome of one :func:`index` invocation."""

    indexed_bills: int


def index(*, db_path: Path, limit: int | None = None) -> IndexStats:
    """Repopulate ``bills_fts`` from the current ``bills`` rows."""
    storage = SqliteStorage(db_path, load_vec=False)
    try:
        conn = storage.connection
        conn.execute("DELETE FROM bills_fts")
        sql = "SELECT bill_id, bill_type, bill_number, title, policy_area FROM bills"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        rows = conn.execute(sql).fetchall()
        for row in rows:
            identifier = f"{row['bill_type']} {row['bill_number']}"
            conn.execute(
                "INSERT INTO bills_fts (bill_id, identifier, title, policy_area) "
                "VALUES (?, ?, ?, ?)",
                (row["bill_id"], identifier, row["title"], row["policy_area"]),
            )
        conn.commit()
        return IndexStats(indexed_bills=len(rows))
    finally:
        storage.close()


__all__ = ["IndexStats", "index"]
