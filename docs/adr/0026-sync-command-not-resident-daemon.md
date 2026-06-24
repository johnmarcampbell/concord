# 0026 — Scheduled scraping is a `sync` command, not a resident daemon

**Status**: Accepted, 2026-06-23.

## Context

We wanted automatic, recurring scraping so the public demo stays current without manual `run` invocations. The prompt that started this was "build a daemon," taking the sibling project Cirrus's `cirrus daemon` as inspiration — a long-lived process that loops `{ heartbeat; sync; wait interval }` forever, owning its own cadence inside the container.

Two facts about Concord made a direct port the wrong move:

1. **Concord already chose the opposite deployment shape, and wrote it down.** [`docs/docker.md`](../docker.md) states the image "stays single-purpose" and punts scheduling to "host-side `cron` (or `systemd` timers)"; [`docs/deployment.md`](../deployment.md) has a "Daily updates via cron" section; and [ADR 0007](./0007-parallel-pipelines-per-entity.md) explicitly says cross-entity ingest orchestration "belongs at the CLI or shell-script level, not inside the per-entity scrapers." A resident daemon reverses all three.

2. **The existing scheduled-scrape orchestration had already rotted — and that, not the lack of a daemon, was the real defect.** The `deployment.md` cron block still invoked `concord pull` / `concord load` / `concord index`; `pull` was renamed to `scrape proceedings` long ago. Because the orchestration ("what to scrape, over what window, in what order, calling which command") lived as untested shell snippets in Markdown, nothing broke when the CLI surface moved. Orchestration in shell drifts silently; orchestration in code breaks loudly in CI.

The insight that resolved it: Cirrus's `daemon.py` is only ~40 lines because the real work lives in an entrypoint-agnostic `jobs.sync()` seam. The part worth stealing is *that seam*, not the loop around it. The loop earns its keep only where there is no process supervisor and adding `cron` is awkward (a bare container) — and on the VPS, `systemd` is already supervising `concord-web`, so a `systemd` timer firing a one-shot command is strictly less machinery than a service wrapping an in-process sleep loop.

## Decision

Add a single root-level command, **`concord sync`**, that performs one **Sync** (see [CONTEXT.md](../../CONTEXT.md) → Orchestration): a bounded, incremental, best-effort pass over all four entity types. The operator's scheduler (`cron` / `systemd` timer) owns cadence; Concord owns the orchestration, in tested code.

Concretely:

- **Per entity, all stages.** A Sync runs Scrape → Load → Index for proceedings, members, bills, and votes, so the derived store is left fully queryable. Index cost stays at cents/day because Stage 2 is idempotent per chunk — only genuinely new chunks embed.
- **Bounded windows, never a backfill.** Proceedings are scraped over a rolling `[today − lookback, today]` window (`--lookback-days`, default 7), which is constant-cost in steady state and cannot surprise a first run with a decade of history. The mutable entities are scraped over the **current Congress** only (a calendar-derived helper, not the CLI's historical `(117, 118, 119)` default), with `--skip-unchanged` always on — i.e. the "daily incremental is just `--skip-unchanged`" path that [ADR 0015](./0015-staleness-aware-rescrape.md) already designed. Historical pulls (old dates, closed Congresses) remain a deliberate one-shot **Backfill** via `run <entity>`.
- **One pipeline definition, shared.** Each entity's inline scrape→load→index chain (today duplicated inside every `run_<entity>_command`) is extracted into a `run_<entity>_pipeline(...)` function in that entity's CLI module; both `run <entity>` and `sync` call it. The Sync is a composition root in `cli/sync.py` that calls the four explicitly — **not** a base class or protocol (which [ADR 0007](./0007-parallel-pipelines-per-entity.md) forbids).
- **Best-effort across entities.** One entity's failure is captured and reported but does not abort the others (the four pipelines are independent by ADR 0007, and everything is idempotent, so a failed entity simply retries next Sync). The command exits non-zero if any entity failed and prints a one-line-per-entity summary.
- **Self-guarding against overlap.** The command takes an advisory `fcntl.flock` on `data/.sync.lock` at startup (`LOCK_EX | LOCK_NB`) and exits cleanly with a distinct code if another Sync already holds it. The lock is kernel-released on process death (no stale-lock footgun), so the command is safe under cron, systemd, `docker run`, or an accidental double-invocation — overlap protection travels with the command rather than depending on the operator wiring `flock(1)` into their crontab.
- **Monolithic.** No entity selector. `--skip-unchanged` makes a quiet entity nearly free, so one cadence over all four is cheap; split cadences (which would push "which entity, which schedule" back into the crontab) are explicitly out of scope until a concrete need appears.

## Consequences

**Trade-offs accepted:**

- **Overlap is now possible, where a daemon loop made it impossible.** A wall-clock scheduler can fire a second Sync while the first still runs; we pay for that with the in-process `flock` guard. This is the one concurrency concern the rejected loop would have gotten for free.
- **Stage 1/2 failures live only in the command's exit code + stderr summary.** A **Scrape Run** ([ADR 0021](./0021-scrape-run-observability.md)) is born only where the network is, so a Load or Index failure produces no ledger row. The scheduler's log (journald / cron) is therefore the canonical record for those, which makes the summary line and exit code load-bearing — they are a deliberate deliverable, not an afterthought.
- **`deployment.md` and `docker.md` must be rewritten** to drive `concord sync` and drop the stale per-stage cron block. Until then those operational docs are doubly stale (wrong command names *and* the wrong model). Tracked as the implementation's doc step.

**Things this buys:**

- **The orchestration can no longer drift silently.** Renaming a scraper now breaks the `sync` call site and CI goes red, instead of a Markdown snippet quietly lying.
- **One mental model, two cadences, zero duplication.** `run <entity>` (one entity, any window, incl. backfill) and `sync` (all entities, bounded, scheduled) share the same per-entity pipeline function.
- **Forward-compatible with the daemon we declined.** If the self-contained-container need ever returns, a thin `concord daemon` looping `sync` is exactly Cirrus's shape — with no rename and no rework, because the seam already exists.

**What stays open:**

- **No periodic forced full refresh.** Senate errata corrections are missed under perpetual `--skip-unchanged` (ADR 0015's known gap). The workaround is an operator-scheduled `run votes` without the flag; promote to a `sync` knob only if it bites.
- **No entity selector / split cadence** until a real workflow needs it (see above).

## Rejected: a resident daemon loop (the original ask)

A long-lived `concord daemon` looping `{ sync; wait interval }` with a `threading.Event`, SIGTERM/SIGINT handlers, and a `heartbeat.json`. Rejected because:

1. **It reverses a documented decision for no gain on the actual deployment.** The VPS already runs `systemd`; a timer + one-shot command is less machinery than a service wrapping a sleep loop, and `systemd` won't double-launch an active unit.
2. **A `threading.Event` loop reimplements — worse — what the scheduler already provides:** reboot persistence, crash restart, missed-run catch-up, log capture and rotation. A daemon would still need `systemd`/`compose` to keep *itself* alive, so the loop adds a layer without removing one.
3. **The liveness machinery is redundant here.** `heartbeat.json`, file logging, and signal handlers exist to keep a *loop* observable and to exit it cleanly *between cycles*. With no loop, the scheduler's log + the per-entity Scrape Runs + the exit code already answer "what happened last run."

The loop earns its place only in a supervisor-less container; should that become a target, see "forward-compatible" above — the seam is reusable as-is.

## Rejected: keep orchestration in the operator's crontab

Fix the stale `deployment.md` cron block in place (correct the command names, add `flock -n`) and call it done. Rejected because it leaves the orchestration — entity list, window logic, stage order, overlap guard — as untested shell that nothing in CI exercises. That is the precise failure mode that produced the stale `concord pull` references; correcting the symptom without moving the logic into code invites the next drift.

## Rejected: a literal start-date floor on the Sync

An earlier framing wanted a configurable date *floor* (`[floor, today]`). Rejected because a fixed floor grows without bound: a year after `floor`, every daily Sync re-enumerates 365+ days of issue metadata. A rolling `[today − lookback, today]` window is constant-cost in steady state and still bounds the first run. The floor intuition is real but belongs to the one-shot Backfill, not the recurring Sync; if a hard rail is ever wanted, it must only ever *narrow* the window (`max(floor, today − lookback)`), never widen it.
