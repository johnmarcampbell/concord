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
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any

import sqlite_vec  # type: ignore[import-untyped]

from concord.models.bills import (
    BillAction,
    BillCosponsor,
    BillDetail,
    BillSubject,
    BillSummary,
    BillTitle,
)
from concord.models.members import Member, Term
from concord.models.proceedings import Proceeding
from concord.models.runs import RunEvent, RunRecord
from concord.models.validation import ValidationFailure
from concord.models.votes import Vote, VotePosition
from concord.storage import bills as bills_storage
from concord.storage import members as members_storage
from concord.storage import runs as runs_storage
from concord.storage import validation as validation_storage
from concord.storage import votes as votes_storage

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

"""
# Each domain module owns its tables' DDL. ensure_schema stays the single
# bootstrap seam (ADR 0012) by folding every fragment into one _BASE_SCHEMA;
# the matching migration in each module replays the same DDL (ADR 0017).
_BASE_SCHEMA += "".join(
    f"\n{fragment}\n"
    for fragment in (
        members_storage.MEMBERS_SCHEMA,
        bills_storage.BILLS_SCHEMA,
        votes_storage.VOTES_SCHEMA,
        runs_storage.RUNS_SCHEMA,
        validation_storage.VALIDATION_FAILURES_SCHEMA,
    )
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


def ensure_schema(db_path: Path | str) -> None:
    """Create the SQLite file (and parent dir) and apply the full schema.

    Idempotent: every DDL statement in ``_BASE_SCHEMA`` and ``_VEC_SCHEMA``
    is ``CREATE … IF NOT EXISTS``, and post-release schema changes are
    applied via the versioned migration runner (``_migrate``) keyed on
    ``PRAGMA user_version`` — see ADR 0017. Safe to call against a fresh
    DB, an older DB, or a DB already at HEAD; in all three cases the
    file converges on the current schema with ``user_version = _HEAD``.

    Used by the web layer to bootstrap an empty store on first boot —
    see ADR 0012.
    """
    SqliteStorage(db_path).close()


#: Ordered, append-only schema migrations. Each entry is
#: ``(version, callable)``; the callable receives a connection and
#: mutates it to bring the DB from version N-1 to version N. The
#: runner (``_migrate``) wraps each call in a transaction and bumps
#: ``PRAGMA user_version`` on success. See ADR 0017.
#:
#: NEVER reorder, renumber, or edit a landed entry. The fix for a
#: buggy migration is a *new* migration with a higher version.
_MIGRATIONS: tuple[tuple[int, Callable[[sqlite3.Connection], None]], ...] = (
    (1, bills_storage.m001_add_bill_last_enrichment_error),
    (2, bills_storage.m002_add_bill_briefs),
    (3, runs_storage.m003_add_runs_tables),
    (4, validation_storage.m004_add_validation_failures),
    (5, members_storage.m005_member_terms_not_null),
    (6, bills_storage.m006_bill_children_not_null),
    (7, votes_storage.m007_vote_positions_not_null),
)
_HEAD: int = _MIGRATIONS[-1][0] if _MIGRATIONS else 0


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply every pending migration in order; bump ``user_version``.

    Reads ``PRAGMA user_version`` (defaults to ``0`` on any DB that has
    never set it, which is every DB created before ADR 0017). Raises if
    the DB reports a version higher than ``_HEAD`` — downgrade is not
    supported. Each migration runs inside its own transaction; a
    failure leaves the DB at the previous version. See ADR 0017.
    """
    current: int = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > _HEAD:
        raise RuntimeError(
            f"SQLite user_version={current} exceeds this build's _HEAD={_HEAD}. "
            "Downgrade is not supported; reinstall a newer Concord build."
        )
    for version, fn in _MIGRATIONS:
        if version <= current:
            continue
        with conn:
            fn(conn)
            conn.execute(f"PRAGMA user_version = {version}")


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
        _migrate(self._conn)
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
        members_storage.upsert_member(self._conn, member, terms, fetched_at=fetched_at)

    def get_member(self, bioguide_id: str) -> sqlite3.Row | None:
        return members_storage.get_member(self._conn, bioguide_id)

    def terms_for_member(self, bioguide_id: str) -> list[sqlite3.Row]:
        return members_storage.terms_for_member(self._conn, bioguide_id)

    # -- Bills (Phase 2a) -------------------------------------------------

    def upsert_bill(self, bill: BillDetail, *, fetched_at: str) -> None:
        """UPSERT one Bill row keyed on ``bill_id`` (latest snapshot wins, ADR 0006)."""
        bills_storage.upsert_bill(self._conn, bill, fetched_at=fetched_at)

    def get_bill(self, bill_id: str) -> sqlite3.Row | None:
        return bills_storage.get_bill(self._conn, bill_id)

    def bill_ids_present(self, bill_ids: Sequence[str]) -> set[str]:
        """Return the subset of ``bill_ids`` that already have a row in ``bills``."""
        return bills_storage.bill_ids_present(self._conn, bill_ids)

    # -- Bills tier-2 child tables (Phase 2b) -----------------------------
    #
    # Each ``replace_bill_*`` runs DELETE-then-INSERT then stamps the section's
    # ``*_fetched_at`` column, wrapped in :meth:`_maybe_transaction` so the work
    # joins an outer :meth:`transaction` batch when one is open.

    def replace_bill_cosponsors(
        self,
        bill_id: str,
        cosponsors: Sequence[BillCosponsor],
        *,
        fetched_at: str,
    ) -> None:
        """DELETE-then-INSERT every Cosponsor for one bill_id; stamp the fetched_at column."""
        with self._maybe_transaction():
            bills_storage.replace_cosponsors(self._conn, bill_id, cosponsors, fetched_at=fetched_at)

    def replace_bill_actions(
        self,
        bill_id: str,
        actions: Sequence[BillAction],
        *,
        fetched_at: str,
    ) -> None:
        """DELETE-then-INSERT every BillAction for one bill_id; stamp the column."""
        with self._maybe_transaction():
            bills_storage.replace_actions(self._conn, bill_id, actions, fetched_at=fetched_at)

    def replace_bill_subjects(
        self,
        bill_id: str,
        subjects: Sequence[BillSubject],
        *,
        fetched_at: str,
    ) -> None:
        """DELETE-then-INSERT every BillSubject for one bill_id; stamp the column."""
        with self._maybe_transaction():
            bills_storage.replace_subjects(self._conn, bill_id, subjects, fetched_at=fetched_at)

    def replace_bill_titles(
        self,
        bill_id: str,
        titles: Sequence[BillTitle],
        *,
        fetched_at: str,
    ) -> None:
        """DELETE-then-INSERT every BillTitle for one bill_id; stamp the column."""
        with self._maybe_transaction():
            bills_storage.replace_titles(self._conn, bill_id, titles, fetched_at=fetched_at)

    def replace_bill_summaries(
        self,
        bill_id: str,
        summaries: Sequence[BillSummary],
        *,
        fetched_at: str,
    ) -> None:
        """DELETE-then-INSERT every BillSummary for one bill_id; stamp the column."""
        with self._maybe_transaction():
            bills_storage.replace_summaries(self._conn, bill_id, summaries, fetched_at=fetched_at)

    def set_bill_enrichment_error(self, bill_id: str, error: str) -> None:
        """Record an enrichment-attempt error on the bills row."""
        with self._maybe_transaction():
            bills_storage.set_enrichment_error(self._conn, bill_id, error)

    def clear_bill_enrichment_error(self, bill_id: str) -> None:
        """Clear any previously-recorded enrichment-attempt error."""
        with self._maybe_transaction():
            bills_storage.clear_enrichment_error(self._conn, bill_id)

    def cosponsors_for_bill(self, bill_id: str) -> list[sqlite3.Row]:
        return bills_storage.cosponsors_for_bill(self._conn, bill_id)

    def actions_for_bill(self, bill_id: str) -> list[sqlite3.Row]:
        return bills_storage.actions_for_bill(self._conn, bill_id)

    def subjects_for_bill(self, bill_id: str) -> list[sqlite3.Row]:
        return bills_storage.subjects_for_bill(self._conn, bill_id)

    def titles_for_bill(self, bill_id: str) -> list[sqlite3.Row]:
        return bills_storage.titles_for_bill(self._conn, bill_id)

    def summaries_for_bill(self, bill_id: str) -> list[sqlite3.Row]:
        return bills_storage.summaries_for_bill(self._conn, bill_id)

    # -- Bill Briefs (record table — ADR 0019 / 0020) ---------------------

    def upsert_bill_brief(
        self,
        *,
        bill_id: str,
        lens: str,
        executive_summary: str,
        facts_hash: str,
        model: str,
        prompt_version: int,
        generated_at: str,
    ) -> None:
        """Insert or replace the cached brief for ``(bill_id, lens)`` (ADR 0019/0020)."""
        with self._maybe_transaction():
            bills_storage.upsert_bill_brief(
                self._conn,
                bill_id=bill_id,
                lens=lens,
                executive_summary=executive_summary,
                facts_hash=facts_hash,
                model=model,
                prompt_version=prompt_version,
                generated_at=generated_at,
            )

    # -- Scrape Run ledger (ADR 0021) -------------------------------------

    def insert_run(self, run: RunRecord) -> None:
        """INSERT one ``runs`` ledger row from a :class:`RunRecord` (ADR 0021).

        A record-table write (ADR 0019): re-running scrape/load/index never
        rewrites it. Column projection + JSON serialization live in
        :mod:`concord.storage.runs`. Does not write ``run.events`` — those
        fan out via :meth:`insert_run_events`.
        """
        with self._maybe_transaction():
            runs_storage.insert_run(self._conn, run)

    def insert_run_events(self, run_id: str, events: Sequence[RunEvent]) -> None:
        """Bulk-INSERT the :class:`RunEvent` rows for one Scrape Run (ADR 0021).

        ``seq`` is assigned from the list order. A no-op for a clean run with
        zero error events. The parent ``runs`` row must already exist (FKs are
        on); :func:`concord.observability.scrape_run` inserts it first in the
        same transaction.
        """
        with self._maybe_transaction():
            runs_storage.insert_run_events(self._conn, run_id, events)

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        """Return the ``runs`` row for ``run_id``, or ``None`` if absent."""
        return runs_storage.get_run(self._conn, run_id)

    def list_run_events(self, run_id: str) -> list[sqlite3.Row]:
        """Return every ``run_events`` row for ``run_id``, ordered by ``seq``."""
        return runs_storage.list_run_events(self._conn, run_id)

    # -- Load Validation Failures (mirror table — ADR 0023) ---------------

    def replace_validation_failures(
        self,
        failures: Sequence[ValidationFailure],
        *,
        entities: Sequence[str],
        entity_key: str | None = None,
    ) -> None:
        """Replace-on-load the validation_failures rows for a load scope (ADR 0023).

        A mirror-table write (ADR 0019): DELETE the ``entities`` family (narrowed
        to ``entity_key`` for the ``load_one`` path), then INSERT the current
        ``failures``. Called by each converging load — even with an empty list, so
        a now-clean load clears stale rows. A ``--limit`` load skips it (it only
        processed a subset, so a family-wide replace would drop real rows).
        """
        with self._maybe_transaction():
            validation_storage.replace_validation_failures(
                self._conn, failures, entities=entities, entity_key=entity_key
            )

    # -- Votes (Phase 3a) -------------------------------------------------

    def upsert_vote(self, vote: Vote, *, fetched_at: str) -> None:
        """UPSERT one Vote row keyed on ``vote_id`` (latest snapshot wins, ADR 0006)."""
        with self._maybe_transaction():
            votes_storage.upsert_vote(self._conn, vote, fetched_at=fetched_at)

    def upsert_vote_positions(
        self,
        vote_id: str,
        positions: Sequence[VotePosition],
    ) -> int:
        """Bulk-replace every position row for one vote; returns the number written."""
        with self._maybe_transaction():
            votes_storage.replace_vote_positions(self._conn, vote_id, positions)
        return len(positions)

    def get_vote(self, vote_id: str) -> sqlite3.Row | None:
        return votes_storage.get_vote(self._conn, vote_id)

    def list_vote_positions_for_vote(self, vote_id: str) -> list[sqlite3.Row]:
        return votes_storage.list_vote_positions_for_vote(self._conn, vote_id)

    def list_recent_votes_for_member(
        self,
        bioguide_id: str,
        *,
        limit: int = 25,
    ) -> list[sqlite3.Row]:
        return votes_storage.list_recent_votes_for_member(self._conn, bioguide_id, limit=limit)

    def get_party_unity_for_member(self, bioguide_id: str) -> list[sqlite3.Row]:
        return votes_storage.get_party_unity_for_member(self._conn, bioguide_id)

    def vote_history_for_bill(self, bill_id: str) -> list[sqlite3.Row]:
        return votes_storage.vote_history_for_bill(self._conn, bill_id)

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
