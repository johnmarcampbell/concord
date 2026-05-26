# 0009 — Multi-endpoint entities split their JSONL canonical store by sub-endpoint

**Status**: Accepted, 2026-05-25.

## Context

[ADR 0006](./0006-snapshot-on-fetch-for-mutable-entities.md) defines the snapshot-on-fetch envelope for mutable entities: every fetch appends one JSONL line tagged with `fetched_at` and a natural-key `key`. Phase 1 (Members) used one JSONL file because `/v3/member/congress/{n}` returns the entire Member shape in a single call.

Phase 2 (Bills) breaks that symmetry. The api.congress.gov surface for one logical Bill spans six independent endpoints:

- `/v3/bill/{c}/{t}/{n}` — identity + sponsor + `latestAction` + `policyArea` + counts
- `/v3/bill/{c}/{t}/{n}/cosponsors` — cosponsor list (paginated)
- `/v3/bill/{c}/{t}/{n}/actions` — full legislative action history (paginated)
- `/v3/bill/{c}/{t}/{n}/subjects` — multi-valued legislative subjects
- `/v3/bill/{c}/{t}/{n}/titles` — official, short, and popular titles
- `/v3/bill/{c}/{t}/{n}/summaries` — CRS summaries by version code

Phases 3 and 4 (Votes, Committees, Amendments) will hit the same situation. We need a rule for "one entity, many endpoints" before more than one phase has resolved it locally.

Three shapes considered:

1. **Bundled snapshot.** One `data/bills.jsonl`; each line's `payload` aggregates all six sub-responses fetched as a unit. The "snapshot" is the bundle.
2. **Discriminated single file.** One `data/bills.jsonl`; each line is a snapshot of one sub-endpoint, with `key.kind` indicating which sub-resource it came from.
3. **One JSONL per sub-endpoint.** Six files: `data/bills.jsonl`, `data/bill_cosponsors.jsonl`, `data/bill_actions.jsonl`, `data/bill_subjects.jsonl`, `data/bill_titles.jsonl`, `data/bill_summaries.jsonl`. Each line is one fetch of one sub-endpoint, keyed by `(congress, bill_type, bill_number)`.

## Decision

One JSONL file per sub-endpoint. Each file follows the ADR 0006 envelope shape unchanged: `{"fetched_at": ..., "key": {"congress": ..., "bill_type": ..., "bill_number": ...}, "payload": ...}`. The `key` is identical across files for snapshots that describe the same Bill — joining at Stage 1 is a key lookup.

The Stage 1 loader reads all six files, groups each by key, picks the latest snapshot per file per key, and projects into the per-table targets in SQLite (`bills`, `bill_cosponsors`, `bill_actions`, `bill_subjects`, `bill_titles`, `bill_summaries`).

This rule applies to every future entity whose API surface is a primary resource plus N sub-resources. Phase 3's `/house-vote` and `/senate-vote` may end up with their own per-vote sub-endpoints (e.g. `/positions`); when they do, the same fan-out applies.

## Consequences

**Trade-offs accepted:**

- **More files in `data/`.** Six per Bill-like entity. The directory listing grows linearly with phases. Mitigated by consistent prefix naming (`bill_*.jsonl`, `vote_*.jsonl`) so the grouping is visually obvious.
- **Loader has to join across files.** Stage 1 reads six streams instead of one, joins by composite key, and writes per-table. The join is in-memory at scrape scale (~50K bills × 6 = 300K lines, ~3 GB raw). Below SQLite working-set thresholds — no streaming-aware loader needed at v1.
- **Partial scrapes leave the SQL projection incomplete.** If `/actions` was fetched for a Bill but `/cosponsors` wasn't, the loaded `bills` row exists with empty `bill_cosponsors`. The Bill page would render a misleading "0 cosponsors" instead of "we haven't fetched cosponsors yet." Mitigated by per-file `fetched_at`: the surface layer can detect and label sub-sections that lag behind the identity row.

**Things this buys:**

- **ADR 0006 carries over unchanged.** No special "bundled snapshot" envelope shape, no `kind` discriminator inside `key`. Every JSONL file in the project — Members, Bills, Bill sub-resources, future entities — uses the same `{fetched_at, key, payload}` shape. The loader logic for "group by key, keep latest" is one pattern, not two.
- **Partial fetches are not wasted.** If a backfill crashes after writing five sub-responses for a Bill but before the sixth, the five existing snapshot lines are already useful: they project into SQLite on the next load. A bundled-snapshot scheme would discard those five lines because there's no complete bundle to write.
- **Each sub-endpoint can refresh independently.** A daily incremental that re-fetches only `/actions` (the part that changes most) writes to one file. The other five files remain at their last fetch. The bundled scheme would force a re-fetch of all six every time anything changes.
- **The "fetch" unit and the "snapshot" unit line up.** ADR 0006's whole framing is "every fetch produces a line, regardless of cadence." The bundle scheme breaks that — one fetch produces no line, the *batch* of N fetches produces one line. This rule keeps the framing intact.
- **Each sub-resource's JSONL is independently inspectable.** `wc -l data/bill_cosponsors.jsonl` answers "how many cosponsor-list fetches have we done?" directly. With a bundle, that question requires parsing JSON to count fields.

**What stays open:**

- **Compaction across files.** ADR 0006 names compaction as a future concern; this ADR doesn't change that, but compaction now operates per file. Compacting `bill_actions.jsonl` is independent of compacting `bill_cosponsors.jsonl` — generally easier.
- **The "loader joins by key" pattern accretes.** Six files at Phase 2; if Phase 4 adds three vote-related files and Phase 4 adds two committee-related files, the loader grows. Stays manageable while each entity's loader is its own module per [ADR 0007](./0007-parallel-pipelines-per-entity.md). Revisit if a single loader is reading >10 files.
- **Cross-file consistency at a point-in-time** is not enforced. A snapshot line in `bills.jsonl` from Tuesday and one in `bill_cosponsors.jsonl` from Thursday describe the Bill at two different moments. Stage 1's projection uses "latest per file"; nothing pins them to the same moment. For v1 surfaces (current state only) this is fine. Time-travel queries (Phase 7 territory) would need a `fetched_at` overlap check.

## Rejected: bundled snapshot

Folding all six sub-responses into one line gives the cleanest "snapshot" semantics — one moment in time per JSONL line, atomic at the entity level. It was rejected because:

1. **It throws away partial work on failure.** The scraper has to hold all six responses in memory until the sixth lands, then write the bundle. Any error in the sixth fetch wastes the previous five — and rate-limited backoff windows make those five expensive.
2. **It special-cases mutable-entity shape in two ways.** The envelope grows a sub-payload structure for entities like Bills, but Members stay flat. Two `payload` shapes instead of one.
3. **Incremental refresh requires all-or-nothing.** Refetching just `/actions` (the highest-churn sub-endpoint) still has to bundle in all five other responses to write a snapshot. Re-fetching five unchanging endpoints is wasted work and inflates JSONL size with duplicated payloads.

## Rejected: discriminated single file

A single `bills.jsonl` where each line carries `key.kind = "cosponsors"` (etc.) keeps the file count down. It was rejected because:

1. **It hides the grouping in `key`.** ADR 0006's `key` is the *natural key* of the entity. Adding a `kind` field overloads it with a "sub-endpoint discriminator" that isn't part of the entity's identity. The Bill `(congress, bill_type, bill_number)` is the natural key whether we're talking about actions or cosponsors.
2. **Grep stops working.** `wc -l data/bills.jsonl` tells you nothing meaningful — it's a count of sub-endpoint fetches, not Bills. With per-sub-endpoint files, `wc -l data/bill_actions.jsonl` is an unambiguous "how many action-list fetches happened."
3. **The loader still has to filter by `kind`.** All the join complexity of the per-file approach, with none of the inspection or partial-refresh benefits.
