"""Tests for the SQLite storage backend."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from concord.models.proceedings import Article, Issue, Proceeding
from concord.storage.base import Storage
from concord.storage.sqlite import _BASE_SCHEMA, _HEAD, _MIGRATIONS, SqliteStorage, ensure_schema

DEFAULT_GRANULE = "CREC-2026-05-22-pt1-PgD551-6"


def _sample_proceeding(*, granule_id: str = DEFAULT_GRANULE, text: str = "body") -> Proceeding:
    """Build a Proceeding whose URLs are derived from the granule ID.

    The Article model verifies that the granule_id matches the granule
    embedded in both text_url and pdf_url, so the URLs are constructed
    from the same granule ID.
    """
    text_url = f"https://www.congress.gov/119/crec/2026/05/22/172/88/modified/{granule_id}.htm"
    pdf_url = f"https://www.congress.gov/119/crec/2026/05/22/172/88/{granule_id}.pdf"
    issue = Issue(
        issue_date="2026-05-22",
        congress=119,
        session=2,
        volume=172,
        issue_number=88,
        update_date="2026-05-23T06:44:22Z",
    )
    article = Article(
        section="Daily Digest",
        title="Sample",
        start_page="D551",
        end_page="D552",
        text_url=text_url,
        pdf_url=pdf_url,
        granule_id=granule_id,
    )
    return Proceeding.build(
        issue=issue,
        article=article,
        text=text,
        fetched_at=datetime(2026, 5, 24, tzinfo=UTC),
    )


# -- protocol conformance ------------------------------------------------------


class TestProtocol:
    def test_sqlite_storage_satisfies_storage_protocol(self, tmp_path: Path) -> None:
        storage: Storage = SqliteStorage(tmp_path / "out.db")
        assert hasattr(storage, "has")
        assert hasattr(storage, "write")


# -- schema --------------------------------------------------------------------


class TestSchema:
    def test_proceedings_table_exists(self, tmp_path: Path) -> None:
        path = tmp_path / "out.db"
        SqliteStorage(path).close()
        conn = sqlite3.connect(path)
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='proceedings'"
            )
            assert cursor.fetchone() is not None
        finally:
            conn.close()

    def test_indexes_exist(self, tmp_path: Path) -> None:
        path = tmp_path / "out.db"
        SqliteStorage(path).close()
        conn = sqlite3.connect(path)
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='proceedings'"
            )
            names = {row[0] for row in cursor.fetchall()}
            assert "idx_proceedings_issue_date" in names
            assert "idx_proceedings_congress" in names
        finally:
            conn.close()

    def test_wal_mode_enabled(self, tmp_path: Path) -> None:
        path = tmp_path / "out.db"
        SqliteStorage(path).close()
        conn = sqlite3.connect(path)
        try:
            (mode,) = conn.execute("PRAGMA journal_mode").fetchone()
            assert mode.lower() == "wal"
        finally:
            conn.close()

    def test_columns_match_proceeding_fields(self, tmp_path: Path) -> None:
        """Every Proceeding field has a corresponding column in the table."""
        path = tmp_path / "out.db"
        SqliteStorage(path).close()
        conn = sqlite3.connect(path)
        try:
            cursor = conn.execute("PRAGMA table_info(proceedings)")
            cols = {row[1] for row in cursor.fetchall()}
        finally:
            conn.close()
        expected = set(Proceeding.model_fields.keys())
        assert expected.issubset(cols), f"missing columns: {expected - cols}"


# -- basic write / has ---------------------------------------------------------


class TestWriteAndHas:
    def test_has_returns_false_for_unseen(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "out.db")
        assert storage.has("CREC-never-seen") is False

    def test_has_returns_true_after_write(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "out.db")
        p = _sample_proceeding()
        storage.write(p)
        assert storage.has(p.granule_id) is True

    def test_multiple_writes_each_persist(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "out.db")
        storage.write(_sample_proceeding(granule_id="CREC-2026-05-22-pt1-PgD551-1"))
        storage.write(_sample_proceeding(granule_id="CREC-2026-05-22-pt1-PgD551-2"))
        assert len(storage) == 2

    def test_len_starts_at_zero(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "out.db")
        assert len(storage) == 0


# -- dedup --------------------------------------------------------------------


class TestDedup:
    def test_writing_same_granule_twice_is_noop(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "out.db")
        p = _sample_proceeding()
        storage.write(p)
        storage.write(p)  # idempotent via INSERT OR IGNORE
        assert len(storage) == 1

    def test_dedup_persists_across_instances(self, tmp_path: Path) -> None:
        """Re-opening the same file sees the previously-written rows.

        This is the resume contract Stage 2 and the web layer depend on.
        """
        path = tmp_path / "out.db"
        first = SqliteStorage(path)
        first.write(_sample_proceeding(granule_id="CREC-2026-05-22-pt1-PgD551-1"))
        first.write(_sample_proceeding(granule_id="CREC-2026-05-22-pt1-PgD551-2"))
        first.close()

        second = SqliteStorage(path)
        try:
            assert second.has("CREC-2026-05-22-pt1-PgD551-1")
            assert second.has("CREC-2026-05-22-pt1-PgD551-2")
            assert not second.has("CREC-2026-05-22-pt1-PgD551-3")
            # Writing an already-stored granule from the second instance is a no-op.
            second.write(_sample_proceeding(granule_id="CREC-2026-05-22-pt1-PgD551-1"))
            assert len(second) == 2
        finally:
            second.close()


# -- round-trip integrity ------------------------------------------------------


class TestRoundTrip:
    def test_written_row_can_be_parsed_back_as_proceeding(self, tmp_path: Path) -> None:
        path = tmp_path / "out.db"
        original = _sample_proceeding(text="some content body")
        with SqliteStorage(path) as storage:
            storage.write(original)

        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM proceedings WHERE granule_id = ?",
                (original.granule_id,),
            ).fetchone()
            assert row is not None
            roundtripped = Proceeding.model_validate(dict(row))
        finally:
            conn.close()
        assert roundtripped == original


# -- path handling -------------------------------------------------------------


class TestPath:
    def test_accepts_string_path(self, tmp_path: Path) -> None:
        storage = SqliteStorage(str(tmp_path / "out.db"))
        storage.write(_sample_proceeding())
        assert len(storage) == 1
        storage.close()

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "c" / "out.db"
        storage = SqliteStorage(nested)
        storage.write(_sample_proceeding())
        assert nested.exists()
        storage.close()

    def test_path_property_returns_path(self, tmp_path: Path) -> None:
        path = tmp_path / "out.db"
        storage = SqliteStorage(path)
        assert storage.path == path
        storage.close()


# -- lifecycle -----------------------------------------------------------------


class TestLifecycle:
    def test_context_manager_closes_connection(self, tmp_path: Path) -> None:
        path = tmp_path / "out.db"
        with SqliteStorage(path) as storage:
            storage.write(_sample_proceeding())
        # Closed: operations on the underlying connection should fail.
        try:
            storage._conn.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            pass
        else:
            raise AssertionError("expected closed connection to refuse queries")


# ---------------------------------------------------------------------------
# Stage 2 — schema additions
# ---------------------------------------------------------------------------


class TestStage2Schema:
    def test_chunks_table_exists(self, tmp_path: Path) -> None:
        SqliteStorage(tmp_path / "out.db").close()
        conn = sqlite3.connect(tmp_path / "out.db")
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='chunks'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_chunks_fts_virtual_table_exists(self, tmp_path: Path) -> None:
        SqliteStorage(tmp_path / "out.db").close()
        conn = sqlite3.connect(tmp_path / "out.db")
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_chunks_vec_virtual_table_exists_when_load_vec_true(self, tmp_path: Path) -> None:
        SqliteStorage(tmp_path / "out.db", load_vec=True).close()
        # Re-open with load_vec=True so the extension is available for inspection.
        with SqliteStorage(tmp_path / "out.db", load_vec=True) as storage:
            row = storage.connection.execute(
                "SELECT name FROM sqlite_master WHERE name='chunks_vec'"
            ).fetchone()
            assert row is not None

    def test_load_vec_false_skips_vec_table(self, tmp_path: Path) -> None:
        SqliteStorage(tmp_path / "out.db", load_vec=False).close()
        conn = sqlite3.connect(tmp_path / "out.db")
        try:
            row = conn.execute("SELECT name FROM sqlite_master WHERE name='chunks_vec'").fetchone()
            assert row is None
        finally:
            conn.close()

    def test_chunking_status_table_exists(self, tmp_path: Path) -> None:
        SqliteStorage(tmp_path / "out.db").close()
        conn = sqlite3.connect(tmp_path / "out.db")
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='chunking_status'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_fts_trigger_fires_on_chunk_insert(self, tmp_path: Path) -> None:
        with SqliteStorage(tmp_path / "out.db") as storage:
            storage.write(_sample_proceeding())
            storage.bulk_insert_chunks(
                DEFAULT_GRANULE,
                [(0, "Senator Warren spoke about banking regulation today.", 0, 50)],
                chunked_at="2026-05-25T00:00:00Z",
            )
            rows = storage.connection.execute(
                "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'warren'"
            ).fetchall()
            assert len(rows) == 1

    def test_fts_trigger_fires_on_chunk_delete(self, tmp_path: Path) -> None:
        with SqliteStorage(tmp_path / "out.db") as storage:
            storage.write(_sample_proceeding())
            storage.bulk_insert_chunks(
                DEFAULT_GRANULE,
                [(0, "Unique sentinel word: zzzpangram.", 0, 30)],
                chunked_at="2026-05-25T00:00:00Z",
            )
            # Deleting the chunk row removes it from FTS too.
            storage.connection.execute(
                "DELETE FROM chunks WHERE granule_id = ?", (DEFAULT_GRANULE,)
            )
            storage.connection.commit()
            rows = storage.connection.execute(
                "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'zzzpangram'"
            ).fetchall()
            assert rows == []

    def test_vec_trigger_cascades_chunk_delete(self, tmp_path: Path) -> None:
        with SqliteStorage(tmp_path / "out.db") as storage:
            storage.write(_sample_proceeding())
            storage.bulk_insert_chunks(
                DEFAULT_GRANULE,
                [(0, "any text", 0, 8)],
                chunked_at="2026-05-25T00:00:00Z",
            )
            chunk_id = storage.connection.execute(
                "SELECT id FROM chunks WHERE granule_id = ?", (DEFAULT_GRANULE,)
            ).fetchone()[0]
            storage.bulk_insert_embeddings([(chunk_id, [0.5] * 1536)])
            assert storage.connection.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0] == 1

            storage.connection.execute("DELETE FROM chunks WHERE id = ?", (chunk_id,))
            storage.connection.commit()
            assert storage.connection.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Stage 2 — chunk helpers
# ---------------------------------------------------------------------------


class TestStage2ChunkHelpers:
    def test_proceedings_without_chunks_yields_unchunked(self, tmp_path: Path) -> None:
        with SqliteStorage(tmp_path / "out.db") as storage:
            storage.write(_sample_proceeding(granule_id="CREC-2026-05-22-pt1-PgD551-1"))
            storage.write(_sample_proceeding(granule_id="CREC-2026-05-22-pt1-PgD551-2"))
            storage.bulk_insert_chunks(
                "CREC-2026-05-22-pt1-PgD551-1",
                [(0, "already chunked", 0, 15)],
                chunked_at="2026-05-25T00:00:00Z",
            )
            remaining = list(storage.proceedings_without_chunks())
            assert [gid for gid, _ in remaining] == ["CREC-2026-05-22-pt1-PgD551-2"]

    def test_proceedings_without_chunks_excludes_empty_text_after_status(
        self, tmp_path: Path
    ) -> None:
        """A proceeding with zero chunks (empty text) is marked done via chunking_status."""
        with SqliteStorage(tmp_path / "out.db") as storage:
            storage.write(_sample_proceeding(text=""))
            # Insert chunking_status with zero chunks — simulating the
            # orchestrator processing an empty-text proceeding.
            storage.bulk_insert_chunks(DEFAULT_GRANULE, [], chunked_at="2026-05-25T00:00:00Z")
            assert list(storage.proceedings_without_chunks()) == []

    def test_bulk_insert_chunks_round_trip(self, tmp_path: Path) -> None:
        with SqliteStorage(tmp_path / "out.db") as storage:
            storage.write(_sample_proceeding())
            chunks = [
                (0, "first chunk", 0, 11),
                (1, "second chunk", 11, 23),
            ]
            inserted = storage.bulk_insert_chunks(
                DEFAULT_GRANULE, chunks, chunked_at="2026-05-25T00:00:00Z"
            )
            assert inserted == 2
            rows = storage.chunks_for(DEFAULT_GRANULE)
            assert len(rows) == 2
            assert rows[0]["text"] == "first chunk"
            assert rows[1]["chunk_index"] == 1

    def test_proceedings_without_chunks_respects_limit(self, tmp_path: Path) -> None:
        with SqliteStorage(tmp_path / "out.db") as storage:
            for i in range(5):
                storage.write(_sample_proceeding(granule_id=f"CREC-2026-05-22-pt1-PgD551-{i}"))
            assert len(list(storage.proceedings_without_chunks(limit=2))) == 2


# ---------------------------------------------------------------------------
# Stage 2 — embedding helpers
# ---------------------------------------------------------------------------


class TestStage2EmbeddingHelpers:
    def test_chunks_without_embeddings_yields_unembedded(self, tmp_path: Path) -> None:
        with SqliteStorage(tmp_path / "out.db") as storage:
            storage.write(_sample_proceeding())
            storage.bulk_insert_chunks(
                DEFAULT_GRANULE,
                [(0, "first", 0, 5), (1, "second", 6, 12)],
                chunked_at="2026-05-25T00:00:00Z",
            )
            ids = [
                row["id"] for row in storage.connection.execute("SELECT id FROM chunks ORDER BY id")
            ]
            # Embed only the first chunk.
            storage.bulk_insert_embeddings([(ids[0], [0.0] * 1536)])
            remaining = list(storage.chunks_without_embeddings())
            assert [cid for cid, _ in remaining] == [ids[1]]

    def test_bulk_insert_embeddings_returns_count(self, tmp_path: Path) -> None:
        with SqliteStorage(tmp_path / "out.db") as storage:
            storage.write(_sample_proceeding())
            storage.bulk_insert_chunks(
                DEFAULT_GRANULE,
                [(0, "x", 0, 1)],
                chunked_at="2026-05-25T00:00:00Z",
            )
            chunk_id = storage.connection.execute("SELECT id FROM chunks").fetchone()[0]
            assert storage.bulk_insert_embeddings([(chunk_id, [1.0] * 1536)]) == 1

    def test_bulk_insert_embeddings_empty_list_noop(self, tmp_path: Path) -> None:
        with SqliteStorage(tmp_path / "out.db") as storage:
            assert storage.bulk_insert_embeddings([]) == 0

    def test_embedding_helpers_require_load_vec(self, tmp_path: Path) -> None:
        with SqliteStorage(tmp_path / "out.db", load_vec=False) as storage:
            with pytest.raises(RuntimeError, match="load_vec=True"):
                list(storage.chunks_without_embeddings())
            with pytest.raises(RuntimeError, match="load_vec=True"):
                storage.bulk_insert_embeddings([(1, [0.0] * 1536)])


def _schema_fingerprint(conn: sqlite3.Connection) -> tuple[object, ...]:
    """Normalized structural snapshot of every user table in the DB.

    Skips ``sqlite_*`` internals, virtual tables (whose internal shape
    is not user-controlled), and FTS5 shadow tables (``sql IS NULL``).
    Column ``cid`` is dropped — column declaration order is not part of
    semantic schema identity (option A in ADR 0017 produces
    ``last_enrichment_error`` in different positions on fresh vs.
    migrated DBs, intentionally).
    """
    tables: list[object] = []
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "AND sql IS NOT NULL AND sql NOT LIKE 'CREATE VIRTUAL%' "
        "ORDER BY name"
    ):
        cols = tuple(
            sorted(
                # (name, type, notnull, dflt_value, pk) — drop cid
                (row[1], row[2], row[3], row[4], row[5])
                for row in conn.execute(f"PRAGMA table_info({name})")
            )
        )
        indexes: list[object] = []
        for idx_row in conn.execute(f"PRAGMA index_list({name})"):
            idx_name = idx_row[1]
            idx_cols = tuple(
                sorted(tuple(r) for r in conn.execute(f"PRAGMA index_info({idx_name})"))
            )
            # (name, unique, origin, partial, cols) — drop seq (idx_row[0])
            indexes.append((idx_row[1], idx_row[2], idx_row[3], idx_row[4], idx_cols))
        tables.append((name, cols, tuple(sorted(indexes, key=repr))))
    return tuple(tables)


class TestSchemaMigrations:
    """Versioned migrations via ``PRAGMA user_version`` — see ADR 0017."""

    def test_fresh_db_stamps_to_head(self, tmp_path: Path) -> None:
        """A brand-new DB is stamped to ``_HEAD`` on first bootstrap."""
        db_path = tmp_path / "fresh.db"
        ensure_schema(db_path)

        conn = sqlite3.connect(db_path)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert version == _HEAD

    def test_pre_migration_db_gets_column_and_bumps_to_head(self, tmp_path: Path) -> None:
        """A pre-ADR-0016 DB (no ``last_enrichment_error``, version 0)
        picks up the column and is stamped to ``_HEAD`` on next boot.

        Guards the regression where ``CREATE TABLE IF NOT EXISTS``
        silently no-ops on a pre-existing table and the new column
        never gets added.
        """
        db_path = tmp_path / "stale.db"
        # Build a "stale" bills table that predates last_enrichment_error.
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE bills (
                bill_id              TEXT PRIMARY KEY,
                congress             INTEGER NOT NULL,
                bill_type            TEXT NOT NULL,
                bill_number          INTEGER NOT NULL,
                origin_chamber       TEXT NOT NULL,
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
                summaries_fetched_at  TEXT
            )
            """
        )
        conn.commit()
        cols_before = {row[1] for row in conn.execute("PRAGMA table_info(bills)")}
        version_before = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert "last_enrichment_error" not in cols_before
        assert version_before == 0

        ensure_schema(db_path)

        conn = sqlite3.connect(db_path)
        cols_after = {row[1] for row in conn.execute("PRAGMA table_info(bills)")}
        version_after = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert "last_enrichment_error" in cols_after
        assert version_after == _HEAD

    def test_already_migrated_db_skips_alter(self, tmp_path: Path) -> None:
        """A 0.2.x DB at ``user_version = 0`` that already had
        ``last_enrichment_error`` added by the pre-ADR-0017
        ``_POST_RELEASE_COLUMNS`` code path must converge on
        ``user_version = _HEAD`` without raising "duplicate column".
        """
        db_path = tmp_path / "post-0.2.x.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE bills (
                bill_id              TEXT PRIMARY KEY,
                congress             INTEGER NOT NULL,
                bill_type            TEXT NOT NULL,
                bill_number          INTEGER NOT NULL,
                origin_chamber       TEXT NOT NULL,
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
                last_enrichment_error TEXT
            )
            """
        )
        conn.commit()
        # user_version stays at 0 — pre-0017 _POST_RELEASE_COLUMNS never set it.
        version_before = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert version_before == 0

        ensure_schema(db_path)

        conn = sqlite3.connect(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(bills)")}
        version_after = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert "last_enrichment_error" in cols
        assert version_after == _HEAD

    def test_repeated_ensure_schema_is_no_op(self, tmp_path: Path) -> None:
        """A DB already at ``_HEAD`` is unchanged on subsequent boots."""
        db_path = tmp_path / "fresh.db"
        ensure_schema(db_path)
        # Running again must not raise (no "duplicate column name") and
        # must keep user_version pinned.
        ensure_schema(db_path)
        conn = sqlite3.connect(db_path)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert version == _HEAD

    def test_db_above_head_raises(self, tmp_path: Path) -> None:
        """A DB whose ``user_version`` exceeds this build's ``_HEAD``
        raises rather than silently opening — downgrade is unsupported.
        """
        db_path = tmp_path / "future.db"
        ensure_schema(db_path)
        # Forge a "future" DB by bumping user_version past _HEAD.
        conn = sqlite3.connect(db_path)
        conn.execute(f"PRAGMA user_version = {_HEAD + 1}")
        conn.commit()
        conn.close()

        with pytest.raises(RuntimeError, match="exceeds"):
            ensure_schema(db_path)

    def test_base_schema_matches_replayed_migrations(self, tmp_path: Path) -> None:
        """The option-A contract from ADR 0017.

        ``_BASE_SCHEMA`` is the head snapshot, which means replaying
        every migration on top of a fresh ``_BASE_SCHEMA`` DB must be a
        structural no-op. If this test fails, the migration that was
        just added did *not* update ``_BASE_SCHEMA`` to match — and
        fresh installs would diverge from migrated installs.
        """
        db_path = tmp_path / "base.db"
        conn = sqlite3.connect(db_path)
        conn.executescript(_BASE_SCHEMA)
        before = _schema_fingerprint(conn)

        for _version, fn in _MIGRATIONS:
            with conn:
                fn(conn)
        after = _schema_fingerprint(conn)
        conn.close()

        assert before == after, (
            "_BASE_SCHEMA and the migration list disagree. Either:\n"
            "  - a migration added a column / index / table that "
            "_BASE_SCHEMA doesn't already have, OR\n"
            "  - _BASE_SCHEMA has something the migrations don't.\n"
            "Update _BASE_SCHEMA (or the migration) so they describe the same head."
        )
