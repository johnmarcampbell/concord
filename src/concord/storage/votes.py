"""Vote storage helpers (Phase 3a, ADR 0006).

Owns the ``votes`` / ``vote_positions`` mirror tables and the
``member_party_unity`` aggregate the Stage-2 indexer recomputes: their DDL,
column tuples, INSERT/UPSERT SQL, the Vote row serializer, and the
persistence/query helpers. ``SqliteStorage`` composes these and owns the
transaction boundary; the helpers here are pure SQL over a connection.
"""

import logging
import sqlite3
from collections.abc import Sequence
from typing import Any

from concord.models.votes import Vote, VotePosition
from concord.storage._ddl import rebuild_table_add_not_null
from concord.storage._sql import upsert_sql

_log = logging.getLogger(__name__)

VOTES_SCHEMA = """
-- Votes (Phase 3a). One row per recorded roll-call decision in a chamber.
-- ``vote_id`` flattens the natural key per ADR 0006-style chamber/congress/
-- session/roll. ``bill_id`` and ``amendment_id`` are bare TEXT (no FK) so
-- ingest is robust to gaps in the Bills / Amendments tables.
-- ``is_party_unity`` is denormalized for join-free filtering in the
-- party-unity numerator query; populated by the indexer.
CREATE TABLE IF NOT EXISTS votes (
    vote_id              TEXT PRIMARY KEY,
    chamber              TEXT NOT NULL
        CHECK (chamber IN ('house', 'senate')),
    congress             INTEGER NOT NULL,
    session              INTEGER NOT NULL
        CHECK (session IN (1, 2)),
    roll_number          INTEGER NOT NULL,
    vote_kind            TEXT NOT NULL
        CHECK (vote_kind IN ('standard', 'election')),
    start_date           TEXT NOT NULL,
    vote_question        TEXT NOT NULL,
    vote_type            TEXT NOT NULL,
    threshold            TEXT
        CHECK (threshold IN ('simple_majority', 'two_thirds', 'three_fifths')
               OR threshold IS NULL),
    result               TEXT NOT NULL,
    yea_count            INTEGER,
    nay_count            INTEGER,
    present_count        INTEGER,
    not_voting_count     INTEGER,
    bill_id              TEXT,
    amendment_id         TEXT,
    is_party_unity       INTEGER NOT NULL DEFAULT 0,
    update_date          TEXT NOT NULL,
    fetched_at           TEXT NOT NULL,
    UNIQUE (chamber, congress, session, roll_number)
);

CREATE INDEX IF NOT EXISTS idx_votes_bill        ON votes (bill_id);
CREATE INDEX IF NOT EXISTS idx_votes_amendment   ON votes (amendment_id);
CREATE INDEX IF NOT EXISTS idx_votes_date        ON votes (start_date DESC);
CREATE INDEX IF NOT EXISTS idx_votes_congress    ON votes (congress);

CREATE TABLE IF NOT EXISTS vote_positions (
    vote_id      TEXT NOT NULL,
    bioguide_id  TEXT NOT NULL,
    position     TEXT NOT NULL,
    vote_party   TEXT NOT NULL,
    vote_state   TEXT NOT NULL,
    PRIMARY KEY (vote_id, bioguide_id)
);

CREATE INDEX IF NOT EXISTS idx_vote_positions_member
    ON vote_positions (bioguide_id);

-- member_party_unity is computed by the Stage 2 indexer; truncated and
-- repopulated each run. Independents are not written (party constraint
-- forbids 'I'); the UI shows a separate muted treatment.
CREATE TABLE IF NOT EXISTS member_party_unity (
    bioguide_id              TEXT NOT NULL,
    congress                 INTEGER NOT NULL,
    chamber                  TEXT NOT NULL
        CHECK (chamber IN ('house', 'senate')),
    party                    TEXT NOT NULL
        CHECK (party IN ('R', 'D')),
    party_unity_votes_cast   INTEGER NOT NULL,
    party_line_votes         INTEGER NOT NULL,
    PRIMARY KEY (bioguide_id, congress, chamber)
);
"""

_VOTE_COLUMNS: tuple[str, ...] = (
    "vote_id",
    "chamber",
    "congress",
    "session",
    "roll_number",
    "vote_kind",
    "start_date",
    "vote_question",
    "vote_type",
    "threshold",
    "result",
    "yea_count",
    "nay_count",
    "present_count",
    "not_voting_count",
    "bill_id",
    "amendment_id",
    "is_party_unity",
    "update_date",
    "fetched_at",
)

_VOTE_UPSERT_SQL = upsert_sql("votes", _VOTE_COLUMNS, conflict=("vote_id",))

_VOTE_POSITION_COLUMNS: tuple[str, ...] = (
    "vote_id",
    "bioguide_id",
    "position",
    "vote_party",
    "vote_state",
)

# vote_positions is replaced wholesale per vote (DELETE-then-INSERT), so a
# per-row INSERT OR REPLACE is the right conflict policy, not an UPSERT.
_VOTE_POSITION_INSERT_SQL = (
    "INSERT OR REPLACE INTO vote_positions ("  # noqa: S608 - static table/column names
    + ", ".join(_VOTE_POSITION_COLUMNS)
    + ") VALUES ("
    + ", ".join("?" for _ in _VOTE_POSITION_COLUMNS)
    + ")"
)


def m007_vote_positions_not_null(conn: sqlite3.Connection) -> None:
    """ADR 0024: tighten ``vote_positions.{vote_party,vote_state}`` to ``NOT NULL``.

    Guarded table rebuild — a no-op on fresh installs whose ``_BASE_SCHEMA``
    already declares the constraint. Any legacy row holding a ``NULL`` in these
    columns is dropped (derived state, rebuildable from JSONL per ADR 0002) and the
    count logged. ``vote_positions`` has no FK or CHECK, so the rebuild is a plain
    structural copy.
    """
    dropped = rebuild_table_add_not_null(
        conn, table="vote_positions", not_null_columns=("vote_party", "vote_state")
    )
    if dropped:
        _log.warning(
            "m007: dropped %d vote_positions row(s) with NULL vote_party/vote_state", dropped
        )


def upsert_vote(conn: sqlite3.Connection, vote: Vote, *, fetched_at: str) -> None:
    """UPSERT one Vote row keyed on ``vote_id`` (latest snapshot wins, ADR 0006).

    The ``is_party_unity`` column is overwritten here; the indexer's UPDATE
    pass runs after the loader and re-establishes it across all rows.
    """
    conn.execute(_VOTE_UPSERT_SQL, _row_from_vote(vote, fetched_at=fetched_at))


def replace_vote_positions(
    conn: sqlite3.Connection,
    vote_id: str,
    positions: Sequence[VotePosition],
) -> None:
    """DELETE-then-INSERT every position row for one vote.

    If the latest members snapshot for a vote no longer carries a Member,
    that Member's position is dropped.
    """
    conn.execute("DELETE FROM vote_positions WHERE vote_id = ?", (vote_id,))
    if positions:
        conn.executemany(
            _VOTE_POSITION_INSERT_SQL,
            [(vote_id, p.bioguide_id, p.position, p.vote_party, p.vote_state) for p in positions],
        )


def get_vote(conn: sqlite3.Connection, vote_id: str) -> sqlite3.Row | None:
    """Return the ``votes`` row for ``vote_id``, or ``None`` if absent."""
    cursor = conn.execute("SELECT * FROM votes WHERE vote_id = ?", (vote_id,))
    row: sqlite3.Row | None = cursor.fetchone()
    return row


def list_vote_positions_for_vote(conn: sqlite3.Connection, vote_id: str) -> list[sqlite3.Row]:
    """Return every ``vote_positions`` row for ``vote_id``, ordered by bioguide_id."""
    cursor = conn.execute(
        "SELECT * FROM vote_positions WHERE vote_id = ? ORDER BY bioguide_id ASC",
        (vote_id,),
    )
    return cursor.fetchall()


def list_recent_votes_for_member(
    conn: sqlite3.Connection,
    bioguide_id: str,
    *,
    limit: int = 25,
) -> list[sqlite3.Row]:
    """Return a member's most recent votes, each with their ``member_position``."""
    cursor = conn.execute(
        """
        SELECT v.*, p.position AS member_position
        FROM votes v
        JOIN vote_positions p ON p.vote_id = v.vote_id
        WHERE p.bioguide_id = ?
        ORDER BY v.start_date DESC
        LIMIT ?
        """,
        (bioguide_id, limit),
    )
    return cursor.fetchall()


def get_party_unity_for_member(conn: sqlite3.Connection, bioguide_id: str) -> list[sqlite3.Row]:
    """Return the ``member_party_unity`` rows for ``bioguide_id``, newest congress first."""
    cursor = conn.execute(
        "SELECT * FROM member_party_unity WHERE bioguide_id = ? "
        "ORDER BY congress DESC, chamber ASC",
        (bioguide_id,),
    )
    return cursor.fetchall()


def vote_history_for_bill(conn: sqlite3.Connection, bill_id: str) -> list[sqlite3.Row]:
    """Return every Vote whose subject (bill or amendment) ties back to ``bill_id``.

    Includes amendment votes whose ``bill_id`` column records the underlying
    bill (the "amendment trap" — the API returns the underlying bill in
    legislationType/Number on amendment vote payloads).
    """
    cursor = conn.execute(
        "SELECT * FROM votes WHERE bill_id = ? ORDER BY start_date DESC",
        (bill_id,),
    )
    return cursor.fetchall()


def _row_from_vote(vote: Vote, *, fetched_at: str) -> tuple[Any, ...]:
    """Project a :class:`Vote` into the column tuple expected by SQL."""
    dumped: dict[str, Any] = vote.model_dump(mode="json")
    dumped["fetched_at"] = fetched_at
    dumped["is_party_unity"] = 1 if dumped.get("is_party_unity") else 0
    return tuple(dumped[col] for col in _VOTE_COLUMNS)
