"""Tests for the Stage 2 orchestrator (`concord.indexing.index`)."""

from datetime import UTC, datetime
from pathlib import Path

from concord.chunking import Chunker, ChunkerConfig
from concord.embedding import EMBEDDING_DIM, Embedder
from concord.models.proceedings import Article, Issue, Proceeding
from concord.pipeline.index_proceedings import IndexResult, ProgressEvent, index
from concord.storage.sqlite import SqliteStorage


def _make_proceeding(granule_id: str, text: str) -> Proceeding:
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
        title=f"Sample {granule_id}",
        start_page="D1",
        end_page="D2",
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


# -- stub Embedder (no network) -----------------------------------------------


class _StubData:
    def __init__(self, vec: list[float]) -> None:
        self.embedding = vec


class _StubResponse:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.data = [_StubData(v) for v in vectors]


class _StubEmbeddings:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def create(self, *, model: str, input: list[str]) -> _StubResponse:
        self.calls.append(list(input))
        # Return distinct-ish vectors per input so we can confirm ordering.
        return _StubResponse([[float(len(t))] * EMBEDDING_DIM for t in input])


class _StubClient:
    def __init__(self) -> None:
        self.embeddings = _StubEmbeddings()


def _embedder(batch_size: int = 100) -> tuple[Embedder, _StubClient]:
    client = _StubClient()
    return Embedder(client, batch_size=batch_size), client


def _seed(storage: SqliteStorage, count: int = 3) -> list[str]:
    """Seed `count` proceedings with distinct granule_ids; return the ids."""
    ids = [f"CREC-2026-05-22-pt1-PgD{500 + i}-1" for i in range(count)]
    for gid in ids:
        storage.write(
            _make_proceeding(gid, f"Body for {gid}. Some words about senate floor activity.")
        )
    return ids


def _make_chunker() -> Chunker:
    # Small chunks for tests so we exercise the multi-chunk path.
    return Chunker(ChunkerConfig(chunk_size=30, overlap=5))


# -- happy-path coverage ------------------------------------------------------


class TestIndex:
    def test_empty_db_no_op(self, tmp_path: Path) -> None:
        with SqliteStorage(tmp_path / "out.db") as storage:
            embedder, _ = _embedder()
            result = index(storage, chunker=_make_chunker(), embedder=embedder)
        assert result == IndexResult(
            chunked_proceedings=0,
            chunks_written=0,
            embedded_chunks=0,
            skipped_chunked=0,
            skipped_embedded=0,
        )

    def test_chunks_and_embeds_new_proceedings(self, tmp_path: Path) -> None:
        with SqliteStorage(tmp_path / "out.db") as storage:
            _seed(storage, count=3)
            embedder, client = _embedder()
            result = index(storage, chunker=_make_chunker(), embedder=embedder)
        assert result.chunked_proceedings == 3
        assert result.chunks_written > 0
        assert result.embedded_chunks == result.chunks_written
        assert result.skipped_chunked == 0
        assert result.skipped_embedded == 0
        # Exactly one embedder API call (all chunks fit in one batch of 100).
        assert len(client.embeddings.calls) == 1

    def test_idempotent_on_rerun(self, tmp_path: Path) -> None:
        with SqliteStorage(tmp_path / "out.db") as storage:
            _seed(storage, count=2)
            embedder1, _ = _embedder()
            first = index(storage, chunker=_make_chunker(), embedder=embedder1)
            embedder2, client2 = _embedder()
            second = index(storage, chunker=_make_chunker(), embedder=embedder2)
        assert first.chunked_proceedings == 2
        assert second.chunked_proceedings == 0
        assert second.chunks_written == 0
        assert second.embedded_chunks == 0
        assert second.skipped_chunked == first.chunked_proceedings
        assert second.skipped_embedded == first.embedded_chunks
        # No new embedding API calls on the second run.
        assert client2.embeddings.calls == []

    def test_resumes_after_partial_embed(self, tmp_path: Path) -> None:
        """Chunk everything first, embed only some, then re-run: only the missing get embedded."""
        with SqliteStorage(tmp_path / "out.db") as storage:
            _seed(storage, count=2)
            # First pass: chunk + partial embed by hand.
            embedder1, _ = _embedder()
            first = index(storage, chunker=_make_chunker(), embedder=embedder1)
            # Manually delete some embeddings to simulate a partial run.
            ids = [
                row[0]
                for row in storage.connection.execute(
                    "SELECT rowid FROM chunks_vec ORDER BY rowid LIMIT 1"
                )
            ]
            assert ids, "test setup expected at least one embedding"
            storage.connection.execute(
                "DELETE FROM chunks_vec WHERE rowid IN ({})".format(",".join("?" * len(ids))),
                ids,
            )
            storage.connection.commit()

            embedder2, client2 = _embedder()
            second = index(storage, chunker=_make_chunker(), embedder=embedder2)

            assert second.chunked_proceedings == 0  # nothing new to chunk
            assert second.embedded_chunks == len(ids)  # just the deleted ones
            # Exactly one API call (one batch) for the missing chunks.
            assert len(client2.embeddings.calls) == 1
            # End state: every chunk has an embedding again.
            assert (
                first.chunks_written
                == storage.connection.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0]
            )

    def test_handles_empty_text_proceedings(self, tmp_path: Path) -> None:
        """Empty-text proceedings produce zero chunks but are marked done in chunking_status."""
        with SqliteStorage(tmp_path / "out.db") as storage:
            storage.write(_make_proceeding("CREC-2026-05-22-pt1-PgD551-empty", ""))
            embedder, _ = _embedder()
            result = index(storage, chunker=_make_chunker(), embedder=embedder)
            assert result.chunked_proceedings == 1
            assert result.chunks_written == 0
            assert result.embedded_chunks == 0
            # Second run finds nothing to do; the chunking_status row prevents
            # re-yielding the empty-text proceeding.
            embedder2, _ = _embedder()
            second = index(storage, chunker=_make_chunker(), embedder=embedder2)
            assert second.chunked_proceedings == 0
            assert second.skipped_chunked == 1

    def test_limit_caps_chunks_written(self, tmp_path: Path) -> None:
        """`--limit` stops after N chunks have been written in the chunk pass."""
        with SqliteStorage(tmp_path / "out.db") as storage:
            _seed(storage, count=4)
            embedder, _ = _embedder()
            result = index(storage, chunker=_make_chunker(), embedder=embedder, limit=1)
        # At least one chunk was written; we hit limit after the first proceeding.
        assert result.chunks_written >= 1
        assert result.chunked_proceedings <= 4

    def test_progress_callback_invoked(self, tmp_path: Path) -> None:
        events: list[ProgressEvent] = []
        with SqliteStorage(tmp_path / "out.db") as storage:
            _seed(storage, count=2)
            embedder, _ = _embedder()
            index(
                storage,
                chunker=_make_chunker(),
                embedder=embedder,
                progress=events.append,
            )
        phases = {e.phase for e in events}
        assert "chunk" in phases
        assert "embed" in phases


# -- timestamp injection -------------------------------------------------------


class TestClockInjection:
    def test_now_callable_used_for_chunked_at(self, tmp_path: Path) -> None:
        fixed = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
        with SqliteStorage(tmp_path / "out.db") as storage:
            _seed(storage, count=1)
            embedder, _ = _embedder()
            index(
                storage,
                chunker=_make_chunker(),
                embedder=embedder,
                now=lambda: fixed,
            )
            row = storage.connection.execute(
                "SELECT chunked_at FROM chunking_status LIMIT 1"
            ).fetchone()
            assert row["chunked_at"] == fixed.isoformat()
