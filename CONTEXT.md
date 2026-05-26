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
- **Bill** — a piece of legislation introduced in either chamber. Identified by the tuple `(congress, bill_type, bill_number)`; Concord's internal `bill_id` flattens that to `"<congress>-<type>-<number>"` (e.g. `"119-hr-1234"`).
- **Bill type** — one of eight codes: `hr`, `hres`, `hjres`, `hconres`, `s`, `sres`, `sjres`, `sconres`. Canonical form is lowercase; the API returns uppercase and Concord canonicalizes on ingest.
- **Sponsor** — the single Member who introduced a Bill. 1:1 with Bill (modeled as a column on the `bills` row, not a separate table).
- **Cosponsor** — a Member who formally added their name to an existing Bill after introduction. M:N with Bill via `bill_cosponsors`; carries `sponsorship_date` and a nullable `sponsorship_withdrawn_date`.
- **Bill action** — one event in a Bill's legislative history (e.g. "Referred to the Committee on Foreign Relations", "Passed House"). Many per Bill; stored verbatim and dimmed at display for routine procedural noise.
- **Policy area** — a single CRS-assigned top-level subject for a Bill (e.g. "Health", "Armed Forces and National Security"). Distinct from the multi-valued **legislative subjects** that live in `bill_subjects`.

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

## Storage

- **Canonical raw store** — `proceedings.jsonl`. One Proceeding per line. Append-only. Generated by the scraper. Source of truth.
- **Derived store** — `proceedings.db` (SQLite). Indexed, queryable, rebuildable from the canonical store. Holds proceedings + FTS5 + vec + (future: entities + mentions).
