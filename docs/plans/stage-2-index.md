# Stage 2 — Index (chunk + FTS5 + embed)

> Build the second half of the derived-store pipeline: chunk every proceeding in the SQLite mirror into ~512-token chunks, populate an FTS5 keyword index over the chunks, and store an OpenAI vector embedding per chunk in a `sqlite-vec` virtual table. New `concord index` CLI, idempotent per chunk and per embedding so a crashed run resumes cleanly.

## Source

- Architecture decisions resolved during a `grill-with-docs` session on 2026-05-24. Relevant outputs:
  - [CONTEXT.md](../../CONTEXT.md) — domain glossary, including "Stage 2 — Index" and the definitions of *Chunk*, *Keyword search*, *Semantic search*, *Hybrid search*, and *RRF*.
  - [ADR-0003 — SQLite as the derived store](../adr/0003-sqlite-as-derived-store.md)
  - [ADR-0004 — OpenAI for embeddings](../adr/0004-openai-embeddings.md)
  - [ADR-0005 — Chunks as the unit of retrieval](../adr/0005-chunks-as-unit-of-retrieval.md)
- The prerequisite for this plan: [Stage 1 — Load](./stage-1-load.md). Stage 2 reads the `proceedings` table that Stage 1 produces.
- No GitHub issue exists for this work yet. File one before starting if you want PR/branch tracking.

## Context

After Stage 1, the SQLite file (`proceedings.db`) holds a `proceedings` table mirroring the canonical JSONL. That table is queryable by metadata but not searchable by content — there's no full-text index, no semantic index, no way for the eventual web layer to answer "find proceedings about bank regulation."

Stage 2 builds those search structures. It splits each proceeding's `text` field into overlapping chunks, indexes the chunks with SQLite's built-in FTS5 extension (for keyword search) and the third-party `sqlite-vec` extension (for vector similarity), and stores an OpenAI embedding for each chunk. Chunks are the unit of retrieval (see ADR-0005); both indexes operate at chunk granularity, and the eventual query layer combines them via Reciprocal Rank Fusion.

Stage 2 is regenerable from Stage 1's output, which is itself regenerable from the canonical JSONL. The pipeline's "delete derived stores, rebuild from JSONL" recovery path includes everything this plan builds.

## Goals

1. Extend the `proceedings.db` schema with: a `chunks` table; the `chunks_fts` FTS5 virtual table indexing chunks; triggers keeping `chunks_fts` in sync with `chunks`; the `chunks_vec` `sqlite-vec` virtual table holding one embedding per chunk row.
2. A `Chunker` that splits a proceeding's text into ~512-token chunks with 100-token overlap, preferring paragraph/sentence boundaries when possible.
3. An `Embedder` that calls OpenAI's `text-embedding-3-small` in batches and returns the resulting vectors.
4. An `index()` orchestrator with two idempotent passes: (a) chunk every proceeding that hasn't been chunked, (b) embed every chunk that hasn't been embedded.
5. New CLI command `concord index --db PATH [--limit N]` that runs the orchestrator end-to-end, prints a summary, and is safe to interrupt and resume.
6. Tests covering the chunker (boundary behavior, overlap, token-budget enforcement), the embedder (batching, OpenAI client injection), the schema additions, the orchestrator's two-pass idempotency, and the CLI's flag parsing.

## Non-goals

1. **Query / search endpoints.** This plan builds the indexes. Querying them (hybrid retrieval, RRF, snippet rendering) is a separate plan for the web layer.
2. **Re-chunking and re-embedding flags.** If chunking strategy or embedding model changes, the recovery path is `DELETE FROM chunks; DELETE FROM chunks_vec;` followed by `concord index`. A `--rebuild-chunks` / `--rebuild-embeddings` UX is a worthwhile follow-up but isn't blocking the initial demo.
3. **Multiple embedding models.** Single model: `text-embedding-3-small`. Switching models means dropping `chunks_vec` and re-embedding; no "embedding model" column per chunk.
4. **Streaming or async OpenAI calls.** A 5-year backfill at batch-of-100 is ~50 minutes of sequential API calls; that's acceptable. Concurrency / async can be added later if it matters.
5. **Local-model fallback for embeddings.** ADR-0004 committed to OpenAI. No local sentence-transformers code.
6. **Speaker-turn-aware chunking.** ADR-0005 calls this out as a Stage 3 concern. Chunks here are speaker-agnostic.
7. **Removing `JsonlStorage` or `MongoStorage`.** Unchanged.

## Relevant prior decisions

- **ADR-0003 — SQLite as the derived store** ([docs/adr/0003-sqlite-as-derived-store.md](../adr/0003-sqlite-as-derived-store.md)). All Stage 2 indexes live in the same `proceedings.db` file Stage 1 created, joined via SQL.
- **ADR-0004 — OpenAI for embeddings** ([docs/adr/0004-openai-embeddings.md](../adr/0004-openai-embeddings.md)). Determines the model (`text-embedding-3-small`), the embedding dimension (1536), and the auth model (`OPENAI_API_KEY` env var).
- **ADR-0005 — Chunks as the unit of retrieval** ([docs/adr/0005-chunks-as-unit-of-retrieval.md](../adr/0005-chunks-as-unit-of-retrieval.md)). Determines that *both* FTS5 and `sqlite-vec` index chunks (not whole proceedings), and that the 100-token overlap is mandatory for keyword recall across chunk boundaries.
- **Stage 1 — Load** ([docs/plans/stage-1-load.md](./stage-1-load.md)). Defines the `SqliteStorage` class and the `proceedings` table this plan extends. In particular, this plan must not break Stage 1's tests: `SqliteStorage` must still satisfy the `Storage` Protocol unchanged, and `concord load` must continue to work on a fresh DB.

## Relevant files and code

Paths verified to exist (or to-be-created as part of Stage 1):

- `src/concord/models.py` — defines `Proceeding`. This plan adds a `Chunk` Pydantic model alongside it (or in a new `chunking.py` module — see Approach).
- `src/concord/storage/sqlite.py` *(created by Stage 1)* — `SqliteStorage` class. This plan extends its schema-init code with chunk/FTS/vec DDL, and adds helper methods (`chunks_for`, `proceedings_without_chunks`, `chunks_without_embeddings`, `bulk_insert_chunks`, `bulk_insert_embeddings`).
- `src/concord/cli.py` — Typer app. Add a new `@app.command("index")` next to the existing `pull_command` and (Stage 1's) `load_command`.
- `tests/test_storage_sqlite.py` *(created by Stage 1)* — extend with Stage 2 schema tests.
- `tests/test_cli.py` — pattern for CLI tests; add `index` command tests here.
- `pyproject.toml` — add three new runtime dependencies (`openai`, `tiktoken`, `sqlite-vec`) and one new dev dependency for embedding-call mocking (recommendation: none — wire `openai.OpenAI` injection so tests pass an in-process stub).

New files this plan creates:

- `src/concord/chunking.py` — `Chunker` class, `ChunkerConfig`, `Chunk` Pydantic model, custom recursive splitter implementation.
- `src/concord/embedding.py` — `Embedder` class wrapping `openai.OpenAI`, batch-call helper, error handling.
- `src/concord/indexing.py` — `index(storage, chunker, embedder, ...) -> IndexResult` orchestrator. Mirrors the shape of `src/concord/pipeline.py:pull`.
- `tests/test_chunking.py`
- `tests/test_embedding.py`
- `tests/test_indexing.py`

## Approach

### Module layout

Three new modules, each focused:

- `chunking.py` — text-in, chunks-out. No knowledge of SQLite, OpenAI, or proceedings. Easy to test, easy to swap out.
- `embedding.py` — texts-in, vectors-out. Wraps `openai.OpenAI`; injectable client for testing.
- `indexing.py` — orchestrator. Holds the two-pass logic and the SQL.

This matches the shape of `src/concord/pipeline.py` (one orchestrator function, dependencies injected) and `src/concord/api.py` / `src/concord/text.py` (one focused module per external boundary).

### Schema additions

All DDL runs at `SqliteStorage` construction time, guarded by `IF NOT EXISTS`. Loading `sqlite-vec` is deferred so Stage 1 doesn't depend on it — `SqliteStorage.__init__` gains a `load_vec: bool` parameter (default `True`) that controls whether `sqlite_vec.load(connection)` runs and whether the `chunks_vec` virtual table is created.

```sql
-- chunks: one row per chunk, autoincrement id, FK to proceedings
CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    granule_id  TEXT NOT NULL REFERENCES proceedings(granule_id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    text        TEXT NOT NULL,
    char_start  INTEGER NOT NULL,
    char_end    INTEGER NOT NULL,
    UNIQUE(granule_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_chunks_granule_id ON chunks(granule_id);

-- FTS5 over chunks via external content (chunks owns the text, fts owns the index)
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    content='chunks',
    content_rowid='id',
    tokenize='porter unicode61'
);

-- triggers keep chunks_fts in sync with chunks
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

-- sqlite-vec virtual table; rowid mirrors chunks.id so JOIN is direct
-- (only created when load_vec=True)
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
    embedding float[1536]
);
```

Note: `chunks_vec` is *not* connected to `chunks` by a foreign key (virtual tables can't be FK targets). The convention is that `chunks_vec.rowid == chunks.id`. The indexing code is responsible for keeping them in sync (delete chunk → delete corresponding `chunks_vec` row).

### Chunker

```python
@dataclass(frozen=True)
class ChunkerConfig:
    chunk_size: int = 512           # target tokens per chunk
    overlap: int = 100              # tokens of overlap between adjacent chunks
    encoding_name: str = "cl100k_base"  # OpenAI's tokenizer for text-embedding-3-small

class Chunk(BaseModel):
    chunk_index: int
    text: str
    char_start: int
    char_end: int

class Chunker:
    def __init__(self, config: ChunkerConfig = ChunkerConfig()) -> None: ...
    def chunk(self, text: str) -> list[Chunk]: ...
```

The splitter is a small custom recursive implementation (~80 lines), not a dependency:

1. Tokenize the input with `tiktoken.get_encoding(config.encoding_name)`. If the whole text fits in `chunk_size` tokens, return a single `Chunk` covering it.
2. Otherwise, try to split on paragraph boundaries (`\n\n`). For each piece, recurse.
3. If a piece is still too big, try sentence boundaries (regex on `.!?` followed by whitespace).
4. If still too big, try line boundaries (`\n`).
5. Last resort: hard-cut at `chunk_size` tokens.
6. After all recursive splits, walk the resulting list and emit overlapping chunks: each new chunk includes the last `overlap` tokens of the previous one.

Each `Chunk` carries its character offsets into the original text, so the web layer can later highlight matched regions in the full proceeding view if it wants.

The custom implementation buys us: no dependency on `langchain-text-splitters` (which pulls in `langchain-core`), no Rust extension from `semantic-text-splitter`, and full control of an algorithm we'll likely iterate on. Cost: ~80 lines of code + tests.

### Embedder

```python
class Embedder:
    def __init__(
        self,
        client: openai.OpenAI,
        *,
        model: str = "text-embedding-3-small",
        batch_size: int = 100,
    ) -> None: ...
    def embed(self, texts: list[str]) -> list[list[float]]: ...
```

`embed()` batches `texts` into groups of `batch_size`, calls `client.embeddings.create(model=..., input=batch)` for each, and returns flat list-of-vectors in input order. The OpenAI SDK handles retries and rate-limit backoff internally; we don't reimplement it. API key is read from `OPENAI_API_KEY` by the SDK's default — we just construct `openai.OpenAI()` with no args.

### Orchestrator

```python
class IndexResult(NamedTuple):
    chunked_proceedings: int       # new proceedings chunked this run
    chunks_written: int            # new chunk rows written
    embedded_chunks: int           # new embeddings written
    skipped_chunked: int           # proceedings already had chunks
    skipped_embedded: int          # chunks already had embeddings

def index(
    storage: SqliteStorage,
    *,
    chunker: Chunker,
    embedder: Embedder,
    limit: int | None = None,
    progress: Callable[[IndexProgressEvent], None] | None = None,
) -> IndexResult: ...
```

Two passes, each fully idempotent:

**Pass 1 — chunk.** `SELECT granule_id FROM proceedings WHERE granule_id NOT IN (SELECT DISTINCT granule_id FROM chunks)`. For each unchunked proceeding, fetch its text, run `chunker.chunk(text)`, and `bulk_insert_chunks(granule_id, chunks)`. FTS5 syncs via trigger. Optionally emit a progress event per N proceedings.

**Pass 2 — embed.** `SELECT id, text FROM chunks WHERE id NOT IN (SELECT rowid FROM chunks_vec)`. Pull in batches of `batch_size` (matches `Embedder.batch_size`), call `embedder.embed([texts])`, `bulk_insert_embeddings([(id, vec) ...])`. Repeat until no more.

If `limit` is set, it caps *new chunks written in pass 1* (not embeddings). Pass 2 still processes whatever pass 1 left + any prior unembedded chunks.

Crash-safety: both passes commit per-proceeding (pass 1) and per-batch (pass 2). If the process dies mid-run, the next invocation picks up from the same "find rows without their derived data" query. Total wasted work in the worst case: one proceeding's chunks + one batch's embeddings (~$0.002 of OpenAI spend).

### CLI

```
concord index --db PATH [--limit N]
```

- `--db PATH` — required, the SQLite file Stage 1 wrote to
- `--limit N` — cap on new proceedings chunked this run (does not limit embedding)

Reads `OPENAI_API_KEY` from env. Missing key → exit 2 with the same clean-error pattern the existing `pull_command` uses for `CONGRESS_API_KEY`. On success prints:

```
Indexed: chunked 1,247 new proceedings (12,433 new chunks, skipped 0 already chunked); embedded 12,580 new chunks (skipped 0 already embedded)
```

## Step-by-step plan

1. **Add Stage 2 runtime dependencies to `pyproject.toml`.** Append to `dependencies`: `"openai>=1.50"`, `"tiktoken>=0.7"`, `"sqlite-vec>=0.1.6"`. Run `uv sync` and confirm install succeeds.

2. **Extend `SqliteStorage` with chunk/FTS/vec schema.** In `src/concord/storage/sqlite.py`, add the DDL from the Approach section to the `__init__` schema block. Add a `load_vec: bool = True` constructor parameter; when `True`, call `sqlite_vec.load(self._conn)` after enabling extension loading on the connection. When `False`, skip the `CREATE VIRTUAL TABLE chunks_vec` line so Stage 1's tests (which don't require sqlite-vec) keep passing on a freshly-installed environment without the extension wheel. Default `True` is fine for production. Update the Stage 1 test `tests/test_storage_sqlite.py::TestSchema` with new cases for the new tables / triggers (don't remove existing tests).

3. **Add `Chunk` model and `Chunker` to `src/concord/chunking.py`.** Define `ChunkerConfig` (frozen dataclass with defaults from ADR-0005: `chunk_size=512`, `overlap=100`, `encoding_name="cl100k_base"`) and `Chunk` (Pydantic `BaseModel` with `chunk_index: int`, `text: str`, `char_start: int`, `char_end: int`). Implement `Chunker.chunk(text: str) -> list[Chunk]` with the recursive splitter described in Approach. Use `tiktoken.get_encoding(self._config.encoding_name)` for token counting.

4. **Add `Embedder` to `src/concord/embedding.py`.** Wrap an injected `openai.OpenAI` client. Implement `embed(texts: list[str]) -> list[list[float]]` that batches into groups of `batch_size` (default 100) and calls `client.embeddings.create`. Don't reimplement OpenAI's retry logic — let the SDK handle it. Raise a typed `EmbeddingError` on unrecoverable API failures so the orchestrator can surface them cleanly.

5. **Add helper methods to `SqliteStorage` for chunk and embedding I/O.** All in `src/concord/storage/sqlite.py`:
   - `proceedings_without_chunks(limit: int | None) -> Iterator[tuple[str, str]]` — yields `(granule_id, text)` for proceedings with no chunks.
   - `bulk_insert_chunks(granule_id: str, chunks: list[Chunk]) -> int` — inserts all chunks in a single transaction, returns count.
   - `chunks_without_embeddings(limit: int | None) -> Iterator[tuple[int, str]]` — yields `(chunk_id, text)` for chunks not present in `chunks_vec`.
   - `bulk_insert_embeddings(rows: list[tuple[int, list[float]]]) -> int` — inserts `(rowid, embedding)` pairs into `chunks_vec`, returns count. Use `sqlite_vec.serialize_float32(vec)` for the BLOB encoding.

6. **Add `index()` orchestrator to `src/concord/indexing.py`.** Function signature as in Approach. Two passes, idempotent. Emit `IndexResult`. Match the style of `src/concord/pipeline.py:pull` — no class, dependencies as kwargs, optional `progress` callback.

7. **Add `concord index` to `src/concord/cli.py`.** New `@app.command("index")` with `--db PATH` (required, no default) and `--limit N` (optional). Body: read `OPENAI_API_KEY` from env (exit 2 if missing, matching the existing `pull_command` error pattern), construct `openai.OpenAI()`, construct `SqliteStorage(db_path)`, construct `Chunker()` and `Embedder(client)`, call `index(storage, chunker=chunker, embedder=embedder, limit=limit)`. Print the summary on success.

8. **Tests for `Chunker` in `tests/test_chunking.py`.** Cover:
   - `TestChunker.test_short_text_one_chunk` — text below `chunk_size` returns a single chunk covering the whole text.
   - `TestChunker.test_long_text_multiple_chunks` — known input of N×chunk_size tokens produces ~N chunks.
   - `TestChunker.test_overlap_between_adjacent_chunks` — chunk N and chunk N+1 share `overlap` tokens at the boundary (verify via the token-decoded text, not raw indices).
   - `TestChunker.test_prefers_paragraph_boundaries` — input with `\n\n` boundaries splits there when possible (not mid-paragraph).
   - `TestChunker.test_prefers_sentence_boundaries_when_paragraph_too_big` — single long paragraph splits on `. `.
   - `TestChunker.test_hard_cut_when_no_natural_boundary` — pathological input (one huge word) still splits cleanly at `chunk_size` token boundary.
   - `TestChunker.test_char_offsets_round_trip` — `text[char_start:char_end]` matches `chunk.text` for every chunk.
   - `TestChunker.test_empty_text` — empty input returns `[]`, doesn't raise.

9. **Tests for `Embedder` in `tests/test_embedding.py`.** Use a stub `openai.OpenAI` client (just a `Mock` or a tiny local class) so no real API calls happen:
   - `TestEmbedder.test_batches_inputs` — given 250 texts and batch_size 100, the stub records 3 calls with sizes [100, 100, 50].
   - `TestEmbedder.test_returns_vectors_in_input_order` — verify ordering survives batching.
   - `TestEmbedder.test_uses_configured_model` — assert the model param passed to `embeddings.create`.
   - `TestEmbedder.test_raises_embeddingerror_on_api_failure` — stub raises, embedder surfaces typed error.

10. **Tests for `SqliteStorage`'s Stage 2 additions in `tests/test_storage_sqlite.py`.** Extend the existing file with:
    - `TestSchema.test_chunks_table_exists`
    - `TestSchema.test_chunks_fts_virtual_table_exists`
    - `TestSchema.test_chunks_vec_virtual_table_exists` (skip with `pytest.skip("sqlite-vec not installed")` if the extension isn't available at test time)
    - `TestSchema.test_fts_triggers_fire_on_insert` — insert a chunk, verify `chunks_fts` returns it via `MATCH`.
    - `TestSchema.test_fts_triggers_fire_on_delete` — delete a chunk, verify `chunks_fts` no longer returns it.
    - `TestSchema.test_load_vec_false_skips_vec_table` — construct with `load_vec=False`, assert `chunks_vec` doesn't exist (lets Stage 1 stay extension-free).
    - `TestChunkHelpers.test_proceedings_without_chunks_yields_unindexed` — insert two proceedings, chunk one, assert the helper yields only the other.
    - `TestChunkHelpers.test_bulk_insert_chunks_round_trip` — insert chunks, read back via `chunks_for(granule_id)`, assert equal.
    - `TestChunkHelpers.test_chunks_without_embeddings_yields_unembedded` — insert chunks, embed half, assert helper yields the other half.
    - `TestChunkHelpers.test_bulk_insert_embeddings_round_trip` — insert embeddings, query `chunks_vec` directly, assert vectors decode correctly.

11. **Tests for `index()` orchestrator in `tests/test_indexing.py`.** Use a fake `Embedder` that returns deterministic fake vectors (e.g. `[float(i)] * 1536`) so no network is needed; use a real `Chunker` with default config. Cover:
    - `TestIndex.test_full_pipeline_on_empty_db` — empty proceedings table → no chunks, no embeddings, all zero counts.
    - `TestIndex.test_chunks_then_embeds_new_proceedings` — load a few proceedings (use the helper from Stage 1's test file), run `index`, assert chunks_written > 0, embedded_chunks == chunks_written.
    - `TestIndex.test_idempotent_on_rerun` — run twice, second run has zero new of either kind.
    - `TestIndex.test_resumes_after_partial_chunk` — manually chunk one proceeding by hand, run `index`, assert it skips that one and processes the rest.
    - `TestIndex.test_resumes_after_partial_embed` — chunk everything but embed only half, run `index`, assert second pass embeds the missing half only.
    - `TestIndex.test_limit_caps_chunk_pass` — `limit=2` processes only 2 proceedings worth of chunks.
    - `TestIndex.test_progress_callback_invoked` — supplied callback receives at least one event.

12. **CLI tests in `tests/test_cli.py`.** Add a `TestIndexCommand` class following the patterns from `TestLoadCommand` (Stage 1) and `TestPullCommand` (existing):
    - `test_help_lists_all_flags` — `--db`, `--limit` appear in `--help` (use the existing `_strip` ANSI helper).
    - `test_missing_openai_api_key_exits_cleanly` — unset env, expect exit 2 with `OPENAI_API_KEY` in the error.
    - `test_indexes_proceedings_end_to_end` — set up a tmp DB pre-loaded with a couple of proceedings (use Stage 1's `SqliteStorage` directly to seed), monkeypatch `cli_module.openai.OpenAI` and `cli_module.Embedder` to a fake that returns canned vectors, invoke `concord index`, assert success message and that `chunks_vec` has rows.
    - `test_summary_message_shape` — assert the success message matches the form `Indexed: chunked X new proceedings (Y new chunks, skipped Z already chunked); embedded P new chunks (skipped Q already embedded)`.

13. **Run the full local CI suite and fix anything that breaks.** `uv sync && uv run ruff check && uv run ruff format --check && uv run mypy src && uv run pytest`. All Stage 1 tests must still pass. New tests should bring the total to roughly 160–170.

14. **Smoke test against a real (small) corpus.** With `CONGRESS_API_KEY` and `OPENAI_API_KEY` set:
    ```
    uv run concord pull --from 2026-05-22 --to 2026-05-22 --storage /tmp/concord_demo.jsonl --limit 5
    uv run concord load --jsonl /tmp/concord_demo.jsonl --db /tmp/concord_demo.db
    uv run concord index --db /tmp/concord_demo.db
    ```
    Confirm:
    - `sqlite3 /tmp/concord_demo.db "SELECT COUNT(*) FROM chunks"` returns > 0
    - `sqlite3 /tmp/concord_demo.db "SELECT COUNT(*) FROM chunks_vec"` returns the same number
    - `sqlite3 /tmp/concord_demo.db "SELECT snippet(chunks_fts, 0, '<b>', '</b>', '…', 10) FROM chunks_fts WHERE chunks_fts MATCH 'senate'"` returns a snippet (FTS is working)
    - Re-running `concord index` reports zero new work (idempotency)

## Demo seed data

Not applicable. Concord doesn't have a demo-mode pattern or a `seed.sql` file. The closest equivalent is the JSONL fixture the smoke test in step 14 produces; it's transient, not committed.

## Testing strategy

**Unit tests, new files:**
- `tests/test_chunking.py` — `Chunker` behavior across boundary cases and overlap. No external deps stubbed (uses real `tiktoken`).
- `tests/test_embedding.py` — `Embedder` batching and ordering against a stub `openai.OpenAI`. No network.
- `tests/test_indexing.py` — orchestrator's two-pass idempotency and resume behavior with stub `Embedder` and real `Chunker`.

**Unit tests, extending existing files:**
- `tests/test_storage_sqlite.py` — schema additions, helper methods, FTS triggers, optional `load_vec` parameter.
- `tests/test_cli.py` — new `TestIndexCommand` class.

**Manual / smoke tests:** step 14 (real OpenAI call, ~5 chunks worth = ~$0.0001 of spend).

**Regression risk:**
- Stage 1's `SqliteStorage` constructor changes shape (new `load_vec` parameter). All existing Stage 1 tests must still pass; the parameter has a default, so calls with no kwargs still work.
- Stage 1's `concord load` is untouched.
- Existing scraper tests / `concord pull` are untouched.

## Acceptance criteria

- [ ] `src/concord/chunking.py`, `src/concord/embedding.py`, `src/concord/indexing.py` all exist with module-level docstrings.
- [ ] `Chunker` produces 512-token chunks with 100-token overlap by default; alternate configs work.
- [ ] `Embedder` batches inputs and preserves order across batches.
- [ ] `SqliteStorage` creates `chunks`, `chunks_fts`, the three sync triggers, and `chunks_vec` (when `load_vec=True`) on construction.
- [ ] `load_vec=False` skips `chunks_vec`, allowing Stage 1 to operate without the `sqlite-vec` extension installed.
- [ ] `index()` is idempotent: a second run with no new proceedings returns `IndexResult(0, 0, 0, ..., ...)` with all skipped counts populated.
- [ ] `index()` resumes correctly after partial chunk completion and after partial embed completion.
- [ ] `concord index --help` shows `--db` and `--limit`.
- [ ] Missing `OPENAI_API_KEY` exits 2 with a clean message naming the env var, no traceback.
- [ ] `uv run ruff check` passes.
- [ ] `uv run ruff format --check` passes.
- [ ] `uv run mypy src` passes (strict mode).
- [ ] `uv run pytest` passes — all existing tests plus ~30 new ones.
- [ ] CI is green on the PR.
- [ ] Step 14's smoke test succeeds against real APIs.

## Open questions

- **Q: Should `chunks_vec` be created at `SqliteStorage` construction time, or lazily on first vector insert?** Construction time is simpler — the schema is fully in place, the orchestrator doesn't have to think about it. Cost: requires `sqlite-vec` installed even if the user only ever uses Stage 1. Mitigated by the `load_vec=False` escape hatch. **Default: construction time when `load_vec=True` (the default), with `load_vec=False` as the Stage-1-only escape hatch.** Executor: don't escalate; this is decided.

- **Q: Should `Chunker` produce chunks that align with speaker turns in floor-debate transcripts?** ADR-0005 explicitly defers this to Stage 3. **Default: no. Chunks are speaker-agnostic.** Executor: don't try to be clever; revisit when Stage 3 lands.

- **Q: What `batch_size` for OpenAI calls?** The API accepts up to 2048 inputs per request (with a token-count cap that's effectively the limiter). 100 is comfortably under both limits and balances throughput against retry blast radius. **Default: 100.** Tune later if the smoke-test wall time is annoying.

- **Q: Should the `text` column on `chunks` be indexed (other than via FTS5)?** No — FTS5 handles all text search, and nothing else queries `chunks.text` directly. **Default: no extra index on `chunks.text`.**

- **Q: What happens if a proceeding's text is empty or whitespace-only?** `Chunker` should return `[]`. The orchestrator then writes zero chunks for that proceeding, which means `proceedings_without_chunks` will re-yield it on the next run (since "no chunks" looks like "not yet chunked"). Bug. **Default: in `bulk_insert_chunks`, even when `chunks` is empty, write a sentinel row to a `proceedings_chunked` tracker table — OR rephrase the "needs chunking" query to use `LEFT JOIN ... WHERE chunks.id IS NULL AND last_chunked_at IS NULL`.** Executor: pick one; default to a small `chunking_status` table keyed on `granule_id` with `chunked_at` timestamp if unsure. Worth a 10-minute design call during implementation.

## Out-of-band work

- **Web layer** consumes `chunks`, `chunks_fts`, and `chunks_vec` for hybrid search via RRF (see CONTEXT.md). That work is a separate plan; this stage just makes the indexes available.
- **Stage 3 — Enrich** (entity extraction, knowledge graph) will likely add `entities` and `mentions` tables to the same `proceedings.db`. Stage 2's schema doesn't preclude this — chunks can be referenced from a `mentions` table when it lands.
- **Re-chunk / re-embed UX.** Out of scope here. If/when chunking or embedding model changes, the manual recovery path is `DELETE FROM chunks; DELETE FROM chunks_vec;` then `concord index`. Worth elevating to flags in a future plan once we know which axis we iterate on most.
