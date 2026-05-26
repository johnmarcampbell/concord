"""SQLite storage — the recommended derived store.

One file on disk that holds every derived index Concord builds: a
``proceedings`` table mirroring the :class:`Proceeding` model (Stage 1),
plus, when Stage 2 is built on top, a ``chunks`` table with an FTS5 index
(``chunks_fts``) and a ``sqlite-vec`` vector index (``chunks_vec``).

Construction creates all the schema lazily via ``CREATE … IF NOT EXISTS``.
WAL mode is enabled so the web layer can read concurrently while the
pipeline writes. Foreign keys are enabled so Stage 2's ``chunks.granule_id``
FK actually cascades.

The class isn't thread-safe — the underlying :class:`sqlite3.Connection`
is opened with ``check_same_thread=True``. Instantiate one per writer.

Parameters
----------

The ``load_vec`` constructor flag controls whether the ``sqlite-vec``
extension is loaded and the ``chunks_vec`` virtual table is created. The
default (``True``) is what the pipeline needs. Tests and any Stage-1-only
caller can pass ``load_vec=False`` to skip the extension entirely.
"""

import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any

import sqlite_vec  # type: ignore[import-untyped]

from concord.models import (
    Bill,
    BillAction,
    BillSubject,
    BillSummary,
    BillTitle,
    Cosponsor,
    Member,
    Proceeding,
    Term,
    Vote,
    VotePosition,
)

# Columns in the exact order they appear in the INSERT statement. Keeping
# this list in one place makes it easy to add a column later: extend here,
# extend _row_from_proceeding, extend the DDL.
_PROCEEDING_COLUMNS: tuple[str, ...] = (
    "granule_id",
    "issue_date",
    "congress",
    "session",
    "volume",
    "issue_number",
    "update_date",
    "section",
    "title",
    "start_page",
    "end_page",
    "text_url",
    "pdf_url",
    "text",
    "fetched_at",
)

# Schema that's always created — Stage 1's proceedings table and Stage 2's
# chunks / FTS5 tables. (chunks_vec lives separately so it can be skipped
# when load_vec=False.)
_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS proceedings (
    granule_id   TEXT PRIMARY KEY,
    issue_date   TEXT NOT NULL,
    congress     INTEGER NOT NULL,
    session      INTEGER NOT NULL,
    volume       INTEGER NOT NULL,
    issue_number INTEGER NOT NULL,
    update_date  TEXT NOT NULL,
    section      TEXT NOT NULL,
    title        TEXT NOT NULL,
    start_page   TEXT NOT NULL,
    end_page     TEXT NOT NULL,
    text_url     TEXT NOT NULL,
    pdf_url      TEXT NOT NULL,
    text         TEXT NOT NULL,
    fetched_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_proceedings_issue_date
    ON proceedings (issue_date);

CREATE INDEX IF NOT EXISTS idx_proceedings_congress
    ON proceedings (congress);

CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    granule_id  TEXT NOT NULL REFERENCES proceedings(granule_id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    text        TEXT NOT NULL,
    char_start  INTEGER NOT NULL,
    char_end    INTEGER NOT NULL,
    UNIQUE (granule_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_chunks_granule_id
    ON chunks (granule_id);

-- chunking_status records every Proceeding whose text has been considered
-- for chunking, including those whose text produced zero chunks (empty /
-- whitespace-only text). Without this, "find unchunked proceedings" would
-- yield empty-text proceedings forever. See ADR-0005 / stage-2-index plan.
CREATE TABLE IF NOT EXISTS chunking_status (
    granule_id  TEXT PRIMARY KEY REFERENCES proceedings(granule_id) ON DELETE CASCADE,
    chunked_at  TEXT NOT NULL,
    chunk_count INTEGER NOT NULL
);

-- FTS5 virtual table indexing chunks.text. External-content mode means
-- chunks owns the bytes and chunks_fts owns the index — no duplication.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    content='chunks',
    content_rowid='id',
    tokenize='porter unicode61'
);

-- Triggers keep chunks_fts in sync with chunks.
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;

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
    party       TEXT,
    state       TEXT NOT NULL,
    district    INTEGER,
    start_date  TEXT,
    end_date    TEXT,
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

-- Bills (Phase 2a). Identity record per Bill — sponsor goes here too
-- because Congress's rules cap one Sponsor per Bill. The mutable
-- political-graph data (cosponsors, actions, subjects, titles,
-- summaries) is added in Phase 2b as child tables. ``bill_id`` is the
-- flattened "{congress}-{bill_type}-{bill_number}" PK chosen so Phase 5
-- chunks linkage matches ADR 0008. The sponsor column is bare TEXT (no
-- REFERENCES members) so ingest is robust to any Phase 1 gap.
CREATE TABLE IF NOT EXISTS bills (
    bill_id              TEXT PRIMARY KEY,
    congress             INTEGER NOT NULL,
    bill_type            TEXT NOT NULL
        CHECK (bill_type IN ('hr', 'hres', 'hjres', 'hconres', 's', 'sres', 'sjres', 'sconres')),
    bill_number          INTEGER NOT NULL,
    origin_chamber       TEXT NOT NULL
        CHECK (origin_chamber IN ('House', 'Senate')),
    title                TEXT NOT NULL,
    introduced_date      TEXT,
    policy_area          TEXT,
    sponsor_bioguide_id  TEXT,
    latest_action_date   TEXT,
    latest_action_text   TEXT,
    update_date          TEXT NOT NULL,
    fetched_at           TEXT NOT NULL,
    cosponsors_fetched_at TEXT,
    actions_fetched_at    TEXT,
    subjects_fetched_at   TEXT,
    titles_fetched_at     TEXT,
    summaries_fetched_at  TEXT,
    UNIQUE (congress, bill_type, bill_number)
);

CREATE INDEX IF NOT EXISTS idx_bills_sponsor
    ON bills (sponsor_bioguide_id);
CREATE INDEX IF NOT EXISTS idx_bills_latest_action
    ON bills (latest_action_date DESC);
CREATE INDEX IF NOT EXISTS idx_bills_policy_area
    ON bills (policy_area);
CREATE INDEX IF NOT EXISTS idx_bills_congress
    ON bills (congress);

-- Tier-2 child tables (Phase 2b). Each row points back to bills via
-- bill_id with ON DELETE CASCADE so wiping a Bill takes its enrichment
-- with it. bioguide_id on bill_cosponsors is bare TEXT (no FK) so the
-- table doesn't depend on Phase 1 having indexed every cosponsoring
-- Member.
CREATE TABLE IF NOT EXISTS bill_cosponsors (
    bill_id                     TEXT NOT NULL REFERENCES bills(bill_id) ON DELETE CASCADE,
    bioguide_id                 TEXT NOT NULL,
    sponsorship_date            TEXT,
    sponsorship_withdrawn_date  TEXT,
    is_original_cosponsor       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bill_id, bioguide_id)
);
CREATE INDEX IF NOT EXISTS idx_bill_cosponsors_bioguide
    ON bill_cosponsors (bioguide_id);

CREATE TABLE IF NOT EXISTS bill_actions (
    bill_id        TEXT NOT NULL REFERENCES bills(bill_id) ON DELETE CASCADE,
    ord            INTEGER NOT NULL,
    action_date    TEXT NOT NULL,
    action_text    TEXT NOT NULL,
    action_code    TEXT,
    source_system  TEXT,
    PRIMARY KEY (bill_id, ord)
);
CREATE INDEX IF NOT EXISTS idx_bill_actions_date
    ON bill_actions (action_date DESC);

CREATE TABLE IF NOT EXISTS bill_subjects (
    bill_id  TEXT NOT NULL REFERENCES bills(bill_id) ON DELETE CASCADE,
    subject  TEXT NOT NULL,
    PRIMARY KEY (bill_id, subject)
);

CREATE TABLE IF NOT EXISTS bill_titles (
    bill_id     TEXT NOT NULL REFERENCES bills(bill_id) ON DELETE CASCADE,
    ord         INTEGER NOT NULL,
    title_type  TEXT NOT NULL,
    title_text  TEXT NOT NULL,
    chamber     TEXT,
    PRIMARY KEY (bill_id, ord)
);

CREATE TABLE IF NOT EXISTS bill_summaries (
    bill_id        TEXT NOT NULL REFERENCES bills(bill_id) ON DELETE CASCADE,
    version_code   TEXT NOT NULL,
    action_date    TEXT,
    action_desc    TEXT,
    summary_text   TEXT NOT NULL,
    PRIMARY KEY (bill_id, version_code)
);

CREATE VIRTUAL TABLE IF NOT EXISTS bills_fts USING fts5(
    bill_id UNINDEXED,
    identifier,
    title,
    policy_area,
    short_title,
    subjects,
    tokenize = 'porter'
);

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
    vote_party   TEXT,
    vote_state   TEXT,
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
    party                    TEXT NOT NULL
        CHECK (party IN ('R', 'D')),
    party_unity_votes_cast   INTEGER NOT NULL,
    party_line_votes         INTEGER NOT NULL,
    PRIMARY KEY (bioguide_id, congress)
);
"""

# Column lists for members + member_terms tables. Mirrors the ``_PROCEEDING_COLUMNS``
# pattern: one source of truth for INSERT order.
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

_MEMBER_UPSERT_SQL = (
    "INSERT INTO members ("
    + ", ".join(_MEMBER_COLUMNS)
    + ") VALUES ("
    + ", ".join("?" for _ in _MEMBER_COLUMNS)
    + ") ON CONFLICT(bioguide_id) DO UPDATE SET "
    + ", ".join(f"{col} = excluded.{col}" for col in _MEMBER_COLUMNS if col != "bioguide_id")
)

_TERM_INSERT_SQL = (
    "INSERT INTO member_terms ("
    + ", ".join(_TERM_COLUMNS)
    + ") VALUES ("
    + ", ".join("?" for _ in _TERM_COLUMNS)
    + ")"
)

_BILL_COLUMNS: tuple[str, ...] = (
    "bill_id",
    "congress",
    "bill_type",
    "bill_number",
    "origin_chamber",
    "title",
    "introduced_date",
    "policy_area",
    "sponsor_bioguide_id",
    "latest_action_date",
    "latest_action_text",
    "update_date",
    "fetched_at",
)

# The five *_fetched_at columns are upserted only when their tier-2
# loader has new snapshots to flush; the parent bills row uses the
# columns above. Listed for use by per-section UPDATE statements.
#: Cap on placeholders per ``bill_id IN (...)`` lookup. SQLite defaults to
#: 32766 in modern builds but historically 999; keeping the chunk well
#: below either limit avoids the compile-time complaints on older
#: distros without measurably hurting throughput.
_BILL_IDS_PRESENT_CHUNK = 500


BILL_TIER2_SECTIONS: tuple[str, ...] = (
    "cosponsors",
    "actions",
    "subjects",
    "titles",
    "summaries",
)

# UPSERT on the parent row. The five *_fetched_at columns are *not*
# touched here — they're owned by the tier-2 loaders, and clobbering
# them on every tier-1 upsert would reset the "enriched" state every
# time `concord load bills` runs. Keep them under per-section UPDATEs.
_BILL_UPSERT_SQL = (
    "INSERT INTO bills ("
    + ", ".join(_BILL_COLUMNS)
    + ") VALUES ("
    + ", ".join("?" for _ in _BILL_COLUMNS)
    + ") ON CONFLICT(bill_id) DO UPDATE SET "
    + ", ".join(f"{col} = excluded.{col}" for col in _BILL_COLUMNS if col != "bill_id")
)

# Per-child column tuples — used to generate INSERT SQL and to project
# pydantic models into row tuples.
_COSPONSOR_COLUMNS: tuple[str, ...] = (
    "bill_id",
    "bioguide_id",
    "sponsorship_date",
    "sponsorship_withdrawn_date",
    "is_original_cosponsor",
)

_ACTION_COLUMNS: tuple[str, ...] = (
    "bill_id",
    "ord",
    "action_date",
    "action_text",
    "action_code",
    "source_system",
)

_SUBJECT_COLUMNS: tuple[str, ...] = ("bill_id", "subject")

_TITLE_COLUMNS: tuple[str, ...] = (
    "bill_id",
    "ord",
    "title_type",
    "title_text",
    "chamber",
)

_SUMMARY_COLUMNS: tuple[str, ...] = (
    "bill_id",
    "version_code",
    "action_date",
    "action_desc",
    "summary_text",
)


def _insert_sql(table: str, columns: tuple[str, ...]) -> str:
    return (
        f"INSERT INTO {table} ("
        + ", ".join(columns)
        + ") VALUES ("
        + ", ".join("?" for _ in columns)
        + ")"
    )


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

_VOTE_UPSERT_SQL = (
    "INSERT INTO votes ("
    + ", ".join(_VOTE_COLUMNS)
    + ") VALUES ("
    + ", ".join("?" for _ in _VOTE_COLUMNS)
    + ") ON CONFLICT(vote_id) DO UPDATE SET "
    + ", ".join(f"{col} = excluded.{col}" for col in _VOTE_COLUMNS if col != "vote_id")
)

_VOTE_POSITION_COLUMNS: tuple[str, ...] = (
    "vote_id",
    "bioguide_id",
    "position",
    "vote_party",
    "vote_state",
)

_VOTE_POSITION_UPSERT_SQL = (
    "INSERT OR REPLACE INTO vote_positions ("
    + ", ".join(_VOTE_POSITION_COLUMNS)
    + ") VALUES ("
    + ", ".join("?" for _ in _VOTE_POSITION_COLUMNS)
    + ")"
)

_MEMBER_PARTY_UNITY_COLUMNS: tuple[str, ...] = (
    "bioguide_id",
    "congress",
    "party",
    "party_unity_votes_cast",
    "party_line_votes",
)

_COSPONSOR_INSERT_SQL = _insert_sql("bill_cosponsors", _COSPONSOR_COLUMNS)
_ACTION_INSERT_SQL = _insert_sql("bill_actions", _ACTION_COLUMNS)
_SUBJECT_INSERT_SQL = _insert_sql("bill_subjects", _SUBJECT_COLUMNS)
_TITLE_INSERT_SQL = _insert_sql("bill_titles", _TITLE_COLUMNS)
_SUMMARY_INSERT_SQL = _insert_sql("bill_summaries", _SUMMARY_COLUMNS)

# Vec-only schema. Only created when load_vec=True.
_VEC_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
    embedding float[1536]
);

-- Cascade chunk deletes into chunks_vec so the two stay in sync. Virtual
-- tables can't be the *target* of trigger creation, but they can appear
-- inside trigger bodies; this is the documented sqlite-vec pattern.
CREATE TRIGGER IF NOT EXISTS chunks_ad_vec AFTER DELETE ON chunks BEGIN
    DELETE FROM chunks_vec WHERE rowid = old.id;
END;
"""

_PROCEEDING_INSERT_SQL = (
    "INSERT OR IGNORE INTO proceedings (" + ", ".join(_PROCEEDING_COLUMNS) + ") "
    "VALUES (" + ", ".join("?" for _ in _PROCEEDING_COLUMNS) + ")"
)

_CHUNK_INSERT_SQL = (
    "INSERT OR IGNORE INTO chunks "
    "(granule_id, chunk_index, text, char_start, char_end) "
    "VALUES (?, ?, ?, ?, ?)"
)

_VEC_INSERT_SQL = "INSERT OR REPLACE INTO chunks_vec(rowid, embedding) VALUES (?, ?)"


class SqliteStorage:
    """SQLite-backed :class:`Storage` implementation."""

    def __init__(self, path: Path | str, *, load_vec: bool = True) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        self._load_vec = load_vec
        # Set when a caller-owned :meth:`transaction` context is active.
        # The per-section ``replace_bill_*`` writers consult this so they
        # don't open nested BEGIN/COMMITs inside an outer batch.
        self._in_tx = False

        if load_vec:
            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._conn.enable_load_extension(False)

        # WAL gives us concurrent reads alongside our single writer.
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_BASE_SCHEMA)
        if load_vec:
            self._conn.executescript(_VEC_SCHEMA)
        self._conn.commit()

    # -- Storage Protocol -------------------------------------------------

    def has(self, granule_id: str) -> bool:
        cursor = self._conn.execute(
            "SELECT 1 FROM proceedings WHERE granule_id = ? LIMIT 1",
            (granule_id,),
        )
        return cursor.fetchone() is not None

    def write(self, proceeding: Proceeding) -> None:
        self._conn.execute(_PROCEEDING_INSERT_SQL, _row_from_proceeding(proceeding))
        self._conn.commit()

    # -- Stage 2: chunk I/O -----------------------------------------------

    def proceedings_without_chunks(self, *, limit: int | None = None) -> Iterable[tuple[str, str]]:
        """Yield ``(granule_id, text)`` for proceedings whose chunking pass hasn't run.

        Uses the ``chunking_status`` ledger rather than a chunks-row count so
        that empty-text proceedings (which produce zero chunks) aren't
        re-yielded on every run.
        """
        sql = (
            "SELECT p.granule_id, p.text FROM proceedings p "
            "LEFT JOIN chunking_status cs ON cs.granule_id = p.granule_id "
            "WHERE cs.granule_id IS NULL"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        cursor = self._conn.execute(sql)
        for row in cursor:
            yield row["granule_id"], row["text"]

    def bulk_insert_chunks(
        self,
        granule_id: str,
        chunks: Sequence[tuple[int, str, int, int]],
        *,
        chunked_at: str,
    ) -> int:
        """Write the chunks for a single proceeding atomically.

        Each ``chunks`` entry is ``(chunk_index, text, char_start, char_end)``.
        Also records the chunking-status row so ``proceedings_without_chunks``
        won't re-yield this proceeding.
        """
        try:
            self._conn.execute("BEGIN")
            if chunks:
                self._conn.executemany(
                    _CHUNK_INSERT_SQL,
                    [(granule_id, idx, text, cs, ce) for idx, text, cs, ce in chunks],
                )
            self._conn.execute(
                "INSERT OR REPLACE INTO chunking_status "
                "(granule_id, chunked_at, chunk_count) VALUES (?, ?, ?)",
                (granule_id, chunked_at, len(chunks)),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return len(chunks)

    def chunks_for(self, granule_id: str) -> list[sqlite3.Row]:
        """Return every chunk row for a proceeding, ordered by chunk_index."""
        cursor = self._conn.execute(
            "SELECT id, granule_id, chunk_index, text, char_start, char_end "
            "FROM chunks WHERE granule_id = ? ORDER BY chunk_index",
            (granule_id,),
        )
        return cursor.fetchall()

    # -- Stage 2: embedding I/O -------------------------------------------

    def chunks_without_embeddings(self, *, limit: int | None = None) -> Iterable[tuple[int, str]]:
        """Yield ``(chunk_id, text)`` for chunks not yet embedded."""
        if not self._load_vec:
            raise RuntimeError("chunks_without_embeddings requires load_vec=True (sqlite-vec)")
        sql = (
            "SELECT c.id, c.text FROM chunks c "
            "LEFT JOIN chunks_vec v ON v.rowid = c.id "
            "WHERE v.rowid IS NULL ORDER BY c.id"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        cursor = self._conn.execute(sql)
        for row in cursor:
            yield row["id"], row["text"]

    def bulk_insert_embeddings(self, rows: Sequence[tuple[int, Sequence[float]]]) -> int:
        """Insert ``(chunk_id, embedding)`` pairs into ``chunks_vec``."""
        if not self._load_vec:
            raise RuntimeError("bulk_insert_embeddings requires load_vec=True (sqlite-vec)")
        if not rows:
            return 0
        try:
            self._conn.execute("BEGIN")
            self._conn.executemany(
                _VEC_INSERT_SQL,
                [(chunk_id, sqlite_vec.serialize_float32(vec)) for chunk_id, vec in rows],
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return len(rows)

    # -- Members (Phase 1) ------------------------------------------------

    def upsert_member(
        self,
        member: Member,
        terms: Sequence[Term],
        *,
        fetched_at: str,
    ) -> None:
        """Project a Member + its Terms into SQLite atomically.

        The Member row is UPSERTed on ``bioguide_id``; the Term rows for
        this Member are replaced (DELETE-then-INSERT) so the projection
        stays consistent with the latest snapshot, including the case
        where the upstream API drops a Term that used to be present.
        """
        member_row = _row_from_member(member, fetched_at=fetched_at)
        term_rows = [_row_from_term(t) for t in terms]
        try:
            self._conn.execute("BEGIN")
            self._conn.execute(_MEMBER_UPSERT_SQL, member_row)
            self._conn.execute(
                "DELETE FROM member_terms WHERE bioguide_id = ?",
                (member.bioguide_id,),
            )
            if term_rows:
                self._conn.executemany(_TERM_INSERT_SQL, term_rows)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def get_member(self, bioguide_id: str) -> sqlite3.Row | None:
        cursor = self._conn.execute(
            "SELECT * FROM members WHERE bioguide_id = ?",
            (bioguide_id,),
        )
        row: sqlite3.Row | None = cursor.fetchone()
        return row

    def terms_for_member(self, bioguide_id: str) -> list[sqlite3.Row]:
        cursor = self._conn.execute(
            "SELECT * FROM member_terms WHERE bioguide_id = ? ORDER BY congress ASC, chamber ASC",
            (bioguide_id,),
        )
        return cursor.fetchall()

    # -- Bills (Phase 2a) -------------------------------------------------

    def upsert_bill(self, bill: Bill, *, fetched_at: str) -> None:
        """UPSERT one Bill row keyed on ``bill_id``.

        Latest snapshot wins per ADR 0006; the loader is responsible for
        feeding only the latest snapshot per natural key.
        """
        row = _row_from_bill(bill, fetched_at=fetched_at)
        self._conn.execute(_BILL_UPSERT_SQL, row)
        self._conn.commit()

    def get_bill(self, bill_id: str) -> sqlite3.Row | None:
        cursor = self._conn.execute(
            "SELECT * FROM bills WHERE bill_id = ?",
            (bill_id,),
        )
        row: sqlite3.Row | None = cursor.fetchone()
        return row

    def bill_ids_present(self, bill_ids: Sequence[str]) -> set[str]:
        """Return the subset of ``bill_ids`` that already have a row in ``bills``.

        Used by the tier-2 loader to filter out orphan rows in one query
        instead of an N+1 ``get_bill`` loop. Empty input returns an
        empty set without touching the DB. The query is chunked at
        :data:`_BILL_IDS_PRESENT_CHUNK` placeholders to stay under
        SQLite's compile-time variable cap.
        """
        if not bill_ids:
            return set()
        present: set[str] = set()
        ids = list(bill_ids)
        for start in range(0, len(ids), _BILL_IDS_PRESENT_CHUNK):
            chunk = ids[start : start + _BILL_IDS_PRESENT_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            cursor = self._conn.execute(
                f"SELECT bill_id FROM bills WHERE bill_id IN ({placeholders})",
                chunk,
            )
            present.update(row["bill_id"] for row in cursor)
        return present

    # -- Bills tier-2 child tables (Phase 2b) -----------------------------

    def replace_bill_cosponsors(
        self,
        bill_id: str,
        cosponsors: Sequence[Cosponsor],
        *,
        fetched_at: str,
    ) -> None:
        """DELETE-then-INSERT every Cosponsor for one bill_id; stamp the fetched_at column.

        Idempotent: re-running with the same set produces the same final
        state. Safe to call for a Bill not in the ``bills`` table — the
        FK will block the INSERT before any rows are written; callers
        should filter out unknown ``bill_id`` before invoking. When the
        caller has an outer :meth:`transaction` open, the BEGIN/COMMIT
        here is skipped — the work joins the batch.
        """
        with self._maybe_transaction():
            self._conn.execute("DELETE FROM bill_cosponsors WHERE bill_id = ?", (bill_id,))
            if cosponsors:
                self._conn.executemany(
                    _COSPONSOR_INSERT_SQL,
                    [
                        (
                            bill_id,
                            c.bioguide_id,
                            c.sponsorship_date,
                            c.sponsorship_withdrawn_date,
                            1 if c.is_original_cosponsor else 0,
                        )
                        for c in cosponsors
                    ],
                )
            self._conn.execute(
                "UPDATE bills SET cosponsors_fetched_at = ? WHERE bill_id = ?",
                (fetched_at, bill_id),
            )

    def replace_bill_actions(
        self,
        bill_id: str,
        actions: Sequence[BillAction],
        *,
        fetched_at: str,
    ) -> None:
        """DELETE-then-INSERT every BillAction for one bill_id; stamp the column."""
        with self._maybe_transaction():
            self._conn.execute("DELETE FROM bill_actions WHERE bill_id = ?", (bill_id,))
            if actions:
                self._conn.executemany(
                    _ACTION_INSERT_SQL,
                    [
                        (
                            bill_id,
                            i,
                            a.action_date,
                            a.action_text,
                            a.action_code,
                            a.source_system,
                        )
                        for i, a in enumerate(actions)
                    ],
                )
            self._conn.execute(
                "UPDATE bills SET actions_fetched_at = ? WHERE bill_id = ?",
                (fetched_at, bill_id),
            )

    def replace_bill_subjects(
        self,
        bill_id: str,
        subjects: Sequence[BillSubject],
        *,
        fetched_at: str,
    ) -> None:
        """DELETE-then-INSERT every BillSubject for one bill_id; stamp the column.

        Duplicate ``name`` values in the input are dedup'd before INSERT
        so the per-row PK (``bill_id, subject``) doesn't trip.
        """
        seen: set[str] = set()
        deduped: list[BillSubject] = []
        for s in subjects:
            if s.name in seen:
                continue
            seen.add(s.name)
            deduped.append(s)
        with self._maybe_transaction():
            self._conn.execute("DELETE FROM bill_subjects WHERE bill_id = ?", (bill_id,))
            if deduped:
                self._conn.executemany(
                    _SUBJECT_INSERT_SQL,
                    [(bill_id, s.name) for s in deduped],
                )
            self._conn.execute(
                "UPDATE bills SET subjects_fetched_at = ? WHERE bill_id = ?",
                (fetched_at, bill_id),
            )

    def replace_bill_titles(
        self,
        bill_id: str,
        titles: Sequence[BillTitle],
        *,
        fetched_at: str,
    ) -> None:
        """DELETE-then-INSERT every BillTitle for one bill_id; stamp the column."""
        with self._maybe_transaction():
            self._conn.execute("DELETE FROM bill_titles WHERE bill_id = ?", (bill_id,))
            if titles:
                self._conn.executemany(
                    _TITLE_INSERT_SQL,
                    [
                        (bill_id, i, t.title_type, t.title_text, t.chamber)
                        for i, t in enumerate(titles)
                    ],
                )
            self._conn.execute(
                "UPDATE bills SET titles_fetched_at = ? WHERE bill_id = ?",
                (fetched_at, bill_id),
            )

    def replace_bill_summaries(
        self,
        bill_id: str,
        summaries: Sequence[BillSummary],
        *,
        fetched_at: str,
    ) -> None:
        """DELETE-then-INSERT every BillSummary for one bill_id; stamp the column.

        Duplicate ``version_code`` values in the input are dedup'd; the
        latest by list order wins.
        """
        latest_per_version: dict[str, BillSummary] = {}
        for s in summaries:
            latest_per_version[s.version_code] = s
        with self._maybe_transaction():
            self._conn.execute("DELETE FROM bill_summaries WHERE bill_id = ?", (bill_id,))
            if latest_per_version:
                self._conn.executemany(
                    _SUMMARY_INSERT_SQL,
                    [
                        (
                            bill_id,
                            s.version_code,
                            s.action_date,
                            s.action_desc,
                            s.summary_text,
                        )
                        for s in latest_per_version.values()
                    ],
                )
            self._conn.execute(
                "UPDATE bills SET summaries_fetched_at = ? WHERE bill_id = ?",
                (fetched_at, bill_id),
            )

    def cosponsors_for_bill(self, bill_id: str) -> list[sqlite3.Row]:
        cursor = self._conn.execute(
            "SELECT * FROM bill_cosponsors WHERE bill_id = ? "
            "ORDER BY is_original_cosponsor DESC, sponsorship_date ASC, bioguide_id ASC",
            (bill_id,),
        )
        return cursor.fetchall()

    def actions_for_bill(self, bill_id: str) -> list[sqlite3.Row]:
        # Sort newest-first by date; fall back to scrape-order ord for
        # same-date ties. Sorting in SQL (not relying on the API's
        # newest-first order) keeps the UI's reverse-chronological
        # rendering correct if the upstream order ever flips.
        cursor = self._conn.execute(
            "SELECT * FROM bill_actions WHERE bill_id = ? "
            "ORDER BY (action_date IS NULL), action_date DESC, ord ASC",
            (bill_id,),
        )
        return cursor.fetchall()

    def subjects_for_bill(self, bill_id: str) -> list[sqlite3.Row]:
        cursor = self._conn.execute(
            "SELECT * FROM bill_subjects WHERE bill_id = ? ORDER BY subject ASC",
            (bill_id,),
        )
        return cursor.fetchall()

    def titles_for_bill(self, bill_id: str) -> list[sqlite3.Row]:
        cursor = self._conn.execute(
            "SELECT * FROM bill_titles WHERE bill_id = ? ORDER BY ord ASC",
            (bill_id,),
        )
        return cursor.fetchall()

    def summaries_for_bill(self, bill_id: str) -> list[sqlite3.Row]:
        cursor = self._conn.execute(
            "SELECT * FROM bill_summaries WHERE bill_id = ? ORDER BY action_date ASC",
            (bill_id,),
        )
        return cursor.fetchall()

    # -- Votes (Phase 3a) -------------------------------------------------

    def upsert_vote(self, vote: Vote, *, fetched_at: str) -> None:
        """UPSERT one Vote row keyed on ``vote_id``.

        Latest snapshot wins per ADR 0006; the loader is responsible for
        feeding only the latest snapshot per natural key. The
        ``is_party_unity`` column is overwritten by this call — the
        indexer's UPDATE pass runs after the loader and re-establishes
        it across all rows.
        """
        row = _row_from_vote(vote, fetched_at=fetched_at)
        with self._maybe_transaction():
            self._conn.execute(_VOTE_UPSERT_SQL, row)

    def upsert_vote_positions(
        self,
        vote_id: str,
        positions: Sequence[VotePosition],
    ) -> int:
        """Bulk-replace every position row for one vote.

        DELETE-then-INSERT semantics: if the latest members snapshot for
        a vote no longer carries a Member, that Member's position is
        dropped. Returns the number of rows written.
        """
        with self._maybe_transaction():
            self._conn.execute("DELETE FROM vote_positions WHERE vote_id = ?", (vote_id,))
            if positions:
                self._conn.executemany(
                    _VOTE_POSITION_UPSERT_SQL,
                    [
                        (
                            vote_id,
                            p.bioguide_id,
                            p.position,
                            p.vote_party,
                            p.vote_state,
                        )
                        for p in positions
                    ],
                )
        return len(positions)

    def get_vote(self, vote_id: str) -> sqlite3.Row | None:
        cursor = self._conn.execute(
            "SELECT * FROM votes WHERE vote_id = ?",
            (vote_id,),
        )
        row: sqlite3.Row | None = cursor.fetchone()
        return row

    def list_vote_positions_for_vote(self, vote_id: str) -> list[sqlite3.Row]:
        cursor = self._conn.execute(
            "SELECT * FROM vote_positions WHERE vote_id = ? ORDER BY bioguide_id ASC",
            (vote_id,),
        )
        return cursor.fetchall()

    def list_recent_votes_for_member(
        self,
        bioguide_id: str,
        *,
        limit: int = 25,
    ) -> list[sqlite3.Row]:
        cursor = self._conn.execute(
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

    def get_party_unity_for_member(self, bioguide_id: str) -> list[sqlite3.Row]:
        cursor = self._conn.execute(
            "SELECT * FROM member_party_unity WHERE bioguide_id = ? ORDER BY congress DESC",
            (bioguide_id,),
        )
        return cursor.fetchall()

    def vote_history_for_bill(self, bill_id: str) -> list[sqlite3.Row]:
        """Return every Vote whose subject (bill or amendment) ties back to ``bill_id``.

        Includes amendment votes whose `bill_id` column records the
        underlying bill (the "amendment trap" the spike named — the
        API returns the underlying bill in legislationType/Number on
        amendment vote payloads).
        """
        cursor = self._conn.execute(
            "SELECT * FROM votes WHERE bill_id = ? ORDER BY start_date DESC",
            (bill_id,),
        )
        return cursor.fetchall()

    # -- introspection ----------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def connection(self) -> sqlite3.Connection:
        """Direct access to the underlying connection.

        Intended for read-only queries the orchestrator and (later) the web
        layer need that aren't worth a dedicated helper.
        """
        return self._conn

    def __len__(self) -> int:
        """Total number of distinct Proceedings stored."""
        cursor = self._conn.execute("SELECT COUNT(*) FROM proceedings")
        (count,) = cursor.fetchone()
        return int(count)

    # -- batched writes ---------------------------------------------------

    @contextmanager
    def _maybe_transaction(self) -> Iterator[None]:
        """Open BEGIN/COMMIT iff no caller-owned transaction is active.

        Internal helper for the per-section ``replace_bill_*`` writers
        so the same code path works whether invoked standalone or
        inside an outer :meth:`transaction` block.
        """
        if self._in_tx:
            yield
            return
        self._conn.execute("BEGIN")
        try:
            yield
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group many tier-2 writes into one BEGIN/COMMIT.

        Callers that flush hundreds of bills across five sections
        benefit substantially (5N small transactions collapse to one). The
        ``replace_bill_*`` methods check :attr:`_in_tx` and skip their
        own transaction wrappers when this context is active. Nested
        ``transaction()`` calls aren't supported — raises if entered
        recursively.

        Rolls back on any exception so a partial bulk load doesn't
        leave the DB half-projected.
        """
        if self._in_tx:
            raise RuntimeError("SqliteStorage.transaction() is not re-entrant")
        self._conn.execute("BEGIN")
        self._in_tx = True
        try:
            yield
        except Exception:
            self._conn.execute("ROLLBACK")
            self._in_tx = False
            raise
        else:
            self._conn.execute("COMMIT")
            self._in_tx = False

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SqliteStorage":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


# -- serialization ---------------------------------------------------------


def _row_from_proceeding(proceeding: Proceeding) -> tuple[Any, ...]:
    """Project a :class:`Proceeding` into the column tuple expected by SQL."""
    dumped: dict[str, Any] = proceeding.model_dump(mode="json")
    return tuple(dumped[col] for col in _PROCEEDING_COLUMNS)


def _row_from_member(member: Member, *, fetched_at: str) -> tuple[Any, ...]:
    """Project a :class:`Member` into the column tuple expected by SQL."""
    dumped: dict[str, Any] = member.model_dump(mode="json")
    dumped["fetched_at"] = fetched_at
    return tuple(dumped[col] for col in _MEMBER_COLUMNS)


def _row_from_term(term: Term) -> tuple[Any, ...]:
    """Project a :class:`Term` into the column tuple expected by SQL."""
    dumped: dict[str, Any] = term.model_dump(mode="json")
    return tuple(dumped[col] for col in _TERM_COLUMNS)


def _row_from_bill(bill: Bill, *, fetched_at: str) -> tuple[Any, ...]:
    """Project a :class:`Bill` into the column tuple expected by SQL."""
    dumped: dict[str, Any] = bill.model_dump(mode="json")
    dumped["fetched_at"] = fetched_at
    return tuple(dumped[col] for col in _BILL_COLUMNS)


def _row_from_vote(vote: Vote, *, fetched_at: str) -> tuple[Any, ...]:
    """Project a :class:`Vote` into the column tuple expected by SQL."""
    dumped: dict[str, Any] = vote.model_dump(mode="json")
    dumped["fetched_at"] = fetched_at
    dumped["is_party_unity"] = 1 if dumped.get("is_party_unity") else 0
    return tuple(dumped[col] for col in _VOTE_COLUMNS)
