"""Hybrid search query layer.

Given a query string + optional metadata filters, runs two retrievals
(FTS5 keyword + ``sqlite-vec`` semantic) at chunk granularity, fuses
them via Reciprocal Rank Fusion, groups results by proceeding, and
returns a top-N list of proceedings each with its best-matching chunk.

Pure function over a :class:`sqlite3.Connection` and an
:class:`Embedder`. No FastAPI knowledge — the web layer is just one
caller. Other callers (CLI smoke tests, future analysis scripts) work
the same way.
"""

import sqlite3
from datetime import date
from typing import Any

import sqlite_vec  # type: ignore[import-untyped]
from pydantic import BaseModel

from ..embedding import EMBEDDING_DIM, Embedder

#: Per-signal retrieval cap. We pull 200 chunks from each index then let
#: RRF + group-by reduce to the final result page. Larger than any
#: reasonable display limit so a chunk that ranks #50 in keyword and
#: #50 in semantic can still surface in the final top 20.
_RETRIEVAL_K = 200

#: RRF constant. The standard value from Cormack et al.'s 2009 paper.
_RRF_K = 60


class ProceedingResult(BaseModel):
    """One result row: a proceeding + its best matching chunk."""

    granule_id: str
    issue_date: date
    section: str
    title: str
    start_page: str
    end_page: str
    text_url: str
    pdf_url: str
    score: float
    chunk_id: int
    chunk_text: str
    chunk_char_start: int
    chunk_char_end: int


class SearchResults(BaseModel):
    """Top-N proceedings for a search request."""

    query: str
    total: int
    offset: int
    limit: int
    results: list[ProceedingResult]


def search(
    db: sqlite3.Connection,
    embedder: Embedder,
    *,
    query: str,
    date_from: date | None = None,
    date_to: date | None = None,
    section: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> SearchResults:
    """Run hybrid search and return one row per proceeding, RRF-ranked.

    Empty / whitespace-only ``query`` returns an empty result set without
    touching the database or the embedder.

    Filters are applied *after* retrieval rather than as part of the
    initial ``MATCH``: applying them earlier would interact awkwardly
    with FTS5's BM25 ranking and ``sqlite-vec``'s KNN limit. Post-
    filtering against a 200-per-signal retrieval cap is wasteful but
    correct, and easy to reason about.
    """
    if not query.strip():
        return SearchResults(query=query, total=0, offset=offset, limit=limit, results=[])

    # 1. Keyword retrieval over chunks_fts. Returns (chunk_id, bm25_rank).
    fts_ranked = _fts_search(db, query)

    # 2. Semantic retrieval over chunks_vec. Returns (chunk_id, vec_rank).
    query_vec = embedder.embed([query])[0]
    vec_ranked = _vec_search(db, query_vec)

    # 3. Reciprocal Rank Fusion over chunk IDs.
    fused = _rrf_fuse(fts_ranked, vec_ranked)
    if not fused:
        return SearchResults(query=query, total=0, offset=offset, limit=limit, results=[])

    # 4. Roll up chunks -> proceedings, keep best chunk per proceeding,
    #    apply metadata filters, paginate.
    proceedings = _rollup_to_proceedings(
        db,
        fused,
        date_from=date_from,
        date_to=date_to,
        section=section,
    )

    paged = proceedings[offset : offset + limit]
    return SearchResults(
        query=query,
        total=len(proceedings),
        offset=offset,
        limit=limit,
        results=paged,
    )


# -- per-signal retrievals --------------------------------------------------


def _fts_search(db: sqlite3.Connection, query: str) -> list[tuple[int, int]]:
    """Return ``[(chunk_id, rank_position)]`` from FTS5, best first.

    FTS5 ``MATCH`` is unforgiving of unescaped query syntax (parens,
    quotes, AND/OR keywords). We pass the user's query verbatim wrapped
    in double-quotes so it's treated as a phrase, which is the most
    user-friendly default for a search box. Advanced query syntax is a
    deliberate non-goal for the demo.
    """
    safe = '"' + query.replace('"', '""') + '"'
    cursor = db.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
        (safe, _RETRIEVAL_K),
    )
    return [(int(row[0]), pos) for pos, row in enumerate(cursor.fetchall(), 1)]


def _vec_search(db: sqlite3.Connection, query_vec: list[float]) -> list[tuple[int, int]]:
    """Return ``[(chunk_id, rank_position)]`` from sqlite-vec KNN, best first."""
    if len(query_vec) != EMBEDDING_DIM:
        raise ValueError(f"query embedding has {len(query_vec)} dims, expected {EMBEDDING_DIM}")
    cursor = db.execute(
        "SELECT rowid FROM chunks_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (sqlite_vec.serialize_float32(query_vec), _RETRIEVAL_K),
    )
    return [(int(row[0]), pos) for pos, row in enumerate(cursor.fetchall(), 1)]


# -- fusion + roll-up --------------------------------------------------------


def _rrf_fuse(
    fts_ranked: list[tuple[int, int]],
    vec_ranked: list[tuple[int, int]],
    k: int = _RRF_K,
) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion. Higher score = better."""
    scores: dict[int, float] = {}
    for chunk_id, rank in fts_ranked:
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    for chunk_id, rank in vec_ranked:
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: -x[1])


def _rollup_to_proceedings(
    db: sqlite3.Connection,
    fused: list[tuple[int, float]],
    *,
    date_from: date | None,
    date_to: date | None,
    section: str | None,
) -> list[ProceedingResult]:
    """Group fused chunks by proceeding (best chunk wins), apply filters."""
    if not fused:
        return []

    # Pull metadata for every chunk in fused order, in one query.
    chunk_ids = [cid for cid, _ in fused]
    placeholders = ",".join("?" for _ in chunk_ids)
    rows = db.execute(
        f"""
        SELECT
            c.id AS chunk_id,
            c.text AS chunk_text,
            c.char_start AS chunk_char_start,
            c.char_end AS chunk_char_end,
            c.granule_id AS granule_id,
            p.issue_date AS issue_date,
            p.section AS section,
            p.title AS title,
            p.start_page AS start_page,
            p.end_page AS end_page,
            p.text_url AS text_url,
            p.pdf_url AS pdf_url
        FROM chunks c
        JOIN proceedings p ON p.granule_id = c.granule_id
        WHERE c.id IN ({placeholders})
        """,
        chunk_ids,
    ).fetchall()

    by_chunk: dict[int, sqlite3.Row] = {row["chunk_id"]: row for row in rows}
    scores_by_chunk = dict(fused)

    # Walk fused order (best first); keep first chunk seen per granule_id.
    seen_granules: set[str] = set()
    results: list[ProceedingResult] = []
    for chunk_id, _score in fused:
        row = by_chunk.get(chunk_id)
        if row is None:
            continue
        granule_id = row["granule_id"]
        if granule_id in seen_granules:
            continue
        # Apply metadata filters.
        issue_date_str = row["issue_date"]
        if date_from is not None and issue_date_str < date_from.isoformat():
            continue
        if date_to is not None and issue_date_str > date_to.isoformat():
            continue
        if section is not None and row["section"] != section:
            continue

        seen_granules.add(granule_id)
        results.append(
            ProceedingResult(
                granule_id=granule_id,
                issue_date=date.fromisoformat(issue_date_str),
                section=row["section"],
                title=row["title"],
                start_page=row["start_page"],
                end_page=row["end_page"],
                text_url=row["text_url"],
                pdf_url=row["pdf_url"],
                score=scores_by_chunk[chunk_id],
                chunk_id=chunk_id,
                chunk_text=row["chunk_text"],
                chunk_char_start=row["chunk_char_start"],
                chunk_char_end=row["chunk_char_end"],
            )
        )
    return results


# -- single-doc lookup ------------------------------------------------------


def get_proceeding(db: sqlite3.Connection, granule_id: str) -> dict[str, Any] | None:
    """Fetch a single proceeding's full row by ``granule_id``.

    Returns ``None`` if not found. Returned dict is the row as-stored —
    callers that want a Pydantic :class:`Proceeding` can pass it through
    :meth:`Proceeding.model_validate`.
    """
    row = db.execute("SELECT * FROM proceedings WHERE granule_id = ?", (granule_id,)).fetchone()
    if row is None:
        return None
    return dict(row)
