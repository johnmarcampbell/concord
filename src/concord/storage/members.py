"""Member identity storage helpers (Phase 1, ADR 0007).

Owns the ``members`` / ``member_terms`` mirror tables (plus the ``members_fts``
search index): their DDL, column tuples, INSERT/UPSERT SQL, the Member/Term
row serializers, and the persistence/query helpers. ``SqliteStorage`` composes
these and owns the transaction boundary; the helpers here are pure SQL over a
connection.
"""

import sqlite3
from collections.abc import Sequence
from typing import Any

from concord.models.members import Member, Term
from concord.storage._ddl import rebuild_table_add_not_null
from concord.storage._sql import insert_sql, upsert_sql

MEMBERS_SCHEMA = """
-- Members (Phase 1). Per-person identity fields; per-Term records the
-- mutable career attributes (party, chamber, state, district). See ADR 0007.
CREATE TABLE IF NOT EXISTS members (
    bioguide_id  TEXT PRIMARY KEY,
    first_name   TEXT NOT NULL,
    middle_name  TEXT,
    last_name    TEXT NOT NULL,
    suffix       TEXT,
    birth_year   INTEGER,
    death_year   INTEGER,
    display_name TEXT NOT NULL,
    photo_url    TEXT,
    biography    TEXT,
    fetched_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS member_terms (
    bioguide_id TEXT NOT NULL REFERENCES members(bioguide_id) ON DELETE CASCADE,
    congress    INTEGER NOT NULL,
    chamber     TEXT NOT NULL CHECK (chamber IN ('house', 'senate')),
    party       TEXT NOT NULL,
    state       TEXT NOT NULL,
    district    INTEGER,
    start_date  TEXT NOT NULL,
    end_date    TEXT NOT NULL,
    PRIMARY KEY (bioguide_id, congress, chamber)
);

CREATE INDEX IF NOT EXISTS idx_member_terms_congress
    ON member_terms (congress);
CREATE INDEX IF NOT EXISTS idx_member_terms_state
    ON member_terms (state);

CREATE VIRTUAL TABLE IF NOT EXISTS members_fts USING fts5(
    bioguide_id UNINDEXED,
    direct_order_name,
    inverted_order_name,
    last_name,
    tokenize = 'porter'
);
"""

# Column lists for members + member_terms tables. One source of truth for
# INSERT order, mirroring the ``_PROCEEDING_COLUMNS`` pattern.
_MEMBER_COLUMNS: tuple[str, ...] = (
    "bioguide_id",
    "first_name",
    "middle_name",
    "last_name",
    "suffix",
    "birth_year",
    "death_year",
    "display_name",
    "photo_url",
    "biography",
    "fetched_at",
)

_TERM_COLUMNS: tuple[str, ...] = (
    "bioguide_id",
    "congress",
    "chamber",
    "party",
    "state",
    "district",
    "start_date",
    "end_date",
)

_MEMBER_UPSERT_SQL = upsert_sql("members", _MEMBER_COLUMNS, conflict=("bioguide_id",))
_TERM_INSERT_SQL = insert_sql("member_terms", _TERM_COLUMNS)


def m005_member_terms_not_null(conn: sqlite3.Connection) -> None:
    """ADR 0024: tighten ``member_terms.{party,start_date,end_date}`` to ``NOT NULL``.

    Guarded table rebuild — a no-op on fresh installs whose ``_BASE_SCHEMA``
    already declares the constraint. Any legacy row holding a ``NULL`` in these
    columns is dropped (derived state, rebuildable from JSONL per ADR 0002). The
    rebuild preserves the chamber CHECK and the FK to ``members`` by injecting
    ``NOT NULL`` into the live DDL.
    """
    rebuild_table_add_not_null(
        conn, table="member_terms", not_null_columns=("party", "start_date", "end_date")
    )


def upsert_member(
    conn: sqlite3.Connection,
    member: Member,
    terms: Sequence[Term],
    *,
    fetched_at: str,
) -> None:
    """Project a Member + its Terms into SQLite.

    The Member row is UPSERTed on ``bioguide_id``; the Term rows for this
    Member are replaced (DELETE-then-INSERT) so the projection stays
    consistent with the latest snapshot, including the case where the
    upstream API drops a Term that used to be present. Pure SQL over the
    connection — the caller/facade transaction provides the atomic boundary
    that lands the member row and its terms together (``SqliteStorage`` wraps
    this in ``_maybe_transaction``).
    """
    member_row = _row_from_member(member, fetched_at=fetched_at)
    term_rows = [_row_from_term(t) for t in terms]
    conn.execute(_MEMBER_UPSERT_SQL, member_row)
    conn.execute(
        "DELETE FROM member_terms WHERE bioguide_id = ?",
        (member.bioguide_id,),
    )
    if term_rows:
        conn.executemany(_TERM_INSERT_SQL, term_rows)


def get_member(conn: sqlite3.Connection, bioguide_id: str) -> sqlite3.Row | None:
    """Return the ``members`` row for ``bioguide_id``, or ``None`` if absent."""
    cursor = conn.execute(
        "SELECT * FROM members WHERE bioguide_id = ?",
        (bioguide_id,),
    )
    row: sqlite3.Row | None = cursor.fetchone()
    return row


def terms_for_member(conn: sqlite3.Connection, bioguide_id: str) -> list[sqlite3.Row]:
    """Return every ``member_terms`` row for ``bioguide_id``, ordered by congress then chamber."""
    cursor = conn.execute(
        "SELECT * FROM member_terms WHERE bioguide_id = ? ORDER BY congress ASC, chamber ASC",
        (bioguide_id,),
    )
    return cursor.fetchall()


def _row_from_member(member: Member, *, fetched_at: str) -> tuple[Any, ...]:
    """Project a :class:`Member` into the column tuple expected by SQL."""
    dumped: dict[str, Any] = member.model_dump(mode="json")
    dumped["fetched_at"] = fetched_at
    return tuple(dumped[col] for col in _MEMBER_COLUMNS)


def _row_from_term(term: Term) -> tuple[Any, ...]:
    """Project a :class:`Term` into the column tuple expected by SQL."""
    dumped: dict[str, Any] = term.model_dump(mode="json")
    return tuple(dumped[col] for col in _TERM_COLUMNS)
