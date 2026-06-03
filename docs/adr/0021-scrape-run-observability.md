---
status: accepted
---

# Scrape-run observability

Concord scrapes had no durable operational record: per-module loggers
(`logging.getLogger("concord.*")`) emitted to an *unconfigured* root logger,
and the `written/skipped/failed` counts in `PullResult` were printed to stderr
and then lost. We add a **Scrape Run** ledger that records, per Stage-0
execution, the count of successful network requests bucketed by endpoint and a
detailed **Run Event** for every request that hit an error — including whether
the error was resolved on retry — plus the project's first central logging
configuration so existing log lines become correlatable to a run. See
`CONTEXT.md` ("Observability") for the term definitions.

## What we decided

- **Unit of record = one Stage-0 execution for one entity** (a `scrape <entity>`
  pull, or the Stage-0 phase of `run <entity>`). `load`/`index` make no network
  calls and produce no Scrape Run. The recorder is installed at the scrape seam,
  not the CLI command, so it is born exactly where the network is.
- **Ambient `contextvars`, not injected parameters.** A new
  `concord/observability.py` holds a `ContextVar[Recorder | None]` and a
  `ContextVar[str | None]` for the run_id. The `scrape_run(...)` context manager
  sets them at pull start and resets + flushes in a `finally`. Each HTTP client's
  network chokepoint (`api.py:_get`, `text.py:_get_with_retry`,
  `senate_xml.py:_get_xml`) does a two-line `rec = active_recorder(); if rec: …`.
  This is a deliberate departure from Concord's otherwise-explicit injection
  style (`transport=`, `sleep=`, `progress=`): run_id correlation for logging
  *cannot* be threaded as a parameter into a bare `_log.warning(...)` deep in a
  retry loop — it fundamentally needs a contextvar — and the recorder rides the
  same mechanism rather than introducing a second one. The module is a thin
  shared helper, not a base class, per ADR 0007.
- **All three HTTP clients are instrumented** — `api.py` (api.congress.gov),
  `text.py` (congress.gov article text), `senate_xml.py` (senate.gov LIS XML).
  The text and senate surfaces are where most failures actually live (Cloudflare
  403s, the senate "404-as-200" trap); monitoring only api.congress.gov would be
  a false economy.
- **Endpoint buckets come from a route table, not heuristic substitution.**
  `observability.py` carries an ordered list of regex templates per source;
  first match wins; a concrete path that matches nothing falls to a loud
  `<source>:unmatched` bucket that also captures the concrete path (deduped,
  capped) so the missing route is debuggable. Concrete per-resource URLs
  (`/bill/119/hr/1234`) are never the key — that would defeat aggregation.
- **DB-authoritative record tables, with `runs.jsonl` as a cold backup.** The
  `runs` and `run_events` tables are *record* tables (ADR 0019): Concord-
  originated, not rebuildable from upstream JSONL, and untouched by an entity
  re-derivation. They are the queried source of truth. `runs.jsonl` is written
  alongside as an append-only audit log that is never read in normal operation
  — it exists purely as disaster-recovery insurance, justified because run
  history is *unreconstructable* (unlike `bill_briefs`, which can be regenerated
  by re-running the LLM). This inverts ADR 0002's "JSONL is the source of truth"
  for this one stream, on purpose.
- **Run Event grain = per error-encountering request.** A logical request (one
  `_get`/fetch call, including its internal retry loop) emits a Run Event iff it
  had ≥1 non-success attempt; the event carries the (capped, with overflow
  count) list of failed attempts and a `final_status` of `resolved` or `failed`.
  A first-try success emits nothing — the happy path is aggregated as counts.
- **429s are recorded as errors,** even though the client retries them
  indefinitely rather than aborting. The retry *behavior* is unchanged; only the
  *recording* differs. A rate-limit-aware client hitting a 429 reflects our own
  request-budget mismanagement, which is exactly the signal worth surfacing.
  "Error event in the ledger" therefore means "≥1 non-success attempt", not "the
  request failed". This is a deliberate divergence from this module's own
  docstrings, which (correctly, for retry policy) frame a 429 as "a wait
  condition, not a fault".
- **Central logging emits run_id-stamped text, not JSON.** Installed once at the
  CLI callback (`cli/__init__.py:_root`) and `serve` startup — not at import
  time, per ADR 0014 (the CLI is the contract; library imports must stay
  side-effect-free). A small `Formatter` subclass reads the run_id contextvar in
  `format()`, so every existing `concord.*` line — including the api.py retry
  heartbeats — gains a run_id with no call-site changes. JSON was rejected: the
  structured/queryable sink is now the DB, so logs only need to be a readable
  human heartbeat plus a correlation key.

## Considered options

- **Prometheus (rejected for the scrape path).** Prometheus is pull-based: it
  scrapes a long-lived `/metrics` endpoint on a schedule. Concord's scrapers are
  short-lived batch CLI processes that have exited before any scrape could
  occur. The batch workarounds each carry real costs — the Pushgateway is
  flagged by Prometheus's own docs as a last resort (metrics persist after the
  job dies, per-run identity is lost, it becomes a stateful SPOF), and the
  node_exporter textfile collector only fits fixed-schedule cron jobs on a host
  we control and still discards per-run history. Prometheus is also a poor fit
  for need #1 (a high-cardinality per-run/per-URL audit trail) regardless. The
  one natural Prometheus target is `concord serve` (a long-running FastAPI
  process), but that is not the scrape path. We can still derive metrics from the
  ledger later if desired.
- **One ADR vs two.** Folded the central-logging decision into this ADR rather
  than splitting it out: run_id is the shared spine between the ledger and the
  logging config, and they ship together.

## Consequences

- `scrape <entity>` now touches SQLite (the ledger), where previously Stage 0
  wrote only JSONL. The `scrape_run(...)` context manager owns a bootstrappable
  DB path and calls `ensure_schema` (ADR 0012 precedent). The ledger write is a
  cross-cutting telemetry side-effect, not a Stage-1 load — the entity JSONL is
  still the only entity data Stage 0 produces.
- A new schema migration (`_m003`) and `_HEAD` bump land per ADR 0017; the
  migration DDL must stay byte-equivalent to the `_BASE_SCHEMA` declaration or
  the schema-equivalence test fails.
- The `/runs` web dashboard is deferred to a later, separate effort; the tables
  it will read are created now.
