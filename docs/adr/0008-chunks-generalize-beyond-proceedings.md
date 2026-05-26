# 0008 — Chunks generalize beyond Proceedings

**Status**: Accepted, 2026-05-25.

## Context

[ADR 0005](./0005-chunks-as-unit-of-retrieval.md) defines a Chunk as a span of a Proceeding's text sized for one embedding, and makes chunks the unit of retrieval for both FTS5 and `sqlite-vec`. The schema today assumes every chunk has a `granule_id` foreign key to a Proceeding.

Phase 5 of the roadmap ([docs/plans/members-bills-votes-roadmap.md](../plans/members-bills-votes-roadmap.md)) indexes Bill text for RAG. Bill text is a different source — keyed by `(congress, bill_type, bill_number)`, not `granule_id` — but it wants the same treatment: chunked, embedded, indexed in FTS5, retrievable via the same hybrid-search ranking. The question is whether to extend the existing chunks tables to span source types, or to stand up a parallel set of tables per source.

Two shapes considered:

1. **Per-source tables.** `proceeding_chunks`, `proceeding_chunks_fts`, `vec_proceeding_chunks`; `bill_chunks`, `bill_chunks_fts`, `vec_bill_chunks`. Search queries `UNION` across them and merges results.
2. **One chunks table with a source discriminator.** `chunks` gains `source_type` + `source_id` columns; `chunks_fts` and `vec_chunks` stay single tables. Search queries one set of indexes and filters on `source_type` at the surface.

## Decision

One chunks table, discriminated by `source_type` + `source_id`. Concretely:

```sql
chunks(
  chunk_id INTEGER PRIMARY KEY,
  source_type TEXT NOT NULL,    -- 'proceeding' | 'bill' | future entity types
  source_id TEXT NOT NULL,      -- granule_id, or '<congress>-<type>-<number>', etc.
  ord INTEGER NOT NULL,         -- chunk position within the source
  text TEXT NOT NULL,
  UNIQUE (source_type, source_id, ord)
)
```

`chunks_fts` and `vec_chunks` continue to key on `chunk_id`. RRF and grouping logic from ADR 0005 are unchanged; the surface layer decides whether to roll a result up to a Proceeding page or a Bill page based on `source_type`.

The migration from the current schema is a column add + backfill (`source_type = 'proceeding'`, `source_id = granule_id`) and a rename of the `granule_id` FK column. Stage 2 is regenerable per ADR 0002, so the migration is a re-derive, not a careful in-place ALTER.

## Consequences

**Trade-offs accepted:**

- **`source_id` is a string instead of a typed FK.** SQLite can't have a foreign key that points to different tables depending on a discriminator column. We accept loss of referential integrity at the schema level; the loader enforces it. In practice the trade is invisible — there's no `ON DELETE CASCADE` workflow we'd otherwise rely on, since chunks are rebuilt from source on Stage 2 re-runs.
- **Every chunks query has to handle `source_type` somewhere.** Either in the WHERE clause (when filtering) or in the result handler (when grouping for display). Bounded: there are three query paths total (keyword, semantic, hybrid).
- **A single FTS5 table grows larger than per-source ones combined would individually.** Operationally identical — SQLite FTS5 handles tens of millions of entries fine, and we're nowhere near that.

**Things this buys:**

- **Hybrid search across sources is mechanical.** A query for "infrastructure spending" returns both Proceeding chunks and Bill chunks in one ranked list, scored on the same axis, via the same RRF logic from ADR 0005. No UNION ALL across parallel tables, no cross-table rank reconciliation.
- **The unit-of-retrieval principle from ADR 0005 holds.** Chunks remain the unit; only the source the chunk came from varies. ADR 0005's argument for "score weighting is meaningful because both signals score the same span" carries over without modification.
- **New entity types with indexable text are additive.** If a v2 phase indexes committee report text or amendment text, it's a new `source_type` value — no new tables, no new index path. The cost of adding a source is a loader change and a surface-page route.
- **The surface layer dispatches cleanly.** A search result hands back `(source_type, source_id, chunk_text, score)`. The web layer routes to `/proceedings/{id}` or `/bills/{id}` based on `source_type`. Snippet rendering is identical.

**What stays open:**

- **Embedding model per source type.** [ADR 0004](./0004-openai-embeddings.md) uses one OpenAI embedding model across all chunks. If a future source benefits from a different model (e.g. a code-aware model for statutory text), `vec_chunks` would need to split by model or carry a `model_id` column. Not a v1 concern.
- **Per-source chunk size tuning.** Bill text has different structural conventions than Congressional Record articles (sections, subsections, enumerated paragraphs). The 512-token / 100-overlap default from ADR 0005 may not be optimal for bill text. Tunable per source if we ever care; v1 uses the default.
- **Speaker attribution from ADR 0005's "What stays open" section** is now even more clearly Proceeding-specific. When that work lands in Stage 3, it touches `source_type = 'proceeding'` chunks only. The generalized table makes this distinction explicit instead of implicit.

## Rejected: per-source chunk tables

Standing up `bill_chunks`, `vec_bill_chunks`, `bill_chunks_fts` in parallel to the Proceeding tables would keep schemas perfectly typed and let each source have its own FK to its parent. It was rejected because it breaks the central property ADR 0005 establishes — that hybrid search operates on one ranked list of chunks. With parallel tables, every cross-source search becomes a UNION ALL that has to reconcile rankings from two independently-scored indexes, and the RRF math gets fuzzy. The unit of retrieval would silently become "chunks within a source type", not "chunks", and the ADR 0005 argument would have to be re-derived per source.

The typing benefit is real but small — `source_id` as a string costs nothing operationally and only matters at schema review time. The hybrid-search clarity benefit is permanent and load-bearing for the Phase 5 surface.
