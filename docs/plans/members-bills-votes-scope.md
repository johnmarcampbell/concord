# Members, Bills, Votes — scoping brainstorm

**Status:** scoping conversation, not a plan yet. Captures a wrap session on extending Concord beyond Proceedings. Needs grilling against [CONTEXT.md](../../CONTEXT.md) and the existing ADRs before any of this becomes committed work.

**Date:** 2026-05-25.

## The ask

Surface, alongside Proceedings:

- **Members** of Congress
- **Bills**
- **Votes**
- A knowledge graph connecting them — "Member X cosponsored Bill Y", "Vote Z determined the outcome of Bill Y", etc.

## Inspiration

[epsteinexposed.com](https://epsteinexposed.com) and its pipeline at [stonesalltheway1/Epstein-Pipeline](https://github.com/stonesalltheway1/Epstein-Pipeline). What's worth borrowing:

- Entity profile pages with aggressive cross-linking (every name/document/flight is a link)
- "Most connected persons" ranking patterns
- Dual storage: Postgres+pgvector for semantic/tabular, Neo4j for the multi-hop graph traversal stuff
- Universal search across all entity types

What to *not* borrow: leading with the network graph viz. They photograph well, mostly underperform a good filtered list.

## Entity model (proposed)

Three peers to Proceeding, plus two I'd argue belong in v1:

- **Member** — canonical key is Bioguide ID
- **Bill** — keyed by `(congress, bill_type, bill_number)`
- **Vote** — keyed by `(chamber, congress, session, roll_number)`
- **Committee** — the *path* of a bill (referred-to → markup → reported-out) is most of the legislative story
- **Amendment** — often the real news is the amendment, not the underlying bill

Edges:

- Member `sponsored` Bill, `cosponsored` Bill, `voted-position` Vote
- Bill `referred-to` Committee, `amended-by` Amendment, `subject-of` Vote
- Member `sits-on` Committee (time-bounded, chair/ranking-member role)
- Vote `determines-outcome-on` Bill/Amendment

Bridge to existing Concord state:

- Mention edges from Proceeding → Member / Bill — this is the current Stage 3 "Enrich" stage from [CONTEXT.md](../../CONTEXT.md), extracted from proceeding text

## How this slots into the existing pipeline

The current pipeline is *one source* (api.congress.gov /congressional-record) → *one entity* (Proceeding). The proposal expands to *multiple sources* (/member, /bill, /committee, /amendment, /house-vote, /senate-vote) → *multiple peer entities*. That's a real architectural shift, not just an extension of Stage 3.

Two ways to slot it in:

1. **Parallel pipelines, shared DB.** Each entity type gets its own Stage 0 + Stage 1. Stage 2 (Index) and Stage 3 (Enrich) operate across the unified DB.
2. **Generalize the existing stages.** Stage 0 becomes "scrape any congress.gov endpoint to JSONL"; Stage 1 becomes "load JSONL → typed tables".

Tension to resolve: Proceedings are immutable once published (granule_id is a stable PK, JSONL is append-only per [ADR 0002](../adr/0002-jsonl-as-canonical-raw-store.md)). Bills *mutate* — they get amended, change status, accumulate cosponsors. The append-only contract doesn't trivially carry over. Option 1 lets Bills/Votes have a different storage contract without retrofitting Proceedings.

## What api.congress.gov gives you, and what it doesn't

**Have:** `/member`, `/bill`, `/committee`, `/amendment`, `/house-vote`, `/senate-vote`. Bills carry sponsor/cosponsor lists. Members carry full term history.

**Thin or missing:**

- Full **roll-call detail** is cleaner from clerk.house.gov / senate.gov bulk XML than from the API
- No **campaign finance** — FEC API + OpenSecrets fill that gap (the EpsteinExposed "Flights" equivalent — a public-data money trail tied to people)
- No **lobbying disclosures** — LDA filings via House + Senate clerk
- No first-class **news coverage** — but if this is ever Post-adjacent, that's the unique-to-Post advantage

## Surfacing patterns, in increasing ambition

1. **Entity pages with hard cross-linking.** Member page = profile + sponsored/cosponsored + recent votes + committees + party-line alignment. Bill page = sponsors + cosponsor list + vote history + committee path. Underrated; ships fast.
2. **Bill lifecycle timeline.** Intro → committee → markup → floor → other chamber → conference → signature. Highly visual.
3. **Pre-computed dossiers.** "Members who broke with party >5x last quarter on healthcare." This is the kind of thing journalists actually want.
4. **Network visualizations.** Cosponsorship + committee co-membership. Earn them with a *specific* multi-hop question.
5. **Conversational layer.** RAG over bill text + the Congressional Record (Concord already has the latter indexed per [ADR 0005](../adr/0005-chunks-as-unit-of-retrieval.md)). TableRAG fits roll-call tables almost perfectly.
6. **Alerts/subscriptions.** "Tell me when Senator X votes against party."

## Storage

SQLite + FKs + materialized views gets you 90% of "knowledge graph" before you need a graph DB. Don't migrate off SQLite ([ADR 0003](../adr/0003-sqlite-as-derived-store.md)) prematurely. The threshold is when >2-hop traversals become load-bearing — "members who cosponsored together >N times across bills of topic X" type queries. At that point: Apache AGE on Postgres, or a separate Neo4j store.

## Risks / tradeoffs to flag explicitly

- **Staleness.** Congress moves daily. Ingest has to be solid before the surface goes public, or trust evaporates on the first stale vote count.
- **Editorial neutrality.** EpsteinExposed has a baked-in political valence; that's appropriate for its topic. A congressional product has to be politically neutral by construction. Affects ranking algorithms, "most connected" lists, dossier templates.
- **Mutability mismatch.** Proceedings are immutable; Bills mutate. The storage contract for the latter has to handle versioning / state transitions.
- **Viz-vs-utility.** Network graphs photograph well, mostly underperform a good filtered list. Resist leading with them.

## Open questions (the conversation to continue)

1. **Audience and surface.** Internal tool? Public site? A feature inside an existing product?
2. **Historical depth.** Current Congress only, or back to 1995 (the CREC floor)?
3. **Bill *text* in v1, or metadata only?** Bill text + RAG is interesting given Concord's existing vector infra; also expensive.
4. **Money trails (FEC, lobbying)** in or out of scope?
5. **Federal only, or state legislatures too?**
6. **Does this stay in the Concord repo, or fork into a new project?** "Concord" implies records of proceedings; expanding scope may warrant a different framing where Concord becomes one of several ingest paths.

## How to pick this up

Start a fresh session in `~/code/concord` and open this file. The wrap session left off at the open questions — that's the natural place to resume.
