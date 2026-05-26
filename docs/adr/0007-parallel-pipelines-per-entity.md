# 0007 — Parallel Stage 0 + Stage 1 per entity type

**Status**: Accepted, 2026-05-25.

## Context

Concord's current pipeline is one-source, one-entity: api.congress.gov's `/congressional-record` endpoint → Proceedings. The roadmap in [docs/plans/members-bills-votes-roadmap.md](../plans/members-bills-votes-roadmap.md) extends this to five additional sources (`/member`, `/bill`, `/committee`, `/amendment`, `/house-vote` + `/senate-vote`) producing five new peer entity types.

[members-bills-votes-scope.md](../plans/members-bills-votes-scope.md) names two ways to slot this in:

1. **Generalize the existing stages.** Stage 0 becomes "scrape any congress.gov endpoint to JSONL"; Stage 1 becomes "load JSONL → typed tables". One pipeline, parameterized by entity type.
2. **Parallel pipelines, shared DB.** Each entity type gets its own Stage 0 + its own Stage 1. Stage 2 (Index) and Stage 3 (Enrich) operate across the unified SQLite.

The question is where to draw the line between "shared infrastructure" and "per-entity code".

## Decision

Stage 0 and Stage 1 are parallel per entity type. Stage 2 (Index) and Stage 3 (Enrich) are shared and operate across the unified SQLite.

In practice:

- Each entity gets its own scraper module (`scraper/members.py`, `scraper/bills.py`, …) and its own JSONL file (`data/members.jsonl`, `data/bills.jsonl`, …).
- Each entity gets its own loader module (`pipeline/load_members.py`, …) writing to its own SQLite tables.
- The CLI gains per-entity verbs: `concord scrape members`, `concord scrape bills`, `concord load members`, etc. The existing `concord scrape` (for Proceedings) becomes `concord scrape proceedings`.
- Stage 2 stays one module operating across the chunks table (see [ADR 0008](./0008-chunks-generalize-beyond-proceedings.md) for how chunks span entity types). Stage 3 stays one module producing the cross-entity `mentions` table.
- Shared concerns — HTTP client, rate-limit handling, retry policy, `fetched_at` envelope from [ADR 0006](./0006-snapshot-on-fetch-for-mutable-entities.md) — live in a thin `scraper/_common.py` that each per-entity scraper imports. Not a framework; a utility module.

## Consequences

**Trade-offs accepted:**

- **Six scrapers instead of one.** Some structural duplication across modules (pagination loop, error handling, JSONL writer). Bounded by the shared utility module; what stays per-entity is the part that's actually different.
- **CLI surface grows.** Five new verb-noun pairs at v1. Mitigated by consistent shape (`concord <stage> <entity>`); discoverability via `--help` listings.
- **No single "scrape everything" command out of the box.** Adding one is trivial (a shell loop or a meta-command), but the per-entity commands are the canonical interface.

**Things this buys:**

- **Per-source quirks stay visible.** Each congress.gov endpoint has its own response shape, pagination idiom, rate-limit behavior, and mutability profile. The `/bill` response has nested cosponsor objects with their own ID conventions; `/house-vote` is thin and needs augmentation from clerk.house.gov bulk XML; `/member` has a quirky term-history substructure. A generalized Stage 0 would either lose this detail behind a lowest-common-denominator schema or accumulate per-source branches inside a single function — the second is worse than just having separate modules.
- **Blast radius per change is small.** Modifying the Bills loader to handle a new field can't break Members ingest. A single shared loader would couple them.
- **Each entity ships independently.** The roadmap phases entity ingest one at a time. Parallel modules mean Phase 1 (Members) doesn't touch the code Phase 2 (Bills) will write.
- **Stage 2 / Stage 3 stay shared, which is where sharing actually pays.** Chunking, embedding, FTS5 indexing, and entity extraction all benefit from one implementation. The decision here is specifically about Stages 0 and 1, where the inputs differ per source.

**What stays open:**

- **The shared utility module will accrete.** That's fine as long as it stays a utility, not a framework — the test is whether a per-entity scraper still reads top-to-bottom as "fetch, paginate, write" without indirection through base classes.
- **If a seventh and eighth source land in v2 (FEC, LDA), the per-entity pattern may start to feel heavy.** The threshold for revisiting is "two new sources have nearly identical Stage 0 logic and only the URL differs." We're nowhere near that at v1.
- **Cross-entity ingest orchestration** (e.g. "scrape everything for Congress 119 since last Tuesday") is unspecified by this ADR. It belongs at the CLI or shell-script level, not inside the per-entity scrapers.

## Rejected: generalized Stage 0 + Stage 1

A single `concord scrape --entity bills` would be aesthetically cleaner and produce less code by line count. It was rejected because the apparent symmetry between the congress.gov endpoints is shallow: they share an auth scheme and a base URL and almost nothing else. A generalized scraper would either expose a config object large enough to encode every per-source quirk (at which point the "generalization" is a serialization of what the per-entity module already says directly), or it would suppress those quirks behind a uniform schema and lose detail.

The roadmap also expects sources beyond congress.gov in v2 — clerk.house.gov XML in Phase 3 for full roll-call detail, FEC and LDA in v2. A "generalize over congress.gov endpoints" abstraction would be the wrong shape for those anyway. Better to start with one module per source and let any real sharing surface from below.
