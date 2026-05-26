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
from collections.abc import Iterable, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any

import sqlite_vec  # type: ignore[import-untyped]

from ..models import Bill, Member, Proceeding, Term

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

CREATE VIRTUAL TABLE IF NOT EXISTS bills_fts USING fts5(
    bill_id UNINDEXED,
    identifier,
    title,
    policy_area,
    tokenize = 'porter'
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

_BILL_UPSERT_SQL = (
    "INSERT INTO bills ("
    + ", ".join(_BILL_COLUMNS)
    + ") VALUES ("
    + ", ".join("?" for _ in _BILL_COLUMNS)
    + ") ON CONFLICT(bill_id) DO UPDATE SET "
    + ", ".join(f"{col} = excluded.{col}" for col in _BILL_COLUMNS if col != "bill_id")
)

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
