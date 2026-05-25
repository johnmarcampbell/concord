# 0005 — Chunks as the unit of retrieval

**Status**: Accepted, 2026-05-24.

## Context

Search in Concord combines two signals: keyword (FTS5) and semantic (vector similarity). Vector search has to operate on chunks because Proceedings vary in length from one paragraph to multi-hour floor speeches, and a single embedding over a 50,000-token proceeding loses all semantic precision. The question was whether FTS5 should *also* operate on chunks, or on whole proceedings.

Two shapes considered:

1. **Proceedings are the FTS unit.** FTS5 indexes whole proceeding text; vector search returns chunks that must be aggregated up to proceeding scores; combining FTS and vec means reconciling different units.
2. **Chunks are the FTS unit too.** Both indexes share `chunk_id`. Search returns chunks; the application layer dedupes them up to proceedings for display.

## Decision

Chunks are Concord's unit of retrieval. Both FTS5 and `sqlite-vec` index the chunks table. The query layer combines them via Reciprocal Rank Fusion on `chunk_id` and groups results by `granule_id` (proceeding) for display.

## Consequences

**Trade-offs accepted:**

- **Phrases at chunk boundaries can be missed by FTS5.** Mitigated by 100-token chunk overlap (~20% of the 512-token chunk size), so any phrase shorter than 100 tokens appears in at least one chunk in full. For longer phrases, you can usually still match via the prefix or suffix in adjacent chunks; rank suffers slightly.
- **The FTS5 index grows ~3–4× compared to per-proceeding indexing** (one FTS entry per chunk instead of per proceeding). At our scale this means a ~1M-entry FTS table instead of ~290k. SQLite handles tens of millions of FTS entries without issue.
- **Embedding cost grows ~20%** because of overlap. For OpenAI `text-embedding-3-small` this is ~$0.60 added to a $3 backfill. Trivial.

**Things this buys:**

- **Hybrid search is mechanical, not stitched together.** Both indexes return `(chunk_id, score)` pairs. RRF combines them by rank in a CTE; group-by `granule_id` rolls them up. Score semantics line up because both indexes are scoring the same span of text.
- **Score weighting is meaningful.** When FTS and vector are scoring the same chunk, "60% keyword / 40% semantic" actually means something. When they're scoring different units (chunk vs full proceeding), any weighting is hand-wavy.
- **Snippets fall out for free.** A chunk *is* the snippet, or close to it — no separate snippet extraction pass.
- **Future re-ranking signals layer on cleanly.** Anything else we want to weight (entity match strength, recency boost, speaker-attribution score) can join on `chunk_id` and contribute another term to the RRF score.

**What stays open:**

- **Speaker turn boundaries don't align with chunk boundaries.** Floor-debate transcripts mix multiple speakers within a single 512-token window. When stage 3 (entity extraction + speaker attribution) lands, we may want to align chunking with speaker turns so that "what did X say about Y" can attribute matched chunks to the correct speaker. This is a Stage 3 problem to revisit; for stage 2 we accept that chunks are speaker-agnostic.
- The 512-token / 100-overlap configuration is a default, not a religion. Tunable behind one parameter; re-tuning is a stage 2 re-run.

## Rejected: proceedings as the unit

The natural-feeling default ("a search returns documents") — but it forces the hybrid search ranking to combine two different units into one score, which is either lossy (aggregating chunk-level vec scores up to proceedings) or hand-wavy (normalizing across heterogeneous score scales). Chunks-as-unit pushes the deduplication to the display layer where it's a simple `GROUP BY`, and leaves the retrieval layer with a clean signal pipeline.
