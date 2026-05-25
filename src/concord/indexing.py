"""Stage 2 orchestrator: chunk every proceeding, then embed every chunk.

Two passes, both idempotent and crash-safe:

1. **Chunk pass.** For every proceeding without a chunking-status row,
   run the chunker, bulk-insert the resulting chunks, and record the
   chunking-status. FTS5 auto-populates via the trigger on ``chunks``.

2. **Embed pass.** For every chunk without a row in ``chunks_vec``, batch
   the texts and call the embedder, then bulk-insert the embeddings.

A crashed run leaves at worst one proceeding's chunks + one batch's
embeddings unprocessed; the next invocation picks them up from the same
"find rows without their derived data" query.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import NamedTuple

from .chunking import Chunk, Chunker
from .embedding import Embedder
from .models import Proceeding
from .storage.sqlite import SqliteStorage


class IndexResult(NamedTuple):
    """Counts produced by one :func:`index` invocation."""

    chunked_proceedings: int
    chunks_written: int
    embedded_chunks: int
    skipped_chunked: int
    skipped_embedded: int


class ProgressEvent(NamedTuple):
    """Emitted by :func:`index` for caller-supplied progress reporting."""

    phase: str  # "chunk" or "embed"
    processed: int  # cumulative count within this phase
    most_recent_granule_id: str | None  # for the chunk phase
    most_recent_chunk_id: int | None  # for the embed phase


def index(
    storage: SqliteStorage,
    *,
    chunker: Chunker,
    embedder: Embedder,
    limit: int | None = None,
    progress: Callable[[ProgressEvent], None] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> IndexResult:
    """Run the chunk pass then the embed pass against ``storage``.

    Parameters
    ----------
    storage:
        :class:`SqliteStorage` opened with ``load_vec=True`` (default).
    chunker:
        Configured :class:`Chunker`. The current chunker config is captured
        by side effect: changing the config and re-running requires manual
        ``DELETE FROM chunks; DELETE FROM chunking_status; DELETE FROM chunks_vec;``.
    embedder:
        Configured :class:`Embedder` wrapping an OpenAI client.
    limit:
        Cap on **new chunks written** in the chunk pass. The embed pass
        still processes every unembedded chunk (including any pre-existing
        ones from prior runs).
    progress:
        Optional callback. Called with a :class:`ProgressEvent` after each
        proceeding is chunked and after each embedding batch lands.
    now:
        Injection point for ``datetime.now(UTC)`` so tests can pin the
        ``chunked_at`` timestamp written into ``chunking_status``.

    Returns
    -------
    IndexResult
        Counts for this run. ``skipped_*`` reflects work already done by
        a previous invocation.
    """
    chunked_proceedings = 0
    chunks_written = 0
    skipped_chunked = 0

    chunked_at = _format_now(now)

    # -- pass 1: chunk ----------------------------------------------------
    for granule_id, text in storage.proceedings_without_chunks():
        chunks = chunker.chunk(text)
        rows: list[tuple[int, str, int, int]] = [
            (c.chunk_index, c.text, c.char_start, c.char_end) for c in chunks
        ]
        storage.bulk_insert_chunks(granule_id, rows, chunked_at=chunked_at)
        chunked_proceedings += 1
        chunks_written += len(chunks)
        if progress is not None:
            progress(
                ProgressEvent(
                    phase="chunk",
                    processed=chunked_proceedings,
                    most_recent_granule_id=granule_id,
                    most_recent_chunk_id=None,
                )
            )
        if limit is not None and chunks_written >= limit:
            break

    # Count anything already chunked before this run. Doing it here (not at
    # the start) avoids double-counting proceedings this run just handled.
    skipped_chunked = _count_chunking_status(storage) - chunked_proceedings

    # -- pass 2: embed ----------------------------------------------------
    embedded_chunks = 0
    skipped_embedded = _count_embedded(storage)

    batch: list[tuple[int, str]] = []
    for chunk_id, text in storage.chunks_without_embeddings():
        batch.append((chunk_id, text))
        if len(batch) >= embedder.batch_size:
            embedded_chunks += _embed_and_write(storage, embedder, batch, progress)
            batch = []
    if batch:
        embedded_chunks += _embed_and_write(storage, embedder, batch, progress)

    return IndexResult(
        chunked_proceedings=chunked_proceedings,
        chunks_written=chunks_written,
        embedded_chunks=embedded_chunks,
        skipped_chunked=max(skipped_chunked, 0),
        skipped_embedded=skipped_embedded,
    )


# -- helpers ----------------------------------------------------------------


def _embed_and_write(
    storage: SqliteStorage,
    embedder: Embedder,
    batch: list[tuple[int, str]],
    progress: Callable[[ProgressEvent], None] | None,
) -> int:
    """Call the embedder for one batch, then bulk-insert the resulting vectors."""
    chunk_ids = [chunk_id for chunk_id, _ in batch]
    texts = [text for _, text in batch]
    vectors = embedder.embed(texts)
    rows: list[tuple[int, list[float]]] = list(zip(chunk_ids, vectors, strict=True))
    storage.bulk_insert_embeddings(rows)
    if progress is not None:
        progress(
            ProgressEvent(
                phase="embed",
                processed=len(batch),
                most_recent_granule_id=None,
                most_recent_chunk_id=chunk_ids[-1] if chunk_ids else None,
            )
        )
    return len(rows)


def _count_chunking_status(storage: SqliteStorage) -> int:
    cursor = storage.connection.execute("SELECT COUNT(*) FROM chunking_status")
    (count,) = cursor.fetchone()
    return int(count)


def _count_embedded(storage: SqliteStorage) -> int:
    cursor = storage.connection.execute("SELECT COUNT(*) FROM chunks_vec")
    (count,) = cursor.fetchone()
    return int(count)


def _format_now(now: Callable[[], datetime]) -> str:
    """ISO-8601 string with timezone for the ``chunking_status.chunked_at`` column."""
    return now().isoformat()


# Re-export for callers that want to introspect a Chunk shape without
# importing through chunking explicitly.
__all__ = [
    "Chunk",
    "IndexResult",
    "Proceeding",
    "ProgressEvent",
    "index",
]
