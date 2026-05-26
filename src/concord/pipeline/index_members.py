"""Stage 2 — Members indexer.

Populates the ``members_fts`` FTS5 virtual table from the ``members``
table. Truncate-then-repopulate keeps the index in lockstep with the
current Member projection — no need for triggers, no risk of stale rows
after a re-load.

Member name search uses FTS5 only; per the Phase 1 plan there's no
embedding pass for Member names (BM25 + porter stemming is the right
tool for short proper-noun lookups).
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from concord.storage.sqlite import SqliteStorage


class IndexStats(NamedTuple):
    """Outcome of one :func:`index` invocation."""

    indexed_members: int


def index(*, db_path: Path) -> IndexStats:
    storage = SqliteStorage(db_path, load_vec=False)
    try:
        conn = storage.connection
        # Truncate. FTS5 supports DELETE; this is preferred over the
        # 'delete-all' command since it works without external content.
        conn.execute("DELETE FROM members_fts")
        cursor = conn.execute(
            "SELECT bioguide_id, display_name, first_name, last_name FROM members"
        )
        rows = cursor.fetchall()
        for row in rows:
            inverted = f"{row['last_name']}, {row['first_name']}".strip(", ")
            conn.execute(
                "INSERT INTO members_fts "
                "(bioguide_id, direct_order_name, inverted_order_name, last_name) "
                "VALUES (?, ?, ?, ?)",
                (
                    row["bioguide_id"],
                    row["display_name"],
                    inverted,
                    row["last_name"],
                ),
            )
        conn.commit()
        return IndexStats(indexed_members=len(rows))
    finally:
        storage.close()


__all__ = ["IndexStats", "index"]
