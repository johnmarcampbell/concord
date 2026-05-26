# 0006 — Snapshot-on-fetch for mutable entities

**Status**: Accepted, 2026-05-25.

## Context

[ADR 0002](./0002-jsonl-as-canonical-raw-store.md) makes JSONL the canonical raw store and treats Proceedings as immutable: a `granule_id` keys a single line, and the file is append-only because the underlying article never changes after publication.

The roadmap in [docs/plans/members-bills-votes-roadmap.md](../plans/members-bills-votes-roadmap.md) extends ingest to Members, Bills, Votes, Committees, and Amendments. These don't share the immutability property:

- **Bills** gain cosponsors, accumulate actions, change status, get amended.
- **Members** extend term history mid-Congress; rare metadata edits happen.
- **Votes** are mostly immutable once a roll is closed, but errata corrections do appear.
- **Committee memberships** are time-bounded and change with each Congress and with mid-session reassignments.

The append-only contract from ADR 0002 doesn't trivially survive that. Two shapes considered:

1. **Mutate in place.** One line per entity key in JSONL; the scraper rewrites the line when the upstream object changes. SQLite is loaded from the latest state.
2. **Snapshot-on-fetch.** Every fetch appends a new line tagged with `fetched_at`. Multiple lines per entity key are expected. SQLite is loaded by projecting "latest snapshot per key".

## Decision

Snapshot-on-fetch. Every Stage 0 fetch of a mutable entity appends a new JSONL line, never edits an existing one. Each line carries `fetched_at` (ISO-8601, UTC). The Stage 1 loader projects the latest snapshot per natural key into SQLite.

Concretely, the JSONL row shape for these entities is:

```json
{"fetched_at": "2026-05-25T14:02:11Z", "key": {...}, "payload": {...}}
```

where `key` is the natural key (e.g. `{"bioguide_id": "S000033"}` for a Member, `{"congress": 119, "bill_type": "hr", "bill_number": 1234}` for a Bill) and `payload` is the raw API response body, stored verbatim.

Proceedings stay under their original ADR 0002 contract — one line per `granule_id`, no `fetched_at` envelope. The snapshot envelope applies only to the new mutable-entity files.

## Consequences

**Trade-offs accepted:**

- **Storage grows with churn.** A Bill that accumulates 200 cosponsors over a year writes 200 snapshot lines if we fetch daily, each containing the full payload. At v1 scope (3 congresses, federal only) the upper bound is comfortably under a few GB per entity type. Revisit when v2 backfill to 1995 is on the table.
- **The loader is no longer a stream-and-insert.** Stage 1 now has to either group-by-key-keep-latest as it reads, or load all snapshots and project at the end. Both are cheap at our scale; neither requires anything beyond standard SQL.
- **JSONL files for mutable entities are not human-greppable for "current state"** — a `grep` on `bill_number=1234` returns every snapshot. Mitigated by: SQLite is the right tool for current-state queries; JSONL is the recovery substrate, not the day-to-day surface.

**Things this buys:**

- **ADR 0002's append-only contract survives unchanged.** No special-case "this file mutates, that one doesn't" rule. Every JSONL file in the project is append-only; the only variation is whether multiple lines share a key.
- **Mutation history is recoverable for free.** "When did Senator X become a cosponsor of HR 1234?" is a JSONL replay — no separate history table, no audit log to maintain. The canonical store *is* the history.
- **Re-derivation stays mechanical.** Stage 1 can be re-run at any earlier cutoff (`WHERE fetched_at < T`) to reconstruct SQLite state as of an arbitrary past moment. Useful for debugging "the surface showed the wrong sponsor list yesterday" without rerunning the scrape.
- **Failed or partial fetches are non-corrupting.** A botched run writes incomplete snapshots; the next clean run supersedes them. No reconciliation logic.

**What stays open:**

- **Compaction policy.** When storage starts to bite (post-v1), we'll want a periodic compaction that keeps the first snapshot, the latest snapshot, and any snapshots where the payload changed — discarding intermediate no-op fetches. This is a non-breaking change: compacted files load identically.
- **Hashing payloads for change detection.** A `payload_hash` field on each snapshot would let the loader skip lines that don't differ from the previous snapshot for the same key, and would make compaction trivial. Cheap to add now or later.
- **Fetch cadence is unspecified by this ADR** — it belongs in each entity's phase plan. The contract here is only that *every* fetch produces a snapshot, regardless of cadence.

## Rejected: mutate in place

Rewriting a JSONL line when the upstream object changes would let SQLite be loaded with a simple line-by-line read, no projection step. It was rejected for three reasons:

1. **It breaks the append-only invariant.** JSONL files become subject to torn writes, partial rewrites, and editor-style corruption modes that append-only files are immune to.
2. **It throws away mutation history.** The previous state is gone the moment the scraper sees a new version. Recovering "what the cosponsor list looked like last Tuesday" requires a separate audit mechanism that didn't need to exist.
3. **It special-cases Proceedings vs mutable entities.** Two contracts for "what a JSONL file is" instead of one. Snapshot-on-fetch keeps every JSONL file under the same append-only rule.

The storage cost of duplicated snapshots is real but bounded and addressable via compaction. The contract simplicity is permanent.
