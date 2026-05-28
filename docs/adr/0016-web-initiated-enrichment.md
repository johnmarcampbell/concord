# 0016 — Web layer may invoke Stage 0 enrichment on demand

**Status**: Accepted, 2026-05-27.

## Context

Concord's pipeline framing per [ADR 0007](0007-parallel-pipelines-per-entity.md) is that **Stage 0 is the scraper** — a CLI-driven module that walks api.congress.gov and appends ADR 0006 snapshot envelopes to JSONL. The web layer is a pure reader of the SQLite derived store: it does not import `concord.api.Client`, does not read `CONGRESS_API_KEY`, has no background-task scaffolding, and cannot write to the canonical raw store.

Phase 2b ([`docs/plans/phase-2b-bills-enrichment.md`](../plans/phase-2b-bills-enrichment.md)) built tier-2 Bill enrichment (cosponsors, actions, subjects, titles, summaries — one JSONL per sub-endpoint per [ADR 0009](0009-multi-endpoint-entities-split-jsonl.md)) but rendered "not yet fetched — run `concord scrape bills enrich --bill-ids …`" placeholders on the Bill profile page for any section without a fetched_at timestamp. That CLI-invocation hint is the kind of thing a casual reader of the public demo can't act on; the natural UX is a button.

The deferral note in phase-2b explicitly said this required "a background-task framework that doesn't exist in the codebase." That framework is what this ADR adds — and answers the framing question: is the web process allowed to be a writer to the JSONL canonical store?

## Decision

**Yes**, behind two explicit gates. The web layer may invoke `scrape_enrichment` → `load_bills.load_one` → `index_bills.reindex_one` for a single Bill in response to a `POST /bills/{c}/{t}/{n}/enrichment` click, dispatched via FastAPI's built-in `BackgroundTasks`, conditional on two env vars:

- `CONGRESS_API_KEY` — must be present (otherwise the underlying `concord.api.Client` can't make calls).
- `CONCORD_ENABLE_WEB_ENRICHMENT` — must be truthy (`1`, `true`, `yes`, `on`). Defense in depth: a casual deployment without the opt-in flag cannot be coaxed into triggering Stage 0 from the web layer even by hand-crafting the POST URL.

Both must be true for the button to render and for the two new routes to be registered. When either is missing, the routes return 404 (not 403); the existing per-section CLI-hint placeholders remain visible as the documentation of how to do the work via the pipeline.

Cross-request de-duplication of in-flight enrichments uses an in-process `set[str]` of `bill_id`s plus an `asyncio.Lock`, stashed on `app.state.enrichment_in_flight` / `app.state.enrichment_lock`. A re-POST while an enrichment is in flight returns the same in-flight status fragment without enqueuing a second task.

Per-bill state machine surfaced as four HTMX fragments — `_enrichment_button`, `_enrichment_in_flight`, `_enrichment_done`, `_enrichment_failed` — driven by a 3-second polling `GET /bills/{c}/{t}/{n}/enrichment-status` endpoint. Failures are captured on a new `bills.last_enrichment_error TEXT NULL` column, cleared at the start of each attempt and written on any exception path. The state machine's `Done` is the strict conjunction "all five `*_fetched_at` populated AND no error"; partial-success (some sections populated, others NULL) is treated as "not done yet" and falls back to the button so a re-click retries the still-NULL sections. The scraper's `EnrichStats.section_failures` count (per-section exceptions it swallowed) is also written to `last_enrichment_error` so a 0-of-5 or 3-of-5 run doesn't masquerade as success.

Both the `POST .../enrichment` and `GET .../enrichment-status` endpoints require a local `bills` row for the requested `(congress, bill_type, bill_number)` — a hand-crafted request for an unknown bill returns 404 rather than enqueueing 5 sub-endpoint calls upstream that the loader would no-op on for lack of a parent row.

The `bills.last_enrichment_error` column is added via an idempotent `ALTER TABLE … ADD COLUMN` inside `ensure_schema` (gated by a `PRAGMA table_info` check) so pre-existing SQLite files from before this release pick up the column on next boot. New column adds in the future follow the same pattern — declare in `_POST_RELEASE_COLUMNS` and the bootstrap path handles both fresh and existing DBs.

The bulk `load_bills.load(...)` and `index_bills.index(...)` paths are factored to share their per-bill body with new `load_one(*, storage_dir, db_path, bill_id)` and `reindex_one(*, db_path, bill_id)` helpers. The request-side projection and FTS update are O(1) per bill rather than O(N).

## Consequences

**Trade-offs accepted:**

- **The web process becomes a writer to the JSONL canonical store.** This is an expansion of [ADR 0007](0007-parallel-pipelines-per-entity.md)'s "Stage 0 is the scraper" framing, not a violation of it. The CLI scraper remains the primary writer and the system of record for bulk runs; the web layer adds a per-click, per-bill writer scoped to one Bill's five sub-endpoints. Both paths use the same ADR 0006 snapshot envelope shape and the same `scrape_enrichment` function, so the data they write is indistinguishable.
- **`BackgroundTasks` is not a real queue.** A crash mid-job loses the in-process in-flight set; the user re-clicks; `scrape_enrichment` re-runs all five sections; the JSONL gains up to 5 duplicate envelopes; the loader's natural-key dedup ([ADR 0002](0002-jsonl-as-canonical-raw-store.md)) absorbs them; end state is correct. Long-running enrichments (a heavily-cosponsored bill with paginated actions can take 20+ seconds) hold a threadpool slot but don't block the event loop because the synchronous scraper/loader/indexer is run via FastAPI's standard threadpool dispatch for sync `add_task` callbacks.
- **The in-process lock is correct for `uvicorn --workers 1` only.** Multi-worker uvicorn would let two workers each accept a POST for the same bill and run enrichment twice. Acceptable today because `concord serve` doesn't expose a `--workers` flag and the demo deployment is single-process. Revisit trigger: introduction of `--workers N > 1`.
- **No automatic retries.** A failed enrichment surfaces as the `_enrichment_failed` fragment with a "Try again" button. The user re-clicks. Simpler than building a retry budget, and cooperates naturally with the future per-IP rate limiter.

**Things this buys:**

- **Demo visitors can populate a Bill they care about without shell access.** The CLI-hint placeholders that used to read "run `concord scrape bills enrich --bill-ids …` to populate" become an actionable button for opt-in deployments.
- **Per-bill load and reindex helpers.** `load_one` and `reindex_one` are useful primitives beyond this feature — anything that wants to re-project one Bill (e.g. a future "the API said this bill changed; refresh just it" hook) calls the same code.
- **A named chokepoint for the follow-up rate-limit plan.** The `POST /bills/{c}/{t}/{n}/enrichment` handler is the single point a `@limiter.limit(...)` decorator can be added to, and the in-flight check inside the handler body sits *after* any future decorator so a 429 returns before any state mutation.

**What stays open:**

- **Rate limiting.** Deferred to a separate plan ([`docs/plans/enrichment-rate-limiting.md`](../plans/enrichment-rate-limiting.md)) and a separate ADR. Without rate limiting, an adversarial visitor walking the `/bills` index could drain the `CONGRESS_API_KEY` quota; the `CONCORD_ENABLE_WEB_ENRICHMENT` kill switch is the v1 mitigation (operators leave it off until the rate-limit plan lands).
- **Multi-worker uvicorn correctness.** Per the trade-off above. Migration path is a SQLite-backed `enrichment_jobs` table replacing the in-process set; not built until the deployment shape demands it.
- **Per-section buttons.** Considered and rejected — the cost asymmetry across the five sections (summaries dominates) is something an operator handles via the CLI's `--sections` flag, not five UI buttons. One button enriches all five sections; partial success (3 of 5) reports as Done because the per-section placeholders for the still-NULL sections naturally invite a re-click.
- **Bills embeddings (Phase 5).** `reindex_one` mirrors the FTS-only scope of today's `index_bills`. When embeddings for Bills land, this helper grows a sibling chunking + embedding pass.

## Rejected: a real task queue (Celery / RQ / arq / Dramatiq)

A real queue would give us durable jobs, multi-worker safety, retries, observable backlogs, and cross-process de-dup for free — at the cost of a Redis (or equivalent) dependency, a separate worker process, and a deployment shape `concord serve` doesn't have today. Rejected because the work is short (5 sub-endpoint calls + UPSERTs + one FTS row), the failure mode is well-tolerated (re-click; loader dedups), and adding Redis to a single-process FastAPI demo is a step we shouldn't take until something else needs it. If the rate-limit follow-up grows into a "we need distributed counters" problem, that's the right moment to revisit — and the migration is well-defined (`Limiter(storage_uri="redis://…")` plus a Celery/arq worker that consumes a `tasks` queue).

## Rejected: a SQLite-backed `enrichment_jobs` table

A small table (`bill_id PRIMARY KEY, started_at, finished_at, error`) would survive process restarts and give multi-worker correctness via SQLite's row-level locks. Rejected for v1 because (a) the in-process set is one line of state and serves the single-worker deployment correctly, (b) writing the table requires an extra DDL and a per-attempt UPSERT-then-cleanup dance that doesn't pay for itself until multi-worker is real, and (c) the failure mode of the in-process set (crash → re-click → loader dedups) is genuinely fine. When multi-worker becomes the revisit trigger, the table is the migration; recording the option here so a future ADR can adopt it without re-deriving.
