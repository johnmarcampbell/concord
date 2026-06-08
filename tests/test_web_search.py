"""Tests for the hybrid search query layer."""

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlite_vec  # type: ignore[import-untyped]

from concord.embedding import EMBEDDING_DIM, Embedder
from concord.models.proceedings import Article, Issue, Proceeding
from concord.storage.sqlite import SqliteStorage
from concord.web.search import get_proceeding, search
from concord.web.snippets import keyword_snippet, semantic_snippet

# -- fixtures -----------------------------------------------------------------


def _make_proceeding(
    granule_id: str,
    *,
    text: str = "default body text",
    issue_date: str = "2026-05-22",
    section: str = "Senate Section",
) -> Proceeding:
    text_url = f"https://www.congress.gov/119/crec/2026/05/22/172/88/modified/{granule_id}.htm"
    pdf_url = f"https://www.congress.gov/119/crec/2026/05/22/172/88/{granule_id}.pdf"
    issue = Issue(
        issue_date=issue_date,
        congress=119,
        session=2,
        volume=172,
        issue_number=88,
        update_date="2026-05-23T06:44:22Z",
    )
    article = Article(
        section=section,
        title=f"Sample {granule_id}",
        start_page="S1",
        end_page="S2",
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


def _add_chunk(storage: SqliteStorage, granule_id: str, text: str, vec: list[float]) -> int:
    """Insert one chunk + its embedding; return the chunk_id."""
    storage.bulk_insert_chunks(
        granule_id,
        [(0, text, 0, len(text))],
        chunked_at="2026-05-25T00:00:00Z",
    )
    row = storage.connection.execute(
        "SELECT id FROM chunks WHERE granule_id = ? ORDER BY id DESC LIMIT 1",
        (granule_id,),
    ).fetchone()
    chunk_id = int(row[0])
    storage.bulk_insert_embeddings([(chunk_id, vec)])
    return chunk_id


def _add_extra_chunk(
    storage: SqliteStorage, granule_id: str, chunk_index: int, text: str, vec: list[float]
) -> int:
    """Insert one more chunk into an already-chunked proceeding."""
    storage.connection.execute(
        "INSERT INTO chunks (granule_id, chunk_index, text, char_start, char_end) "
        "VALUES (?, ?, ?, ?, ?)",
        (granule_id, chunk_index, text, 0, len(text)),
    )
    storage.connection.commit()
    row = storage.connection.execute(
        "SELECT id FROM chunks WHERE granule_id = ? AND chunk_index = ?",
        (granule_id, chunk_index),
    ).fetchone()
    chunk_id = int(row[0])
    storage.bulk_insert_embeddings([(chunk_id, vec)])
    return chunk_id


@pytest.fixture
def seeded_db(tmp_path: Path) -> SqliteStorage:
    """A small DB with three proceedings, each with one chunk + embedding."""
    storage = SqliteStorage(tmp_path / "out.db")
    # Proceeding 1: mentions banking, "high" embedding similarity to [0.9...]
    storage.write(_make_proceeding("CREC-2026-05-22-pt1-PgS001-1", text="Senator on banking"))
    # Proceeding 2: mentions climate, "high" embedding similarity to [0.1...]
    storage.write(_make_proceeding("CREC-2026-05-22-pt1-PgS001-2", text="Senator on climate"))
    # Proceeding 3: mentions neither (semantic-only signal)
    storage.write(_make_proceeding("CREC-2026-05-22-pt1-PgS001-3", text="Procedural notes"))
    _add_chunk(
        storage,
        "CREC-2026-05-22-pt1-PgS001-1",
        "Senator on banking regulation",
        [0.9] * EMBEDDING_DIM,
    )
    _add_chunk(
        storage,
        "CREC-2026-05-22-pt1-PgS001-2",
        "Senator on climate policy",
        [0.1] * EMBEDDING_DIM,
    )
    _add_chunk(
        storage,
        "CREC-2026-05-22-pt1-PgS001-3",
        "Procedural notes only",
        [0.5] * EMBEDDING_DIM,
    )
    return storage


# -- stub Embedder -----------------------------------------------------------


class _StubData:
    def __init__(self, vec: list[float]) -> None:
        self.embedding = vec


class _StubResponse:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.data = [_StubData(v) for v in vectors]


class _StubEmbeddings:
    """Returns whatever vector you've staged via ``set_vector()``."""

    def __init__(self) -> None:
        self.vector: list[float] = [0.0] * EMBEDDING_DIM

    def set_vector(self, vec: list[float]) -> None:
        self.vector = vec

    def create(self, *, model: str, input: list[str]) -> _StubResponse:
        return _StubResponse([self.vector for _ in input])


class _StubClient:
    def __init__(self) -> None:
        self.embeddings = _StubEmbeddings()


def _embedder(vector: list[float] | None = None) -> Embedder:
    client = _StubClient()
    if vector is not None:
        client.embeddings.set_vector(vector)
    return Embedder(client)


# -- happy paths --------------------------------------------------------------


class TestSearch:
    def test_empty_query_returns_empty_results(self, seeded_db: SqliteStorage) -> None:
        result = search(seeded_db.connection, _embedder(), query="")
        assert result.total == 0
        assert result.results == []

    def test_whitespace_query_returns_empty_results(self, seeded_db: SqliteStorage) -> None:
        result = search(seeded_db.connection, _embedder(), query="   ")
        assert result.total == 0

    def test_pure_keyword_match(self, seeded_db: SqliteStorage) -> None:
        # Query that lexically matches proceeding 1's chunk but with a stub
        # vector that points nowhere meaningful.
        result = search(
            seeded_db.connection,
            _embedder(vector=[0.0] * EMBEDDING_DIM),
            query="banking",
        )
        assert result.total >= 1
        # Proceeding 1 should be ranked first since FTS gave it a hit and
        # vec gave nothing close.
        assert result.results[0].granule_id == "CREC-2026-05-22-pt1-PgS001-1"

    def test_pure_semantic_match(self, seeded_db: SqliteStorage) -> None:
        # Query that doesn't lexically match anything. The stub vector
        # points at proceeding 2 (its embedding is [0.1...]).
        result = search(
            seeded_db.connection,
            _embedder(vector=[0.1] * EMBEDDING_DIM),
            query="zzzpangram nonexistent",
        )
        # FTS finds nothing, vec finds all three; #2 is closest.
        assert result.total >= 1
        assert result.results[0].granule_id == "CREC-2026-05-22-pt1-PgS001-2"

    def test_results_grouped_by_proceeding(self, tmp_path: Path) -> None:
        """A proceeding with multiple matching chunks appears once."""
        storage = SqliteStorage(tmp_path / "out.db")
        storage.write(_make_proceeding("CREC-2026-05-22-pt1-PgS001-1"))
        # Two chunks under the same granule.
        _add_chunk(
            storage,
            "CREC-2026-05-22-pt1-PgS001-1",
            "First chunk discusses banking regulation thoroughly",
            [0.9] * EMBEDDING_DIM,
        )
        _add_extra_chunk(
            storage,
            "CREC-2026-05-22-pt1-PgS001-1",
            1,
            "Second chunk also mentions banking matters",
            [0.85] * EMBEDDING_DIM,
        )
        result = search(storage.connection, _embedder(), query="banking")
        # Only one row even though two chunks matched.
        assert result.total == 1
        assert result.results[0].granule_id == "CREC-2026-05-22-pt1-PgS001-1"
        storage.close()


# -- filters ------------------------------------------------------------------


class TestFilters:
    def test_date_filter_excludes_out_of_range(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "out.db")
        storage.write(_make_proceeding("CREC-2026-05-22-pt1-PgS001-1", issue_date="2026-05-22"))
        storage.write(_make_proceeding("CREC-2026-01-10-pt1-PgS001-2", issue_date="2026-01-10"))
        _add_chunk(
            storage,
            "CREC-2026-05-22-pt1-PgS001-1",
            "Banking regulation in May 2026",
            [0.5] * EMBEDDING_DIM,
        )
        _add_chunk(
            storage,
            "CREC-2026-01-10-pt1-PgS001-2",
            "Banking regulation in January 2026",
            [0.5] * EMBEDDING_DIM,
        )

        # Filter to May only.
        result = search(
            storage.connection,
            _embedder(),
            query="banking",
            date_from=date(2026, 5, 1),
            date_to=date(2026, 5, 31),
        )
        assert result.total == 1
        assert result.results[0].granule_id == "CREC-2026-05-22-pt1-PgS001-1"
        storage.close()

    def test_section_filter_excludes_other_sections(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "out.db")
        storage.write(_make_proceeding("CREC-2026-05-22-pt1-PgS001-1", section="Senate Section"))
        storage.write(_make_proceeding("CREC-2026-05-22-pt1-PgH001-2", section="House Section"))
        _add_chunk(storage, "CREC-2026-05-22-pt1-PgS001-1", "banking talk", [0.5] * EMBEDDING_DIM)
        _add_chunk(storage, "CREC-2026-05-22-pt1-PgH001-2", "banking too", [0.5] * EMBEDDING_DIM)

        result = search(storage.connection, _embedder(), query="banking", section="Senate Section")
        assert result.total == 1
        assert result.results[0].section == "Senate Section"
        storage.close()


# -- pagination ---------------------------------------------------------------


class TestPagination:
    def test_offset_pages_through_results(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "out.db")
        granules = [f"CREC-2026-05-22-pt1-PgS{i:03d}-1" for i in range(5)]
        for gid in granules:
            storage.write(_make_proceeding(gid))
            _add_chunk(storage, gid, f"banking discussion {gid}", [0.5] * EMBEDDING_DIM)

        page1 = search(storage.connection, _embedder(), query="banking", limit=2, offset=0)
        page2 = search(storage.connection, _embedder(), query="banking", limit=2, offset=2)
        assert page1.total == 5
        assert page2.total == 5
        assert len(page1.results) == 2
        assert len(page2.results) == 2
        # No overlap.
        seen = {r.granule_id for r in page1.results}
        for r in page2.results:
            assert r.granule_id not in seen
        storage.close()


# -- vector dimension --------------------------------------------------------


class TestVectorDimMismatch:
    def test_wrong_dim_query_raises(self, seeded_db: SqliteStorage) -> None:
        with pytest.raises(ValueError, match="dims, expected"):
            search(
                seeded_db.connection,
                _embedder(vector=[0.0] * 5),  # too short
                query="anything",
            )


# -- get_proceeding ----------------------------------------------------------


class TestGetProceeding:
    def test_returns_dict_for_known(self, seeded_db: SqliteStorage) -> None:
        row = get_proceeding(seeded_db.connection, "CREC-2026-05-22-pt1-PgS001-1")
        assert row is not None
        assert row["granule_id"] == "CREC-2026-05-22-pt1-PgS001-1"
        assert row["title"].startswith("Sample ")

    def test_returns_none_for_unknown(self, seeded_db: SqliteStorage) -> None:
        assert get_proceeding(seeded_db.connection, "CREC-not-real") is None


# -- snippets ----------------------------------------------------------------


class TestSnippets:
    def test_keyword_snippet_highlights_match(self, seeded_db: SqliteStorage) -> None:
        chunk_id = seeded_db.connection.execute(
            "SELECT id FROM chunks WHERE granule_id = ?",
            ("CREC-2026-05-22-pt1-PgS001-1",),
        ).fetchone()[0]
        snip = keyword_snippet(seeded_db.connection, chunk_id, "banking")
        assert "<mark>" in snip
        assert "</mark>" in snip
        assert "banking" in snip.lower()

    def test_keyword_snippet_empty_for_non_matching_chunk(self, seeded_db: SqliteStorage) -> None:
        # Chunk for proceeding 2 has "climate", not "banking".
        chunk_id = seeded_db.connection.execute(
            "SELECT id FROM chunks WHERE granule_id = ?",
            ("CREC-2026-05-22-pt1-PgS001-2",),
        ).fetchone()[0]
        snip = keyword_snippet(seeded_db.connection, chunk_id, "banking")
        assert snip == ""

    def test_semantic_snippet_truncates_long_chunks(self) -> None:
        long_text = "word " * 200
        snip = semantic_snippet(long_text, length=50)
        # Truncated to length plus optional ellipses.
        assert len(snip) <= 60
        assert "…" in snip

    def test_semantic_snippet_returns_short_text_unchanged(self) -> None:
        short = "A short chunk."
        assert semantic_snippet(short) == "A short chunk."

    def test_semantic_snippet_escapes_html(self) -> None:
        snip = semantic_snippet("<script>alert('x')</script>")
        assert "<script>" not in snip
        assert "&lt;script&gt;" in snip

    def test_keyword_snippet_escapes_html_outside_marks(self, tmp_path: Path) -> None:
        """FTS5-returned text is HTML-escaped; only the allowlisted `<mark>` survives."""
        storage = SqliteStorage(tmp_path / "out.db")
        gid = "CREC-2026-05-22-pt1-PgS001-1"
        storage.write(_make_proceeding(gid))
        _add_chunk(
            storage,
            gid,
            "She said <stop> repeatedly during banking testimony today",
            [0.5] * EMBEDDING_DIM,
        )
        chunk_id = storage.connection.execute("SELECT id FROM chunks").fetchone()[0]
        snip = keyword_snippet(storage.connection, chunk_id, "banking")
        # The literal <stop> from the chunk must be HTML-escaped.
        assert "<stop>" not in snip
        assert "&lt;stop&gt;" in snip
        # The <mark> tags around 'banking' must remain.
        assert "<mark>" in snip
        assert "</mark>" in snip
        storage.close()


# silence unused imports we keep for explicitness
_ = (sqlite_vec, Any)
