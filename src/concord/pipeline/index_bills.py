"""Stage 2 — Bills indexer.

Populates the ``bills_fts`` FTS5 virtual table from the ``bills`` table.
Truncate-then-repopulate keeps the index in lockstep with the current
projection without triggers.

Per Phase 2b, the indexer also pulls a bill's first short title from
``bill_titles`` and its CRS subjects from ``bill_subjects`` so federated
search matches on short-title and subject text once enrichment has run.
Tier-1-only bills are still indexed; their short_title/subjects columns
are simply empty.

Bill search is FTS5-only in Phase 2a/2b; embeddings come in Phase 5 when
the ``chunks`` table generalizes to ``source_type='bill'`` per ADR 0008.
"""

import sqlite3
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
            _insert_bill_fts_row(conn, row)
        conn.commit()
        return IndexStats(indexed_bills=len(rows))
    finally:
        storage.close()


def reindex_one(*, db_path: Path, bill_id: str) -> None:
    """Re-build the single ``bills_fts`` row for ``bill_id``.

    DELETE the matching FTS row (no-op if absent), then re-INSERT
    using the same SELECT shape as the bulk path. The bill must
    exist in the ``bills`` table; missing bills are silently
    skipped so a partial enrichment-failure flow doesn't 500 on
    the reindex pass.
    """
    storage = SqliteStorage(db_path, load_vec=False)
    try:
        conn = storage.connection
        row = conn.execute(
            "SELECT bill_id, bill_type, bill_number, title, policy_area "
            "FROM bills WHERE bill_id = ?",
            (bill_id,),
        ).fetchone()
        if row is None:
            return
        conn.execute("DELETE FROM bills_fts WHERE bill_id = ?", (bill_id,))
        _insert_bill_fts_row(conn, row)
        conn.commit()
    finally:
        storage.close()


def _insert_bill_fts_row(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Insert one ``bills_fts`` row computed from one ``bills`` row.

    Shared by the bulk :func:`index` and the per-bill :func:`reindex_one`
    so the FTS payload shape stays in lockstep.
    """
    identifier = f"{row['bill_type']} {row['bill_number']}"
    short_title_row = conn.execute(
        "SELECT title_text FROM bill_titles "
        "WHERE bill_id = ? AND title_type LIKE 'Short Title%' "
        "ORDER BY ord LIMIT 1",
        (row["bill_id"],),
    ).fetchone()
    short_title = short_title_row["title_text"] if short_title_row else ""
    # GROUP_CONCAT row order is not guaranteed without an inner ORDER BY
    # — fix it to alphabetical so a re-index produces byte-identical
    # bills_fts.subjects content across runs.
    subjects_row = conn.execute(
        "SELECT GROUP_CONCAT(subject, ' | ') AS joined "
        "FROM (SELECT subject FROM bill_subjects WHERE bill_id = ? "
        "      ORDER BY subject ASC)",
        (row["bill_id"],),
    ).fetchone()
    subjects = (subjects_row["joined"] if subjects_row else None) or ""
    conn.execute(
        "INSERT INTO bills_fts "
        "(bill_id, identifier, title, policy_area, short_title, subjects) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            row["bill_id"],
            identifier,
            row["title"],
            row["policy_area"],
            short_title,
            subjects,
        ),
    )


__all__ = ["IndexStats", "index", "reindex_one"]
