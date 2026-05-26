# Members, Bills, Votes — roadmap

**Status:** roadmap, not yet broken into implementation plans. Follow-up to [members-bills-votes-scope.md](./members-bills-votes-scope.md). Each phase below should get its own `docs/plans/<slug>.md` before code lands.

**Date:** 2026-05-25.

## Scope locked from the scoping conversation

- **Audience / surface:** public demo, extending the existing FastAPI+HTMX site. Same ethos as the current `/search`.
- **Entities in v1:** Member, Bill, Vote, Committee, Amendment. Bill *text* indexed for RAG.
- **History:** last ~3 congresses (117th–119th, roughly 2021→present).
- **Repo:** stays in Concord. The domain broadens; [CONTEXT.md](../../CONTEXT.md) gets updated, not replaced.

Explicitly **out of v1:** FEC / lobbying money trails, state legislatures, network-graph visualizations, conversational layer over bills, alerts/subscriptions, graph DB.

## Architectural decisions to ratify before Phase 1

These need new ADRs. They're prerequisites; no ingest work should start until they land.

### ADR 0006 — Mutability model for non-Proceeding entities

**Problem.** [ADR 0002](../adr/0002-jsonl-as-canonical-raw-store.md) makes JSONL append-only and Proceedings immutable. Bills mutate: they gain cosponsors, get amended, change status. Member term histories extend.

**Proposed answer.** Keep JSONL append-only. On every fetch of a mutable entity, append a *snapshot* line with a `fetched_at` timestamp. The Stage 1 loader projects "latest snapshot per key" into SQLite. The canonical-store contract survives; mutation history is recoverable by replaying JSONL with earlier cutoffs.

Tradeoff: storage grows with churn (every cosponsor addition rewrites a Bill snapshot). At 3 congresses this is fine. Revisit if we backfill to 1995.

### ADR 0007 — Pipeline shape for multi-source ingest

**Problem.** [members-bills-votes-scope.md](./members-bills-votes-scope.md) names two options: parallel pipelines per entity, or one generalized pipeline.

**Proposed answer.** Parallel Stage 0 + Stage 1 per entity type. Each source produces its own JSONL file (`members.jsonl`, `bills.jsonl`, `votes.jsonl`, …) and its own loader. Stage 2 (Index) and Stage 3 (Enrich) operate across the unified SQLite. CLI gains `concord scrape <entity>` / `concord load <entity>`.

Rationale: each congress.gov endpoint has its own response shape, rate-limit behavior, and mutability profile. A "generalized Stage 0" abstracts over differences that should stay visible. Three similar loaders beats one premature framework.

### ADR 0008 — Chunks generalize beyond Proceedings

**Problem.** [ADR 0005](../adr/0005-chunks-as-unit-of-retrieval.md) defines Chunks as spans of a Proceeding's text. Bill text RAG means chunks come from Bills too.

**Proposed answer.** A chunk gets a `source_type` + `source_id`. The `chunks`, `chunks_fts`, and `vec_chunks` tables stay single-tabled, with `source_type` as a discriminator. Search ranking is the same; surface layer decides whether to roll up to a Proceeding or a Bill.

## Phasing

Each phase ships a visible public page. Order is driven by (a) prerequisite-first and (b) what unlocks the next phase's UX.

### Phase 1 — Members

Smallest entity, most stable, fastest visible win.

- Stage 0: scrape `/member` for the 3 congresses in scope → `data/members.jsonl`
- Stage 1: `members` table (Bioguide ID PK), `member_terms` child table
- Stage 2: name-alias FTS5 index for "did you mean" search (no embeddings)
- Web: `/members` index + `/members/{bioguide_id}` profile page (party, state, term history)
- CONTEXT.md update: define Member, Bioguide ID, Term

**Done means:** a journalist can pull up any current member's profile by name from the public site.

### Phase 2 — Bills (metadata)

- Stage 0: scrape `/bill` → `data/bills.jsonl` (snapshot-on-fetch per ADR 0006)
- Stage 1: `bills`, `bill_sponsors`, `bill_cosponsors`, `bill_actions` tables
- Web: `/bills/{congress}/{type}/{number}` page — sponsor + cosponsor list (links to Member pages), action history, status
- Cross-link from Member page: "Sponsored", "Cosponsored" sections

**Done means:** a bill page reads like a profile, with every name a working link.

### Phase 3 — Votes

- Stage 0: scrape `/house-vote` + `/senate-vote`. Augment from clerk.house.gov / senate.gov bulk XML for full per-member positions (the API is thin here).
- Stage 1: `votes`, `vote_positions` (member × vote → yea/nay/present/not-voting) tables
- Web: `/votes/{chamber}/{congress}/{session}/{roll}` page; Member page gains "Recent votes" + party-line alignment stat; Bill page gains "Vote history".

**Done means:** the Member↔Bill loop closes through Votes. Party-line break stats are computable.

### Phase 4 — Committees + Amendments

- Stage 0: scrape `/committee`, `/amendment` → respective JSONL
- Stage 1: `committees`, `committee_memberships` (time-bounded), `amendments` tables
- Web: Bill page gains "Committee path" timeline; Member page gains committee assignments; Committee page lists membership + referred bills.

**Done means:** the legislative path of a bill is visible end-to-end on the bill page.

### Phase 5 — Bill text RAG

This is where Stage 2 generalizes (ADR 0008).

- Stage 0: fetch bill text from congress.gov for bills in scope → JSONL or per-bill files
- Stage 2: generalize chunks table with `source_type`; chunk bill text; embed via existing OpenAI pipeline ([ADR 0004](../adr/0004-openai-embeddings.md))
- Web: existing `/search` becomes hybrid across Proceedings + Bills, with source-type filters. Bill page gains "Search within this bill".

**Done means:** a single search box returns Congressional Record passages *and* bill-text passages, with the same RRF ranking.

### Phase 6 — Mentions (Stage 3 Enrich)

This is the bridge to the existing Proceedings infra. It was always going to be Stage 3 per [CONTEXT.md](../../CONTEXT.md); the entity tables are the prerequisite that made it tractable.

- Stage 3: NER + dictionary-match over Proceeding text → `mentions` table (proceeding_id × entity_type × entity_id)
- Web: Member page gains "Mentioned in proceedings"; Bill page gains "Discussed in"; Proceeding page gains a sidebar of detected entities.

**Done means:** Concord's original Proceeding corpus is now navigable *through* the entity graph.

### Phase 7 — Universal search + entity pages polish

- One search box, faceted across all entity types
- "Most cosponsored with" / "Most votes against party" style derived rankings (computed as SQLite materialized views, not at request time)
- Editorial-neutrality review of any ranking surface (per the risk in the scoping doc)

**Done means:** the surface is good enough to be the public demo. Anything beyond this is v2.

## What v2 looks like (not committed)

Captured so we don't quietly drift into them mid-v1:

- FEC + LDA money trails (the EpsteinExposed "Flights" analog)
- Dossier templates ("members who broke with party >5x on healthcare last quarter")
- Subscriptions / alerts
- Bill lifecycle viz (only after committee data is rich enough)
- Multi-hop graph queries → Apache AGE / Neo4j evaluation
- State legislatures
- Backfill to 1995

## Risks carried forward from scoping

Tracked here so each phase's plan can re-check them:

- **Staleness.** Each ingest source needs a daily refresh cadence wired up before its surface goes public. Phase plans must specify cadence + failure visibility.
- **Editorial neutrality.** Phase 7's ranking surfaces are the highest-risk; Phase 3's party-line stat is the first place this bites. Define the stat with a named methodology, not a vibe.
- **Mutability mismatch.** ADR 0006 handles the storage side. UI side: surface `fetched_at` on every Bill/Vote page so staleness is legible.
- **Viz-vs-utility.** No network graphs in v1. If the urge appears mid-phase, push it to v2.

## How to pick this up

Next session should start by drafting the three ADRs (0006, 0007, 0008). Those are blocking for Phase 1. After they land, Phase 1 gets its own `docs/plans/phase-1-members.md` via the `implementation-plan` skill.
