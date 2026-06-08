# Concord — domain glossary

The vocabulary the project's code, docs, and conversations must agree on.
Implementation lives elsewhere; this file is a dictionary, not a spec.

## Source domain (Congressional Record)

The vocabulary in this section covers the Congressional Record sub-domain — the original scope of the project. The broader Congress sub-domain (Members, Bills, Votes, …) is defined under "Entities" below.


- **Congressional Record** — the official record of the proceedings and debates of the U.S. Congress, published daily that Congress is in session. Data source.
- **Issue** — one day's edition of the Congressional Record. Identified by `(volume, issue_number)`. Carries metadata: `issue_date`, `congress`, `session`.
- **Section** — a top-level grouping within an issue: `Senate Section`, `House Section`, `Extensions of Remarks Section`, `Daily Digest`.
- **Article** — one discrete item within a section. Has `title`, `start_page`, `end_page`, a Formatted Text URL and a PDF URL on congress.gov.
- **Granule ID** — the stable identifier for an article, e.g. `CREC-2026-05-22-pt1-PgD551-6`. Embedded in both the text and PDF URLs. Used as the primary key across the entire pipeline; dedup is keyed on it.
- **Proceeding** — the canonical output record of the scrape pipeline. One article's metadata + plain text + fetch timestamp. The unit of analysis throughout the project.

## Entities (broader Congress sub-domain)

- **Member** — a person who has served in the U.S. Congress. The canonical actor entity. Identified by Bioguide ID. "Former" vs "current" is a query filter, not a separate term.
- **Bioguide ID** — the stable primary key for a Member, assigned by the Biographical Directory of the United States Congress. Example: `O000172`. Never reused, never rewritten.
- **Term** — one continuous service period for a Member in one chamber. A Member has 1..N terms. Keyed by `(bioguide_id, congress, chamber)`. Carries party, state, district (House only), and start/end dates.
- **Chamber** — `house` or `senate`. A property of the Term, not the Member: Members can move between chambers across terms.
- **Party** — recorded per-Term, not per-Member. Members can change parties between terms; per-Term storage preserves historical state.
- **Bill** — a piece of legislation introduced in either chamber. Identified by the tuple `(congress, bill_type, bill_number)`; Concord's internal `bill_id` flattens that to `"<congress>-<type>-<number>"` (e.g. `"119-hr-1234"`). In code, "Bill" is conceptual — the aggregate that spans all six endpoints listed under [ADR 0009](docs/adr/0009-multi-endpoint-entities-split-jsonl.md). The wire-shape model for the identity endpoint is `BillDetail`; see [ADR 0018](docs/adr/0018-pydantic-at-the-load-boundary.md).
- **Bill type** — one of eight codes: `hr`, `hres`, `hjres`, `hconres`, `s`, `sres`, `sjres`, `sconres`. Canonical form is lowercase; the API returns uppercase and Concord canonicalizes on ingest.
- **Sponsor** — the single Member who introduced a Bill. 1:1 with Bill (modeled as a column on the `bills` row, not a separate table).
- **Cosponsor** — a Member who formally added their name to an existing Bill after introduction. M:N with Bill via `bill_cosponsors`; carries `sponsorship_date` and a nullable `sponsorship_withdrawn_date`. The wire-shape model is `BillCosponsor` per the endpoint-naming rule in [ADR 0018](docs/adr/0018-pydantic-at-the-load-boundary.md); the bare "Cosponsor" stays as the domain term in prose.
- **Bill action** — one event in a Bill's legislative history (e.g. "Referred to the Committee on Foreign Relations", "Passed House"). Many per Bill; stored verbatim and dimmed at display for routine procedural noise.
- **Policy area** — a single CRS-assigned top-level subject for a Bill (e.g. "Health", "Armed Forces and National Security"). Distinct from the multi-valued **legislative subjects** that live in `bill_subjects`.
- **Bill Brief** — a short, readable distillation of a Bill that sits on top of the scraped data. Two layers (see [ADR 0020](docs/adr/0020-bill-brief-llm-summary.md)): a deterministic **fact pack** rendered verbatim, plus a single LLM-authored **executive summary**. The executive summary is the only generated content and the only conditionable content; everything factual is computed, not generated.
- **Fact pack** — the deterministic, always-neutral set of facts about a Bill that grounds its Brief: identity, sponsor, cosponsor counts + party split, dates, subjects, action/vote counts, and the most recent CRS summary (shown verbatim, labeled with its stage). Computed in code, never by the model; fed to the model as ground truth it may narrate but not contradict. In code: `concord.brief.BriefFacts`.
- **Lens** — the optional free-text emphasis a user supplies when generating a Bill Brief ("frame for a budget-committee audience"). Steers the executive summary's emphasis and framing only — never the facts. The Brief's honesty rules sit above the lens in priority. Stored verbatim with the generated brief; an empty lens is the neutral, cacheable default.
- **Vote** — one recorded roll-call decision in a chamber. Identified by the tuple `(chamber, congress, session, roll_number)`; Concord's internal `vote_id` flattens that to `"{chamber}-{congress}-{session}-{roll}"` (e.g. `"house-119-1-240"`). May be on a Bill, on an Amendment to a Bill, or procedural (Speaker election, motion to adjourn, journal approval).
- **Roll-call number** — the chamber's per-session sequential identifier for a recorded vote. Resets to 1 at the start of each `(congress, session)` slot. Combined with chamber/congress/session it forms the Vote's natural key.
- **Session** — the half-Congress that a Vote (or other dated event) falls in. A Congress has two sessions, numbered `1` and `2`; in practice session 1 covers an odd calendar year and session 2 covers the following even year.
- **Vote question** — the free-text description of what was being decided on a roll call (e.g. "On Passage of the Bill", "On Agreeing to the Amendment", "Election of the Speaker"). Always populated; the source of truth for procedural votes that have no Bill or Amendment subject.
- **Vote position** — one Member's recorded choice on one Vote. For standard votes the value is `"Yea"` / `"Nay"` / `"Present"` / `"Not Voting"`; for election votes (e.g. Speaker) it's a candidate's surname. Stored in `vote_positions` keyed by `(vote_id, bioguide_id)`, with `vote_party` denormalized from the API payload.
- **Party Unity Score** — per-Member, per-Congress, per-chamber: the share of *party-unity votes* (votes where a majority of one party opposed a majority of the other) on which the Member voted with their party's majority. Modeled on the CQ Almanac's methodology. Scored independently per chamber so Senate and House majorities aren't pooled. See [ADR 0011](docs/adr/0011-party-unity-score-methodology.md) and `/about/methodology#party-unity` for the precise definition.
- **LIS member ID** — Senate-internal stable identifier for a senator, used in senate.gov LIS (Legislative Information System) XML feeds. Format `"S\d+"` (e.g. `"S428"`). Concord does not store LIS IDs in SQLite; they are used only as a transient join key inside the Senate vote loader, where the LIS-keyed XML position rows are bridged to Bioguide IDs via the senators_cfm.xml roster.
- **`member_full`** — Senate display string in the form `"Surname (Party-State)"` (e.g. `"Alsobrooks (D-MD)"`). Appears in both senate.gov vote-detail XML and `senators_cfm.xml`. Concord uses it as the bridge string for LIS↔Bioguide resolution at vote-load time; the Senate detail XML keys positions by `lis_member_id` rather than Bioguide ID, so the bridge is required to land Senate `vote_positions` rows on the same `bioguide_id` PK as the House.

## Pipeline stages

- **Stage 0 — Scrape**: produces the canonical raw store from the live API. (See ADR 0002.)
- **Stage 1 — Load**: turns the canonical raw store into a database mirror — metadata and full text only, no derived indexes.
- **Stage 2 — Index**: builds the derived indexes (chunks + FTS5 + vector embeddings) from the database mirror. Regenerable. (See ADR 0005.)
- **Stage 3 — Enrich** *(future)*: extracts entities (people, bills, ...) and writes them to entity / mention tables.

## Search vocabulary

- **Keyword search** — lexical search against the FTS5 index. BM25 ranking, phrase queries, NEAR, snippets.
- **Semantic search** — vector similarity search against chunk embeddings.
- **Hybrid search** — combined ranking that mixes keyword and semantic scores in a single query.
- **RRF (Reciprocal Rank Fusion)** — the strategy Concord uses to combine FTS5 and vector results into a single ranking. Operates on ranks rather than raw scores.
- **Chunk** — a span of a Proceeding's text sized for one embedding. Chunks are Concord's *unit of retrieval*: both keyword and semantic indexes operate on chunks, and results roll up to Proceedings for display. (See ADR 0005.)

## Components

- **Scraper** — Python. Stage 0. Writes JSONL.
- **Pipeline** — Python. Stages 1, 2, (3). Reads JSONL, writes SQLite.
- **Web** — Python (FastAPI + Jinja2 + HTMX), Tailwind for styling. Reads SQLite, serves the public demo. Single process, same repo. See ADR 0001.

## Models

The shapes data takes in code as it crosses pipeline boundaries. See [ADR 0018](docs/adr/0018-pydantic-at-the-load-boundary.md) for the rules these terms underpin.

- **Wire-shape model** — a Pydantic model whose structure mirrors a single response from a source the project does not control: one Congress API endpoint, one senate.gov XML document, etc. Field names, optionality, and nesting follow the upstream contract verbatim, modulo two cosmetic adaptations (camelCase→snake_case via `alias_generator`, Pydantic's built-in JSON-native type parsing). Custom `@field_validator` semantic shims do not belong on a wire-shape model. The only sanctioned constructor is `@classmethod from_congress_api(payload) -> Self` (or `from_<source>` for non-Congress sources). Wire-shape models are named after the endpoint that produced them (`BillDetail`, `Cosponsor`, `ParsedVoteDetail`). *Project vocabulary; the compound is built from the industry-standard "wire format" but is specific to this codebase.*
- **Domain model** — the Pydantic model the rest of the codebase operates on after any normalization. When the wire shape already aligns with what the application wants, the wire-shape model *is* the domain model — one class plays both roles. This is the common case (`BillDetail`, `Member`, House `Vote`). When the wire shape is awkward, a separate domain model is defined and the wire-shape model is projected into it via a plain function. The Senate vote path is the worked example: `ParsedVoteDetail` (XML wire shape) → `Vote` (domain model).
- **Snapshot envelope** — the on-disk JSONL persistence shape for a mutable entity: `{fetched_at, key, payload}`. Represented in code as `Snapshot[T]`, a Pydantic generic parameterized by the wire-shape model carried in `payload`. `fetched_at` is the capture timestamp; `key` is the natural-key composite used for dedup and upsert; `payload` is wire-shape data (or a sub-section for multi-endpoint entities per [ADR 0009](docs/adr/0009-multi-endpoint-entities-split-jsonl.md)). Proceedings predate the envelope and persist flat per [ADR 0002](docs/adr/0002-jsonl-as-canonical-raw-store.md). The word *envelope* describes the shape; the class is `Snapshot[T]`.

## Observability

- **Scrape Run** — the record of one Stage 0 execution for one entity type: a single `scrape <entity>` pull, or the Stage-0 phase of a `run <entity>` invocation. Carries timing (`started_at`, `ended_at`), the per-endpoint count of *successful* network requests, and the set of errors encountered. Born only where the network is — `load`/`index` produce no Scrape Run. Distinct from the `concord run` command, which chains all stages.
- **Run Event** — one detailed record of a logical network request that encountered **at least one non-success attempt** within a Scrape Run. Carries the endpoint bucket, the (capped) list of failed attempts (HTTP status or transport-error class, message, attempt number), and a `final_status` of `resolved` (a later retry succeeded) or `failed` (terminal). A request that succeeds on the first attempt emits no Run Event — the happy path is aggregated as counts only. **Rate-limit (429) attempts count as errors here**, even though the client retries them indefinitely rather than aborting: a rate-limit-aware client hitting a 429 reflects our own request-budget mismanagement, which is precisely the signal worth surfacing. "Error event" in the ledger therefore means "≥1 non-success attempt", not "the request failed". The attempts list is capped with an overflow count so a heavily rate-limited request can't store an unbounded array. Currently request-failures are the only event kind; the term is deliberately general.
- **Endpoint bucket** — the normalized, templated identity of a network endpoint, used as the aggregation key for a Scrape Run's success counts and Run Events (e.g. `api:bill/detail`, `text:article`, `senate:detail`). Concrete per-resource URLs (`/bill/119/hr/1234`) are never the key — that would defeat aggregation. An unrecognized path falls to a loud `<source>:unmatched` bucket rather than being silently mis-grouped.
- **Load Validation Failure** — one upstream payload that violated a model contract at the Stage 1 load boundary — i.e. an `X.from_congress_api(...)` rejection (`ValidationError` / `ValueError` / `KeyError`). Recorded per occurrence, keyed to the entity and the field that drifted, so contract drift can be aggregated ("which once-required field started coming back null?"). Scoped to the ten parsed entities: Bill, Cosponsor, Bill action, legislative subject, title, summary, Vote, Vote position, Member, Term. Distinct from a **Run Event** (a Stage 0 *network* error in the unreconstructable Scrape Run ledger) and from a "malformed line" (local JSONL corruption, not upstream drift). Because it is a deterministic function of the canonical raw store, it is rebuildable — it lives in a mirror table that a re-load converges, not the record-table ledger. See [ADR 0023](docs/adr/0023-load-validation-failures-mirror-table.md).

## Storage

- **Canonical raw store** — `proceedings.jsonl`. One Proceeding per line. Append-only. Generated by the scraper. Source of truth.
- **Derived store** — `proceedings.db` (SQLite). Indexed, queryable, rebuildable from the canonical store. Holds proceedings + FTS5 + vec + (future: entities + mentions).
- **Mirror table** — a SQLite table that is a deterministic projection of the canonical raw store: drop it, re-run the relevant Stage 1/2 pass, get a byte-identical result. Most tables (`proceedings`, `bills`, `votes`, the indexes…). The [ADR 0002](docs/adr/0002-jsonl-as-canonical-raw-store.md) "SQLite is rebuildable" contract applies to these.
- **Record table** — a SQLite table holding Concord's own application state / generated artifacts, which originate here rather than upstream and are **not** rebuildable from JSONL (e.g. `bill_briefs`). A JSONL re-derivation must not destroy them. See [ADR 0019](docs/adr/0019-mirror-tables-vs-record-tables.md), which draws this line as an amendment to ADR 0002.
