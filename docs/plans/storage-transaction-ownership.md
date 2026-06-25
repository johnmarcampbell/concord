# Storage transaction ownership — one committer, pure-SQL projectors

> Make `SqliteStorage`'s single `_maybe_transaction()` context manager the only thing that commits, so every projector function is pure SQL and the `upsert_*` interface stops hiding a "don't call me inside `transaction()`" precondition.

## Source

- Conversation context (no durable issue): an `improve-codebase-architecture` session that flagged the storage seam's inconsistent transaction ownership as a deepening candidate, then grilled the fix to a settled design ("Option B — writes auto-wrap via one owner"). There is no external doc.
- Deferred follow-up (out of scope here): GitHub issue [#149](https://github.com/johnmarcampbell/concord/issues/149) — batching the Stage-1 load loops / choosing per-entity atomicity granularity. This plan is the prerequisite that makes that follow-up safe.

## Context

`concord.storage.sqlite.SqliteStorage` is the facade over the derived SQLite store. It already has a coherent, nested-aware transaction idiom:

- `_maybe_transaction()` ([sqlite.py:648](../../src/concord/storage/sqlite.py)) — opens a `BEGIN`/`COMMIT` only when no caller-owned transaction is active, otherwise yields so the work joins the open one.
- `transaction()` ([sqlite.py:668](../../src/concord/storage/sqlite.py)) + the `_in_tx` flag ([sqlite.py:260](../../src/concord/storage/sqlite.py)) — the caller-owned batching entry point.

Most write methods route through `_maybe_transaction()` (`upsert_vote`, `upsert_vote_positions`, the five `replace_bill_*`, validation failures, enrichment-error set/clear, Bill Brief upsert). **Two entity projectors and the bulk ops opt out** and manage their own transactions at the lower layer:

- `bills.upsert_bill` calls `conn.commit()` itself ([bills.py:301-309](../../src/concord/storage/bills.py)); the facade delegates without wrapping ([sqlite.py:410-412](../../src/concord/storage/sqlite.py)).
- `members.upsert_member` runs its own `BEGIN`/`COMMIT`/`ROLLBACK` ([members.py:106-135](../../src/concord/storage/members.py)); the facade delegates without wrapping ([sqlite.py:386-400](../../src/concord/storage/sqlite.py)).
- `SqliteStorage.write` (Proceeding) ([sqlite.py:285-287](../../src/concord/storage/sqlite.py)), `bulk_insert_chunks` ([sqlite.py:322-338](../../src/concord/storage/sqlite.py)), and `bulk_insert_embeddings` ([sqlite.py:372-382](../../src/concord/storage/sqlite.py)) hand-roll their own `BEGIN`/`COMMIT`/`ROLLBACK` (or a bare `commit()`) inline.

**Why this is friction.** The `upsert_bill(model, *, fetched_at)` / `upsert_member(...)` signatures look identical to the transaction-neutral methods, but carry a hidden precondition not in the signature: *"I manage my own transaction — do not call me inside `transaction()`."* That is a leaky seam, and a latent landmine: `with storage.transaction(): storage.upsert_member(...)` would crash with a `BEGIN`-within-`BEGIN` error, and `upsert_bill` inside `transaction()` would silently `commit()` the outer batch early (a correctness bug that wouldn't even raise). Nobody has hit it only because every caller keeps these outside `transaction()` (verified below).

The fix makes `_maybe_transaction()` the single committer that **every** write method routes through, and turns the projector functions and bulk ops into pure SQL. Their stated guarantees survive unchanged: `_maybe_transaction()` gives a standalone call its own atomic `BEGIN`/`COMMIT`, so a standalone `upsert_member` keeps member+terms atomic and a standalone `upsert_bill` still commits immediately. It is **behaviour-neutral for every current caller** and removes the landmine.

Domain terms: see [CONTEXT.md](../../CONTEXT.md) — **derived store**, **mirror table**, **snapshot** (ADR 0006 latest-snapshot-wins), **Bill section**, **Chunk**. Architecture terms (deep module, seam, leaky seam) follow the `improve-codebase-architecture` skill's vocabulary.

## Goals

1. `_maybe_transaction()` is the only place in `SqliteStorage` that issues a commit. No `*_storage` module function and no facade method self-commits.
2. `bills.upsert_bill` and `members.upsert_member` become pure SQL (no `commit`, no `BEGIN`); the facade methods route them through `_maybe_transaction()`.
3. `write`, `bulk_insert_chunks`, `bulk_insert_embeddings` use `_maybe_transaction()` instead of inline `BEGIN`/`COMMIT`/`ROLLBACK`.
4. `upsert_bill` and `upsert_member` can be safely called inside `transaction()` (no crash, no premature commit, atomic rollback) — proven by a new test.
5. No behaviour change for any existing caller; the full existing test suite passes unchanged.

## Non-goals

1. **Batching the Stage-1 load loops** (wrapping `load_members` / bills tier-1 in `transaction()`) — that changes per-entity atomicity granularity and is tracked in [#149](https://github.com/johnmarcampbell/concord/issues/149). This plan only makes that *possible*; it does not change any loader.
2. **Changing the `Storage` Protocol** ([base.py:15-28](../../src/concord/storage/base.py)). `write`'s signature is unchanged; only `SqliteStorage`'s internal implementation changes. `JsonlStorage.write` is untouched.
3. **Switching `transaction()` to mandatory** (the rejected "Option A" — raise if a write runs outside a transaction). We chose auto-wrap to keep bare one-liners working and minimise call-site churn.
4. **Changing the connection's `isolation_level`** or the WAL/pragma setup ([sqlite.py:262-274](../../src/concord/storage/sqlite.py)). The existing `_maybe_transaction()` already issues explicit `BEGIN` under the current settings and works; we extend that same path, nothing more.

## Relevant prior decisions

- ADR 0006 — Snapshot-on-fetch for mutable entities ([docs/adr/0006-snapshot-on-fetch-for-mutable-entities.md](../adr/0006-snapshot-on-fetch-for-mutable-entities.md)): "latest snapshot wins" upsert semantics — preserved; this plan changes only *who commits*, not *what* is written.
- ADR 0007 — Parallel pipelines per entity ([docs/adr/0007-parallel-pipelines-per-entity.md](../adr/0007-parallel-pipelines-per-entity.md)): the `*_storage` modules are thin per-entity helpers, not a base class. Making them pure-SQL keeps them thin; the facade owns orchestration (transactions). Consistent.
- ADR 0002 / ADR 0003 — JSONL canonical, SQLite derived ([docs/adr/0002-jsonl-as-canonical-raw-store.md](../adr/0002-jsonl-as-canonical-raw-store.md), [docs/adr/0003-sqlite-as-derived-store.md](../adr/0003-sqlite-as-derived-store.md)): SQLite is rebuildable, so this refactor carries no data-migration risk.
- **No new ADR.** This consolidates onto the existing `_maybe_transaction()` idiom rather than introducing a new pattern or contradicting one.

## Relevant files and code

- `src/concord/storage/sqlite.py` — the facade. Change: wrap `upsert_member` ([:386-400](../../src/concord/storage/sqlite.py)) and `upsert_bill` ([:410-412](../../src/concord/storage/sqlite.py)) in `_maybe_transaction()`; replace inline transaction handling in `write` ([:285-287](../../src/concord/storage/sqlite.py)), `bulk_insert_chunks` ([:322-338](../../src/concord/storage/sqlite.py)), `bulk_insert_embeddings` ([:372-382](../../src/concord/storage/sqlite.py)); generalise the `_maybe_transaction` docstring ([:648-654](../../src/concord/storage/sqlite.py)) and the `_in_tx` comment ([:257-259](../../src/concord/storage/sqlite.py)).
- `src/concord/storage/bills.py` — `upsert_bill`: delete `conn.commit()` ([bills.py:309](../../src/concord/storage/bills.py)); fix module docstring ([bills.py:10](../../src/concord/storage/bills.py)) and the function docstring ([bills.py:302-306](../../src/concord/storage/bills.py)).
- `src/concord/storage/members.py` — `upsert_member`: delete the `try/BEGIN/.../COMMIT/except ROLLBACK` wrapper ([members.py:123-135](../../src/concord/storage/members.py)), keeping the three `execute`/`executemany` calls; fix module docstring ([members.py:7](../../src/concord/storage/members.py)) and function docstring ([members.py:118-119](../../src/concord/storage/members.py)).
- Callers — all verified **standalone** (none inside `transaction()`), so behaviour is preserved: `load_members.py:120`, `load_bills.py:108,196`, `load_proceedings.py:174`, `cli/proceedings.py:128`, `index_proceedings.py:96,151`.
- Tests that exercise these methods (must stay green): `tests/test_storage_sqlite.py`, `tests/test_storage_bills_sqlite.py`, `tests/test_storage_members_sqlite.py`, `tests/test_storage_votes_sqlite.py`, `tests/test_web_search.py`, `tests/test_indexing.py`, `tests/test_smoke.py`, `tests/test_pipeline_*`.

## Approach

One owner, pure projectors. `_maybe_transaction()` is already nested-aware, so routing the holdouts through it is a faithful swap:

- `members.upsert_member` currently does `BEGIN; UPSERT member; DELETE terms; INSERT terms; COMMIT`. After: the three statements only; the facade's `with self._maybe_transaction():` supplies the atomic boundary. Standalone → its own `BEGIN`/`COMMIT` (member+terms still atomic). Inside `transaction()` → joins the batch (atomic with the rest).
- `bills.upsert_bill` currently does `UPSERT; commit()`. After: the `UPSERT` only; the facade wrapper commits when standalone, or defers to the batch.
- `write` / `bulk_insert_chunks` / `bulk_insert_embeddings` currently hand-roll `BEGIN`/`COMMIT`/`ROLLBACK`. After: `with self._maybe_transaction():` around the same `execute`/`executemany` calls (keeping the empty-input guards and return values). `_maybe_transaction()` already does `ROLLBACK` on exception, so the explicit try/except is redundant.

Why behaviour-neutral: every call site today is standalone (grep above), and a standalone `_maybe_transaction()` call opens exactly one `BEGIN`/`COMMIT` — identical to the durability/atomicity the inline code provided. The only *new* capability is that the two outliers may now also be called inside `transaction()` safely; nothing forces them to be.

Note on `isolation_level`: the connection is opened with `sqlite3.connect(...)` default settings and `_maybe_transaction()` already issues an explicit `BEGIN` that works for `upsert_vote` and the `replace_bill_*` writers under those settings. Routing more methods through the same helper introduces no new interaction; the existing storage tests are the guard.

## Step-by-step plan

1. **Make `bills.upsert_bill` pure SQL.** In [storage/bills.py](../../src/concord/storage/bills.py), delete `conn.commit()` ([:309](../../src/concord/storage/bills.py)) so the function is just the `conn.execute(_BILL_UPSERT_SQL, ...)`. Update its docstring ([:302-306](../../src/concord/storage/bills.py)) to drop "and commit" / "owns its own commit" and state it is pure SQL committed by the caller/facade. Update the module docstring line ([:10](../../src/concord/storage/bills.py)).

2. **Make `members.upsert_member` pure SQL.** In [storage/members.py](../../src/concord/storage/members.py), remove the `try: conn.execute("BEGIN") ... conn.execute("COMMIT") except Exception: conn.execute("ROLLBACK"); raise` scaffolding ([:123-135](../../src/concord/storage/members.py)), leaving the member UPSERT, the `DELETE FROM member_terms`, and the conditional `executemany` of term rows. Update the function docstring ([:118-119](../../src/concord/storage/members.py)) to say atomicity is provided by the caller/facade transaction, and the module docstring ([:7](../../src/concord/storage/members.py)).

3. **Wrap the two facade delegations.** In [storage/sqlite.py](../../src/concord/storage/sqlite.py), change `upsert_member` ([:400](../../src/concord/storage/sqlite.py)) and `upsert_bill` ([:412](../../src/concord/storage/sqlite.py)) to wrap their delegation in `with self._maybe_transaction():`, matching the `upsert_vote` pattern ([:591](../../src/concord/storage/sqlite.py)).

4. **Route the bulk ops through `_maybe_transaction()`.** Replace the inline `BEGIN`/`COMMIT`/`ROLLBACK` in `write` ([:285-287](../../src/concord/storage/sqlite.py)), `bulk_insert_chunks` ([:322-338](../../src/concord/storage/sqlite.py)), and `bulk_insert_embeddings` ([:372-382](../../src/concord/storage/sqlite.py)) with `with self._maybe_transaction():` around the existing `execute`/`executemany` calls. Keep the `if not rows: return 0` guard in `bulk_insert_embeddings` and the return values (`len(chunks)` / `len(rows)`) outside/after the block.

5. **Generalise the helper's documentation.** Update `_maybe_transaction()`'s docstring ([:649-654](../../src/concord/storage/sqlite.py)) and the `_in_tx` comment ([:257-259](../../src/concord/storage/sqlite.py)) so they describe the helper as the single committer for **all** write methods, not just "the per-section `replace_bill_*` writers."

6. **Add a contract test.** In `tests/test_storage_sqlite.py` (or `tests/test_storage_members_sqlite.py` / `_bills_sqlite.py`), add a test that exercises the previously-broken path: open `with storage.transaction():`, call `storage.upsert_member(...)` and `storage.upsert_bill(...)` inside it, and assert (a) no exception, (b) both rows are present after commit. Add a rollback case: inside `with pytest.raises(...): with storage.transaction(): upsert_member(...); raise RuntimeError(...)`, then assert the member+terms (and bill) did **not** land — proving the writes join the caller's atomic batch. These tests would have failed before this change.

7. **Run the gate.** `uv run ruff format`, `uv run ruff check`, `uv run mypy src`, `uv run pytest`. Confirm the storage, pipeline, web, and smoke suites are green with no behavioural edits to existing assertions.

## Demo seed data

No seed changes — this is a pure refactor of transaction plumbing in the derived store, with no new tables, columns, entity types, or persisted state.

## Testing strategy

- **Regression guard (must pass unchanged):** the existing storage suites (`tests/test_storage_sqlite.py`, `tests/test_storage_bills_sqlite.py`, `tests/test_storage_members_sqlite.py`, `tests/test_storage_votes_sqlite.py`), the indexing/chunking suites (`tests/test_indexing.py`, `tests/test_web_search.py` — which drive `write`/`bulk_insert_*`), and `tests/test_pipeline_*` / `tests/test_smoke.py`. Green-with-no-edits is the proof of behaviour-neutrality for standalone callers.
- **New contract test (Step 6):** `upsert_bill` + `upsert_member` inside `transaction()` — success path lands both rows; failure path rolls the whole batch back. This is the test that asserts the seam no longer lies.
- **Regression risk:** the bulk-op return values (`bulk_insert_chunks` → chunk count, `bulk_insert_embeddings` → row count, with the empty-input short-circuit) must be preserved — `tests/test_indexing.py` covers these. WAL/FTS triggers fire on the same INSERTs regardless of who owns the transaction.
- **Manual check (optional):** `uv run concord run members --congresses 119 --limit 5` and `uv run concord run proceedings --from 2026-05-22 --to 2026-05-22` against a real DB; confirm rows persist (durability of standalone writes is preserved).

## Acceptance criteria

- [ ] No `*_storage` module function and no `SqliteStorage` method calls `conn.commit()` or issues a bare `BEGIN`/`COMMIT` outside `_maybe_transaction()` / `transaction()` (grep `commit\|BEGIN` over `src/concord/storage/` shows only the two context managers and the schema triggers).
- [ ] `bills.upsert_bill` and `members.upsert_member` are pure SQL; their facade methods wrap in `_maybe_transaction()`.
- [ ] `write` / `bulk_insert_chunks` / `bulk_insert_embeddings` use `_maybe_transaction()`; return values and empty-input guards preserved.
- [ ] New test: `upsert_bill`/`upsert_member` inside `transaction()` succeed and roll back atomically on error.
- [ ] `_maybe_transaction()` docstring + `_in_tx` comment describe it as the single committer for all writes.
- [ ] `uv run ruff check`, `uv run ruff format --check`, `uv run mypy src` clean; `uv run pytest` green with no edits to existing assertions.

## Open questions

None — all design decisions resolved during grilling. (Auto-wrap vs mandatory-transaction settled as auto-wrap; scope settled as the full write surface; loader batching explicitly deferred to [#149](https://github.com/johnmarcampbell/concord/issues/149).)

## Out-of-band work

- [#149](https://github.com/johnmarcampbell/concord/issues/149) becomes actionable once this merges: with commit ownership uniform, each Stage-1 loader can deliberately choose its atomicity granularity (one `transaction()` around the load loop vs per-record).
