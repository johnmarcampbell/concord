# 0026 — Unsupervised scraping daemon

**Status**: Accepted, 2026-06-21. Builds on [ADR 0007](./0007-parallel-pipelines-per-entity.md) (parallel pipelines per entity), [ADR 0014](./0014-publish-to-pypi-cli-first.md) (the CLI is the stable contract), [ADR 0015](./0015-staleness-aware-rescrape.md) (staleness-aware re-scrape), and [ADR 0021](./0021-scrape-run-observability.md) (Scrape Run ledger).

## Context

Every stage in Concord is operator-initiated: a human runs `concord run proceedings --from … --to …`, `concord scrape bills --congresses …`, and so on. Keeping the derived store current therefore means somebody remembering to run four entities' pipelines on a regular basis, and remembering to fill in history when a new deployment starts from an empty `data/`. Nothing in the project schedules that work.

Two prior decisions make automation cheap rather than a rewrite:

- [ADR 0015](./0015-staleness-aware-rescrape.md) already gives us "daily incremental is just `--skip-unchanged`": the list-walk is the cursor and the JSONL is the state, so a forward refresh is one cheap pass with no new orchestration.
- [ADR 0021](./0021-scrape-run-observability.md) already records a Scrape Run per Stage-0 execution, with per-endpoint success counts and a Run Event for every failed request, plus run_id-stamped logging. Any automation that drives Stage 0 inherits durable observability for free.

What is missing is the thing that *decides what to run and when*, runs it unattended, and — critically — fills in historical data over time instead of only ever scraping "today". That is this ADR.

The deployment shape we are targeting is the single VPS that already hosts `concord serve` (see ADR 0003). The two realistic ways to schedule recurring work there are an external timer (systemd/cron invoking the CLI) or a self-contained long-lived process. We chose the self-contained process; the external-timer option is recorded under "Rejected".

## Decision

Add a new top-level command, **`concord daemon`**, that runs a long-lived **scheduler** loop. Each iteration is a **Tick** (default cadence: once every 24h). The daemon is a *supervisor*, not a new pipeline: it does no scraping itself and imports nothing from `concord.scraper` / `concord.pipeline`. It executes work by spawning the existing CLI as child processes (`python -m concord <stage> <entity> …`). This is deliberate — it binds the daemon to ADR 0014's stable surface (the CLI), keeps it decoupled from pipeline internals, isolates a crash or memory leak in one entity's scrape from the long-lived loop, and lets each child mint its own Scrape Run (ADR 0021) and emit its own run_id-stamped logs with no extra wiring.

### The endpoints the daemon drives

One Tick drives all four entity pipelines. For each, the daemon runs the three stages in order (`scrape` → `load` → `index`), each as its own child invocation:

| Entity | Forward (every Tick) | Backfill (one chunk per Tick) | Index needs |
|---|---|---|---|
| Proceedings | `scrape proceedings --from <today − forward-days> --to <today>` | one older date-window, walking back to `--proceedings-since` | `OPENAI_API_KEY` (embeddings) |
| Members | `scrape members --congresses <current> --skip-unchanged` | one older target Congress | FTS5 only |
| Bills | `scrape bills --congresses <current> --skip-unchanged` (+ optional `enrich`) | one older target Congress | FTS5 only |
| Votes | `scrape votes --congresses <current> --chambers house --skip-unchanged` | one older target Congress | computes Party Unity Score |

"Current" Congress is `max(--congresses)`; the older members of that set are the backfill targets. Proceedings is date-windowed, so its "current" is a trailing window ending today and its backfill walks date-windows backward to a floor.

### Cadence

A **daily** Tick to begin (`--interval`, default `24h`). The cadence is uniform across entities at v1 — one knob, not per-entity schedules (see "What stays open"). The loop sleeps `interval` between Ticks; on startup it runs a Tick immediately, then sleeps. `--once` runs exactly one Tick and exits (for testing, and so the same binary can be driven by an external timer if an operator later prefers that).

### Auto-backfill strategy

Forward passes keep the present fresh; **backfill** fills the past, in **bounded chunks**, one chunk per entity per Tick (`--backfill-per-tick`, default 1). This spreads a cold-start historical fill across many days instead of issuing a multi-hour thundering scrape on first launch, and stays friendly to the api.data.gov rate budget.

- **Congress-scoped entities (Members, Bills, Votes):** the chunk unit is one Congress. Each Tick the daemon picks the next target Congress not yet marked backfilled for that entity, scrapes it (full, *without* `--skip-unchanged`, so a cold congress is captured completely), and on success records it done. Once every target Congress is done, the entity has no backfill work and only its forward pass runs.
- **Proceedings:** the chunk unit is a date-window of `--proceedings-window` days (default 30). A cursor (`proceedings_oldest_scraped`) walks backward from today to `--proceedings-since` (default the start of the 117th Congress, `2021-01-03`, matching the default Congress set). When the cursor reaches the floor, backfill is complete.

### Daemon state

Backfill progress lives in a single JSON watermark file, `data/daemon_state.json` (path derives from `--data-dir`): the set of `(entity → [backfilled congresses])` and the proceedings cursor. It is **Concord-originated operational state**, the same category as the `runs` ledger (ADR 0019 / 0021) — not rebuildable from upstream and not a mirror of anything. It is stored as JSON rather than a SQLite record table specifically to avoid a `user_version` migration (ADR 0017) for state that only the daemon reads and writes and that the web layer never queries; it sits next to `runs.jsonl` in `data/` by the same "operational sidecar" logic.

A completion marker is applied to the in-memory state and the file is rewritten **only after** the relevant child process exits 0. A crash mid-Tick therefore loses at most the current chunk, which the next Tick re-attempts; because every stage is idempotent (ADR 0002/0006/0015) the re-attempt is safe and, for forward passes, cheap.

### Failure isolation

Each child invocation is independent. A non-zero exit is logged (with the child's run_id surfacing via ADR 0021) and the Tick continues with the next job; one entity's failure never aborts the Tick or the daemon. Backfill markers are gated on success, so a failed congress is simply retried next Tick. 429s are already handled inside the HTTP client's retry loop (ADR 0021) and recorded as Run Events; the daemon's bounded per-Tick work is the coarse rate-limit control on top.

### Lifecycle

`configure_logging()` is installed at the daemon CLI seam (as `serve` does, per ADR 0021), not at import time. `SIGTERM`/`SIGINT` request a clean stop: the daemon finishes the in-flight child, then exits — so `systemctl stop` / `docker stop` is graceful. Liveness (restart-on-crash) is delegated to the process supervisor: a sample systemd unit ships under `deploy/`, and a container can use `restart: always`. The daemon owns *scheduling*; the OS owns *being alive*.

## Consequences

**Trade-offs accepted:**

- **A new long-lived process to operate.** Unlike a stateless cron line, the daemon must be kept running and its `daemon_state.json` must persist across restarts (it lives in the same `data/` volume the rest of the pipeline already needs). This is the cost of the self-contained option we chose; see "Rejected".
- **Subprocess orchestration sees results coarsely.** The daemon knows a child's exit code, not a structured result object. Detail lives in the Scrape Run ledger (ADR 0021), which is the right place for it; the daemon stays a thin scheduler. The cost is one process spawn per stage per entity per Tick (~24/day) — negligible at daily cadence.
- **Backfill of *closed* Congresses is one-shot.** Once a past Congress is marked done it is not re-scraped, so a late errata correction to an old roll or bill is missed — the same caveat ADR 0015 accepts for Senate errata under `--skip-unchanged`. Workaround: delete the entity's entry from `daemon_state.json` (or run the CLI by hand) to force a re-backfill.
- **`daemon_state.json` can drift from reality.** If an operator scrapes by hand, the daemon doesn't know and may redo that chunk. Redo is idempotent and cheap, so this is a wasted pass, never a corruption.

**Things this buys:**

- **Hands-off freshness.** The derived store tracks Congress without anyone remembering to run anything.
- **Self-healing cold start.** A fresh deployment fills its history automatically over a bounded number of days instead of demanding a giant manual seed scrape.
- **Free observability and idempotency.** By driving the CLI it inherits the Scrape Run ledger, run_id logging, dedup keys, and `--skip-unchanged` with zero new machinery.
- **Decoupled from internals.** The daemon depends only on the CLI contract (ADR 0014); pipeline refactors don't touch it.

**What stays open:**

- **Per-entity cadence.** v1 is one uniform interval. If Bills want hourly and Members weekly, split the schedule later — the Tick already builds an independent job list per entity (ADR 0007), so this is additive.
- **Senate votes.** Forward and backfill drive House only, matching Phase 3a (ADR 0010). When Phase 3b lands, add `senate` to the votes chamber set here.
- **Periodic re-backfill of closed Congresses** to catch errata (see trade-off above). Deferred until a real need appears, mirroring ADR 0015's stance.
- **A `/daemon` status surface.** The state file and the existing `runs` tables are enough to inspect by hand for now; a dashboard is future work alongside ADR 0021's deferred `/runs` view.

## Rejected: external timer (systemd timer / cron) invoking the CLI

The leading alternative was to add no long-lived code at all: ship a systemd timer (or crontab line) that runs `concord run <entity>` on a schedule, letting the OS own restarts, logging capture, and missed-run catch-up. It is genuinely simpler and more robust *for the forward pass*. We rejected it as the primary mechanism because the **backfill** half of the requirement wants stateful, bounded, self-advancing progress ("do the next chunk of history each run, until caught up, then stop"). Expressing that in cron means either a separate stateful helper script the timer calls (which is most of this daemon, minus the loop) or encoding the cursor logic into the units themselves. Concentrating the scheduling *and* the backfill state machine in one self-contained, unit-testable Python component — rather than splitting it across systemd unit files and a helper — won. The external-timer path is not lost: `concord daemon --once` is exactly one Tick, so an operator who prefers OS-owned scheduling can run *that* from a timer and still get the backfill state machine.

## Rejected: in-process orchestration

The daemon could import the entity pipelines and call them in-process instead of spawning the CLI. Rejected: it would couple the daemon to private `cli/<entity>.py` stage workers and `concord.pipeline` internals (against the ADR 0014 contract boundary), and a leak or hard crash deep in one scrape would take down the long-lived loop. Subprocesses give crash isolation and make each entity's Scrape Run and run_id logging fall out naturally. The marginal cost (process spawns) is irrelevant at daily cadence.

## Rejected: a SQLite `daemon_state` record table

State could live in a record table (ADR 0019) beside `runs`. Rejected for v1 because it would require a `_BASE_SCHEMA` change plus a migration and `_HEAD` bump (ADR 0017) for a single-writer, single-reader cursor the web layer never queries. JSON in `data/` carries the same "operational, not rebuildable" semantics as `runs.jsonl` with none of the migration ceremony. Promote it to a table if a future reader (a dashboard, multiple daemons) needs to query it.

## Rejected: APScheduler / Celery

A scheduling library was considered for the loop. Rejected as overkill: a single daily Tick with an injectable clock and sleep is a dozen lines, needs no persistence layer of its own (the backfill cursor is our state), and adds no dependency. The project's style is thin helpers over frameworks (ADR 0007); a `while True: tick(); sleep(interval)` with signal handling fits it.
</content>
</invoke>
