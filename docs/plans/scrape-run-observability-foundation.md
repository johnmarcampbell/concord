# Scrape-run observability — foundation + api.congress.gov slice (PR 1 of 2)

> Build the Scrape Run ledger end-to-end for the api.congress.gov path: a new `observability.py` (run_id + recorder contextvars, the route-table endpoint normalizer, the `Recorder`, and the `scrape_run(...)` context manager), central run_id-stamped logging, the `runs`/`run_events` record tables with a schema-version bump, and full wiring through `api.py` and the **Bills** scraper as a proven vertical slice.

## Source

- Conversation context (no GitHub issue predates this): a grilling session that resolved the full design. The decisions are recorded durably in **ADR 0021 — Scrape-run observability** ([docs/adr/0021-scrape-run-observability.md](../adr/0021-scrape-run-observability.md), new, created with this plan) and the **Observability** section of [CONTEXT.md](../../CONTEXT.md) (new terms: Scrape Run, Run Event, Endpoint bucket). This plan is PR 1 of 2; PR 2 is [scrape-run-observability-fanout.md](scrape-run-observability-fanout.md).

## Context

Concord scrapes had no durable operational record. Per-module loggers exist (`logging.getLogger("concord.*")`) but **nothing configures the root logger** anywhere in `src/concord/`, so log lines fall through to defaults; and `PullResult`'s `written/skipped/failed` counts are printed to stderr then lost. We want to track each Stage-0 scrape: which endpoints it called successfully (counts), which requests errored (detail), and whether each error resolved on retry — with a `/runs` dashboard as a later, separate effort.

A **Scrape Run** is the record of one Stage-0 execution for one entity (a `scrape <entity>` pull, or the Stage-0 phase of `run <entity>`). A **Run Event** is the per-request error detail. An **Endpoint bucket** is the normalized aggregation key. All three terms are defined in [CONTEXT.md](../../CONTEXT.md); read that section first. Read ADR 0021 in full — it records every design decision and the rationale (including why Prometheus was rejected, why 429s count as errors, and why the ledger is DB-authoritative with a JSONL backup).

This PR proves the whole architecture on **one client (`api.py`) and one scraper (Bills)** before PR 2 fans out to the other two clients and three scrapers. Bills is chosen because it is the most endpoint-rich path (list + detail + five sub-endpoints), so it exercises the route table hardest.

## Goals

1. A `concord/observability.py` module: `run_id` and `recorder` contextvars; a `Recorder` that accumulates per-bucket success counts and per-request Run Events (with capped attempts + `resolved`/`failed`); a route-table endpoint normalizer with a loud `<source>:unmatched` fallback; and a `scrape_run(...)` context manager that mints the run_id, sets/resets both contextvars, and flushes to SQLite + appends `runs.jsonl` on exit.
2. Central logging configured once (run_id-stamped **text**, no JSON), installed at the CLI callback and `serve` startup, so every existing `concord.*` log line carries its run_id.
3. `runs` and `run_events` **record** tables (ADR 0019 semantics) created via a new `_m003` migration with a `_HEAD` bump (ADR 0017), and declared identically in `_BASE_SCHEMA`.
4. `api.py:_get` reports successes (by bucket) and per-request error outcomes (with retry resolution; 429s counted as errors) into the active recorder.
5. The Bills Stage-0 path (`_run_scrape_bills` in `cli/bills.py`, used by both `scrape bills` and `run bills`) wrapped in `scrape_run(...)`, producing a complete Scrape Run + Run Events on every bills scrape.

## Non-goals

1. **`text.py` and `senate_xml.py` instrumentation** — PR 2. This PR leaves those two clients uninstrumented; a Proceedings or Senate-vote scrape will record a Scrape Run with only the api.congress.gov portion (or, for Proceedings text fetches, nothing) until PR 2.
2. **Proceedings, Members, Votes scraper wiring** — PR 2. Only Bills is wired here.
3. **The `/runs` web dashboard** — a later, separate effort. This PR creates the tables it will read but adds no web route.
4. **JSON log output / external log aggregation** — explicitly rejected in ADR 0021.
5. **Changing any retry *behavior*.** The recording is additive; backoff, retry counts, and the 429 "wait forever" policy are untouched.

## Relevant prior decisions

- **ADR 0021 — Scrape-run observability** ([docs/adr/0021-scrape-run-observability.md](../adr/0021-scrape-run-observability.md)) — *(new, created with this plan)*. The governing decision record.
- **ADR 0002 — JSONL as canonical raw store** ([docs/adr/0002-jsonl-as-canonical-raw-store.md](../adr/0002-jsonl-as-canonical-raw-store.md)) — the ledger deliberately inverts "JSONL is source of truth" for the runs stream (DB-authoritative).
- **ADR 0007 — Parallel pipelines per entity** ([docs/adr/0007-parallel-pipelines-per-entity.md](../adr/0007-parallel-pipelines-per-entity.md)) — `observability.py` is a thin shared helper, **not** a base class; do not introduce one.
- **ADR 0012 — Web bootstraps empty schema on startup** ([docs/adr/0012-web-bootstraps-empty-schema-on-startup.md](../adr/0012-web-bootstraps-empty-schema-on-startup.md)) — precedent for `scrape_run` calling `ensure_schema` to bootstrap the ledger DB.
- **ADR 0017 — SQLite schema versioning** ([docs/adr/0017-sqlite-schema-versioning.md](../adr/0017-sqlite-schema-versioning.md)) — add a new `_MIGRATIONS` entry + bump `_HEAD`; never edit a landed migration; DDL must match `_BASE_SCHEMA`.
- **ADR 0019 — Mirror tables vs record tables** ([docs/adr/0019-mirror-tables-vs-record-tables.md](../adr/0019-mirror-tables-vs-record-tables.md)) — `runs`/`run_events` are record tables, like `bill_briefs`; an entity re-derivation must not touch them.

## Relevant files and code

- `src/concord/observability.py` — **new.** Home of the contextvars, `Recorder`, route table + normalizer, and `scrape_run(...)`.
- `src/concord/api.py:506` — `Client._get`, the single api.congress.gov chokepoint; its retry loop at `:511`. 429 handling at `:531`, 5xx at `:539`, transport at `:515`. Successes return at `:569`.
- `src/concord/storage/sqlite.py:73` — `_BASE_SCHEMA` (add `runs`/`run_events` DDL here too). `:362` `bill_briefs` DDL is the record-table DDL template. `:666` `_m002_add_bill_briefs` is the migration template. `:701` `_MIGRATIONS` tuple. `:704` `_HEAD`. `:584` the `bill_briefs` column-tuple + UPSERT pattern (S608 waiver) to mirror for ledger inserts.
- `src/concord/cli/__init__.py:65` — `_root()` callback; install central logging here.
- `src/concord/cli/serve.py` — `serve_command`; install central logging at the top (before `create_app`).
- `src/concord/cli/bills.py` — `_run_scrape_bills` (the shared Stage-0 helper called by `scrape_bills_command` and `run_bills_command:518`); wrap its body in `scrape_run(...)`.
- `src/concord/scraper/bills.py:198` — `scrape_basic`; the api calls flow through the injected `Client`, so no change is needed here once `api.py` is instrumented.
- `src/concord/cli/_common.py:23` — `DEFAULT_DB = Path("./data/proceedings.db")`, the default ledger DB path.
- `tests/` — existing client tests use `httpx.MockTransport` (see `Client(transport=...)`); follow that for chokepoint tests.

## Approach

**Two contextvars in one module.** `observability.py` declares `_run_id: ContextVar[str | None]` and `_recorder: ContextVar[Recorder | None]`, plus `active_recorder()` / `current_run_id()` readers. The `Recorder` is a plain object (no inheritance) holding: `entity`, `command`, `started_at`, a `dict[bucket -> int]` of successes, and a `list[RunEvent]`. Its API is `note_success(source, path)` and `note_request_outcome(source, path, attempts, resolved)`; both normalize `path -> bucket` via the route table. The route table is an ordered `tuple[(regex, template)]` per source; `normalize(source, path)` returns the first match or `f"{source}:unmatched"`, and on the unmatched branch records the concrete path into a deduped, capped sample (e.g. ≤20 distinct) and emits a run_id-stamped `WARNING`.

**`scrape_run(...)` owns the lifecycle.** A `@contextmanager` that takes `entity`, `command`, a data dir (for `runs.jsonl`, default the parent of `DEFAULT_DB`), and `db_path` (default `DEFAULT_DB`). On enter: mint a run_id (sortable, e.g. `f"{started_at:%Y%m%dT%H%M%S}-{token}"` — derive the token from the start timestamp + entity, **not** `random`/`uuid4` if that complicates determinism in tests; a short hash of `(started_at, entity, pid)` is fine), `ensure_schema(db_path)`, set both contextvars, yield the recorder. On exit (`finally`): reset both contextvars, then flush — INSERT one `runs` row + N `run_events` rows (DB authoritative), then append one JSON line to `runs.jsonl` (cold backup). Flush must be best-effort and must **not** mask an in-flight exception from the scrape body.

**Chokepoint instrumentation is additive and tiny.** In `api.py:_get`, accumulate a local `attempts: list[Attempt]` inside the retry loop (one entry per non-success response/transport failure, including 429s — see ADR 0021). On the success return path, call `note_success` and, if `attempts` is non-empty, `note_request_outcome(..., resolved=True)`. On the terminal-raise paths (transport give-up, 5xx give-up, non-retryable 4xx), call `note_request_outcome(..., resolved=False)` before raising. Guard every call with `rec = active_recorder(); if rec is not None:` so the client is a no-op when no scrape is active (tests, web).

**Logging install is idempotent and import-safe.** A `configure_logging()` in `observability.py` attaches a single `StreamHandler(sys.stderr)` with a `RunIdFormatter(logging.Formatter)` whose `format()` sets `record.run_id = current_run_id() or "-"` before delegating to `super().format()`. Called from `_root()` and `serve_command` only — never at import (ADR 0014). Make it idempotent (skip if our handler is already attached) so repeated CLI invocations in one process (tests) don't stack handlers.

```mermaid
sequenceDiagram
    participant CLI as cli/bills.py _run_scrape_bills
    participant SR as scrape_run() (observability)
    participant API as api.py _get
    participant DB as proceedings.db (runs/run_events)
    CLI->>SR: with scrape_run(entity="bills", command=...)
    SR->>SR: ensure_schema(db); set run_id + recorder contextvars
    CLI->>API: scrape_basic(client=Client(...))
    API->>SR: active_recorder().note_success / note_request_outcome
    CLI-->>SR: body returns (or raises)
    SR->>DB: INSERT runs + run_events
    SR->>SR: append runs.jsonl; reset contextvars
```

## Step-by-step plan

1. **Create `src/concord/observability.py` with the contextvars and readers.** Declare `_run_id` and `_recorder` `ContextVar`s (default `None`), plus `active_recorder()`, `current_run_id()`. No other logic yet. Verify `uv run mypy src` passes (module imports cleanly, fully type-hinted).

2. **Add the route table + `normalize(source, path)`.** Ordered `tuple` of `(compiled_regex, template)` for `source="api"` covering: `daily-congressional-record` list + `/articles`, `member/congress/{c}` list, `bill/{c}/{t}` list + `bill/{c}/{t}/{n}` detail + `bill/{c}/{t}/{n}/{sub}` sub-endpoints, `house-vote/{c}/{s}` list + `/{roll}` detail + `/{roll}/members`. First match wins; no match returns `"api:unmatched"`. Unit-test every api.congress.gov path shape used in `api.py` maps to a stable bucket, and that an unknown path yields `api:unmatched`.

3. **Add the `Recorder` class.** Plain class (no base class). State: `entity`, `command`, `started_at`, `successes: dict[str, int]`, `events: list[RunEvent]`, `unmatched_paths: set[str]` (capped). Methods `note_success(source, path)` and `note_request_outcome(source, path, attempts, resolved)`. Define small dataclasses/`NamedTuple`s `Attempt` (n, status|None, transport_class|None, message) and `RunEvent` (bucket, attempts capped + overflow_count, final_status). Unit-test counting, capping, and `resolved`/`failed` classification.

4. **Add `configure_logging()` + `RunIdFormatter`.** Idempotent stderr handler; formatter stamps `record.run_id` from `current_run_id()`. Unit-test that a log emitted inside a `_run_id.set(...)` block carries the id and one outside carries `-`, and that calling `configure_logging()` twice attaches only one handler.

5. **Add the `runs`/`run_events` DDL to `_BASE_SCHEMA`** in `src/concord/storage/sqlite.py:73`. `runs`: `run_id TEXT PRIMARY KEY`, `entity TEXT NOT NULL`, `command TEXT NOT NULL`, `started_at TEXT NOT NULL`, `ended_at TEXT`, `status TEXT NOT NULL` (`ok`/`error`/`partial`), `success_counts TEXT NOT NULL` (JSON object bucket→count), `throttle_counts TEXT` (JSON, reserved), `unmatched_sample TEXT`, `error_event_count INTEGER NOT NULL DEFAULT 0`. `run_events`: `run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE`, `seq INTEGER NOT NULL`, `endpoint_bucket TEXT NOT NULL`, `attempts TEXT NOT NULL` (JSON array), `overflow_count INTEGER NOT NULL DEFAULT 0`, `final_status TEXT NOT NULL`, `ts TEXT NOT NULL`, `PRIMARY KEY (run_id, seq)`. Add a comment block flagging both as **record tables (ADR 0019)**, mirroring the `bill_briefs` comment at `:352`.

6. **Add migration `_m003_add_runs_tables`** following `_m002` (`:666`): `CREATE TABLE IF NOT EXISTS` for both tables, DDL **byte-equivalent** to step 5. Append `(3, _m003_add_runs_tables)` to `_MIGRATIONS` (`:701`); `_HEAD` updates to 3 automatically. Verify `uv run pytest` schema-equivalence test (ADR 0017) passes — `_BASE_SCHEMA` and the migration must agree.

7. **Add ledger persistence helpers in `sqlite.py`.** Mirror the `bill_briefs` column-tuple + parameterized INSERT pattern at `:584` (the S608 waiver covers this module). Add `insert_run(conn, run_row)` and `insert_run_events(conn, run_id, events)` on `SqliteStorage` (or module-level helpers it delegates to). Serialize JSON columns with `json.dumps`. Unit-test a round-trip insert + read-back.

8. **Implement `scrape_run(...)` context manager** in `observability.py`. Signature `scrape_run(*, entity: str, command: str, db_path: Path = DEFAULT_DB, data_dir: Path | None = None)`. On enter: compute `started_at` (UTC), mint run_id, `ensure_schema(db_path)`, construct `Recorder`, set both contextvars, `yield recorder`. In `finally`: reset contextvars; open a `SqliteStorage(db_path)` connection and write the `runs` row + `run_events`; append one line to `<data_dir or db_path.parent>/runs.jsonl`. Determine `status` from whether the body raised and whether any events are `failed`. Flush errors must be logged, not raised (don't mask the body's exception). Unit-test: a fake body that makes recorder calls yields a persisted run + events; a body that raises still flushes a `status="error"` run.

9. **Instrument `api.py:_get`.** Add a module-level `_normalize`-free hook: import `active_recorder` from `concord.observability` (lazy import inside `_get` if needed to avoid any import cycle — `observability` imports `DEFAULT_DB` from `cli/_common`, so verify direction; if a cycle appears, move `DEFAULT_DB` or pass it in). Accumulate `attempts` across the loop; on success `return data` path call `note_success` + (if attempts) `note_request_outcome(resolved=True)`; on each terminal `raise ApiError` path call `note_request_outcome(resolved=False)` first. Guard with `if rec is not None`. Add a test using `httpx.MockTransport` that: (a) a clean request bumps the right bucket and emits no event; (b) a 503-then-200 emits one `resolved` event; (c) a 503×6 terminal failure emits one `failed` event; (d) a 429-then-200 emits one `resolved` event whose attempts include the 429.

10. **Wrap the Bills Stage-0 helper.** In `src/concord/cli/bills.py`, wrap the body of `_run_scrape_bills` in `with scrape_run(entity="bills", command=<"scrape bills"|"run bills">, db_path=...):`. `scrape bills` has no `--db` today — add a `--db` option defaulting to `DEFAULT_DB` (ledger destination only; document that Stage 0 still writes entity data only to JSONL, the DB write is telemetry per ADR 0021), and pass `run bills`'s existing `db_path` through. Verify both `concord scrape bills --limit 1` and `concord run bills --limit 1` produce a `runs` row + any `run_events` (use a real or recorded API key; or a transport-mocked integration test).

11. **Install logging at the entrypoints.** Call `configure_logging()` at the top of `_root()` in `cli/__init__.py:65` and at the top of `serve_command` in `cli/serve.py`. Verify `concord scrape bills` stderr lines now show the `[run_id]` prefix and `concord --help` still imports nothing heavy (no behavior/timing regression).

12. **Update `CONTEXT.md` cross-references if needed.** The three Observability terms already exist; confirm they still read correctly against the shipped column names (adjust wording only, not new terms).

## Demo seed data

Not applicable. This repo has no `backend/demo/seed.sql`; demo/data is produced by running the pipeline against the live API. The ledger populates itself the first time any instrumented scrape runs; no fixture rows are needed. (The `/runs` dashboard PR, when written, may add representative rows — out of scope here.)

## Testing strategy

- **Unit (`tests/`):**
  - `test_observability_routes.py` — every api.congress.gov path shape → expected bucket; unknown path → `api:unmatched` + sample captured.
  - `test_observability_recorder.py` — success counting, attempt capping + overflow, `resolved`/`failed` classification.
  - `test_observability_logging.py` — run_id stamping inside/outside a run; idempotent handler install.
  - `test_observability_scrape_run.py` — enter/exit persists `runs` + `run_events`; exception path still flushes with `status="error"`; `runs.jsonl` line appended.
  - `test_api_recording.py` — the four `httpx.MockTransport` scenarios in step 9.
  - `test_sqlite_runs.py` — insert/read round-trip; schema-equivalence test still passes with `_HEAD = 3`.
- **Integration:** `concord scrape bills --limit 1 --db <tmp>` (transport-mocked or live) writes exactly one `runs` row with non-empty `success_counts`.
- **Regression risk:** all existing `tests/` must pass unchanged — especially `api.py` client tests (the chokepoint edits must not alter retry behavior) and the `sqlite.py` schema-equivalence / migration tests. `uv run mypy src` must stay clean.

## Acceptance criteria

- [ ] `concord/observability.py` exists with the two contextvars, route table, `Recorder`, `configure_logging`, and `scrape_run`.
- [ ] `runs` + `run_events` exist as record tables; `_HEAD == 3`; schema-equivalence test passes.
- [ ] `concord scrape bills` and `concord run bills` each persist a Scrape Run (DB row + `runs.jsonl` line) with per-bucket success counts and any Run Events.
- [ ] A 503-then-success records a `resolved` event; a terminal failure records a `failed` event; a 429-then-success records a `resolved` event whose attempts include the 429.
- [ ] Existing `concord.*` log lines carry a `[run_id]` prefix during a scrape and `-` otherwise.
- [ ] `api.py` retry behavior is unchanged (existing api tests pass verbatim).
- [ ] `uv run ruff check`, `uv run ruff format --check`, `uv run mypy src`, `uv run pytest` all pass.

## Open questions

- **Run-id minting under test determinism.** `Math.random`/`uuid4` are fine in production but can complicate deterministic tests. Default: derive a short token from `(started_at, entity, pid)`; if collisions are a concern in tight loops, the executor may add a process-local counter. Pick the simplest thing that keeps `run_id` unique and tests stable — don't block.
- **Import direction `observability` ↔ `cli/_common`.** `scrape_run` wants `DEFAULT_DB` from `cli/_common`, but `cli` imports lots. If a cycle appears, the executor should move `DEFAULT_DB` to a leaf module (or have `scrape_run` require `db_path` explicitly with the default resolved at the CLI call site). Decide during implementation; prefer requiring `db_path` at the call site if in doubt.
- **`scrape <entity>` gaining `--db`.** Adds a flag to a previously DB-free command. ADR 0021 sanctions it as telemetry, but if the maintainer prefers, the ledger DB could instead be derived from `--storage-dir`'s parent with no new flag. Default: add `--db` for explicitness; flag for review.

## Out-of-band work

- **PR 2** ([scrape-run-observability-fanout.md](scrape-run-observability-fanout.md)) is **blocked by this PR** and will reuse `Recorder`/`scrape_run`/the route table verbatim — keep their public shapes stable, as PR 2's plan was written against them and may need revision once this PR's concrete API lands.
- The `/runs` dashboard is a deferred, unplanned follow-up.
