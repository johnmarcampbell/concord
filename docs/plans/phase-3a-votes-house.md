# Phase 3a — Votes (House) ingest

> Ingest House roll-call votes — metadata, totals, bill/amendment subject, and full per-member positions — from `api.congress.gov` into Concord, surface them as `/votes/house/{congress}/{session}/{roll}` profile pages, federate Vote history onto the Bill profile, add a "Recent votes" section + a CQ-style party-unity score to each House Member's profile, and ship the methodology page that documents the stat.

## Source

- Roadmap: [docs/plans/members-bills-votes-roadmap.md](./members-bills-votes-roadmap.md) (Phase 3 section, lines 70–77)
- Sibling plan (follow-up): **Phase 3b — Votes (Senate)** — to be planned after this lands; covers senate.gov LIS XML ingest end-to-end.
- Phase 2a exemplar (template for this plan's structure and patterns): [docs/plans/phase-2a-bills-basic.md](./phase-2a-bills-basic.md)
- Phase 1 exemplar (template for scraper / loader / indexer module shape): [docs/plans/phase-1-members.md](./phase-1-members.md)
- Spike that reshaped the phase: [docs/plans/.spike-votes-api.md](./.spike-votes-api.md) and [docs/plans/.spike-votes-api-findings.md](./.spike-votes-api-findings.md). **Both files should be deleted once this plan is approved** — their findings are reflected below.

## Context

Concord's roadmap calls Phase 3 "Votes." The original sketch described one phase ingesting `/house-vote` + `/senate-vote` from `api.congress.gov`, augmented by clerk.house.gov / senate.gov bulk XML for full per-member positions. A spike against the live API (see findings file linked above) surfaced two facts that reshape the work:

1. **api.congress.gov has no Senate vote endpoint at all** — `/v3/house-vote/{c}/{s}/{roll}` works but every Senate variant (`/senate-vote`, `/senate-rollcall-vote`, …) returns 404. The roadmap's "API is thin" phrasing turned out to mean "absent for Senate." Senate work must read senate.gov LIS XML from the start.
2. **The House API delivers full per-member positions** at `/v3/house-vote/{c}/{s}/{roll}/members` (Bioguide-keyed, one ~100KB call per roll). The clerk.house.gov XML augmentation the roadmap named is not needed — the API has everything Phase 3 wants for the House half.

Together these collapse the roadmap's implied "metadata vs positions" split and replace it with a **chamber split**: Phase 3a covers House end-to-end via the API; Phase 3b covers Senate end-to-end via senate.gov LIS XML (Legislative Information System — the Senate's internal data interchange format, exposed publicly per-roll at `https://www.senate.gov/legislative/LIS/roll_call_votes/...`). Recorded in [ADR 0010](../adr/0010-votes-phased-by-chamber.md) (new, created with this plan).

A **Vote** is one recorded roll-call decision in a chamber. It is keyed by the tuple `(chamber, congress, session, roll_number)`. Concord's internal `vote_id` flattens that to `"{chamber}-{congress}-{session}-{roll}"` (e.g. `"house-119-1-240"`). A vote may be on a **Bill** (passage, motion to recommit), on an **Amendment** to a Bill, or on something else entirely (Speaker election, motion to adjourn, journal approval). The free-text `vote_question` field always carries the API's description of what was decided. New terms are added to the **Entities** section of [CONTEXT.md](../../CONTEXT.md) as part of this plan.

The roadmap calls out two risks Phase 3 has to address head-on:

- **Editorial neutrality on party-line alignment stats.** The Member-page party-line summary is the first place this risk bites. We commit to a **Party Unity Score** modeled on the *Congressional Quarterly* (CQ) Almanac's long-running methodology — published annually since 1953 and widely cited in political science — defined in [ADR 0011](../adr/0011-party-unity-score-methodology.md) (new, created with this plan), rather than an unnamed vibe number.
- **Staleness.** Vote refresh is a daily-cadence operator-driven `concord run votes` job, same stance as Phase 2a. The Vote page header surfaces `Updated: {fetched_at}` so staleness is legible.

## Goals

1. The Tier 1 scraper fetches, for every House roll-call vote in Congresses 117, 118, and 119 (six `(congress, session)` slots — three congresses × two sessions), two endpoints per vote — the list endpoint `/v3/house-vote/{c}/{s}` (stubs, for discovery; not persisted) and the detail endpoint `/v3/house-vote/{c}/{s}/{roll}` (full vote record). For every detail fetched, also fetch `/v3/house-vote/{c}/{s}/{roll}/members` (the per-member positions sub-endpoint). Appends one [ADR 0006](../adr/0006-snapshot-on-fetch-for-mutable-entities.md) snapshot envelope per detail fetch to `data/house_votes.jsonl` and one per members fetch to `data/house_vote_positions.jsonl`.
2. The loader projects the latest snapshot per `(chamber, congress, session, roll_number)` into a `votes` SQLite table and the latest members snapshot into `vote_positions` (one row per `(vote_id, bioguide_id)`).
3. The indexer (Stage 2) populates `votes.is_party_unity` (a boolean denormalized onto each vote) and writes the `member_party_unity` table (denominator + numerator per `(bioguide_id, congress)`). No FTS5 over votes in this phase — see Non-goals.
4. The web app serves:
   - `GET /votes/{chamber}/{congress}/{session}/{roll}` — Vote profile (header + result block + subject + full position roster). Renders `chamber='senate'` rolls with a "(Phase 3b)" placeholder body if the route is hit before 3b ships.
   - `GET /votes` — paginated browse, default sort `start_date DESC`, filters for `chamber`, `congress`, `result`, `vote_kind`, `bill` (by bill_id substring). 50 per page.
5. The existing `/bills/{c}/{t}/{n}` profile gets a **Vote history** section: every roll where `votes.bill_id` OR `votes.amendment_id` (resolved to its underlying bill via Phase 4 once that lands; for now `amendment_id`'s bill linkage is derived from the same API payload via a stored `amendment_underlying_bill_id` column — see [Approach > Storage shape](#storage-shape)) matches. Replaces the existing "Vote history (Phase 3)" placeholder.
6. The existing `/members/{bioguide_id}` profile gets two new sections for House members:
   - **Recent votes** — latest 25 votes by date desc; shows date, identifier, question (truncated), position, result.
   - **Party-unity score** — formatted as "Voted with [Republican|Democratic] majority on N% of party-unity votes (X/Y in 119th Congress)" plus a small history strip for earlier Congresses. Independents get a muted "no party-unity score (Independent)" note. Members with <10 party-unity votes in a Congress get a "(not enough votes yet)" note. A `?` icon links to `/about/methodology#party-unity`.
   - Senate members continue to show a "(Phase 3b)" muted placeholder for both sections.
7. A new static **methodology page** at `/about/methodology` ships in 3a, documenting the party-unity score definition. Single page; not a CMS. Anchor target `#party-unity` is the only anchor in scope.
8. The existing `/search` route does **not** federate Votes in 3a (no FTS5 index — see Non-goals). The bare-identifier redirect path (`HR 1234` → bill) is unchanged.
9. CLI follows [ADR 0007](../adr/0007-parallel-pipelines-per-entity.md): `concord scrape votes`, `concord load votes`, `concord index votes`, `concord run votes`. Every command takes `--limit N`. Every command takes `--chambers house,senate` defaulting to `house` only in 3a (with senate becoming a no-op that prints "Senate ingest lands in Phase 3b — skipping.").
10. The code layout matches ADR 0007: `src/concord/scraper/votes.py` (one entry point `scrape_house`; `scrape_senate` lands in 3b alongside it) and `src/concord/pipeline/{load,index}_votes.py`.
11. Vote→Bill / Vote→Amendment / Vote→Member columns store bare-TEXT identifiers (no `REFERENCES`) so ingest is robust to gaps in any peer table.
12. Two new ADRs land with this work: [ADR 0010 — Votes phased by chamber, not metadata-vs-positions](../adr/0010-votes-phased-by-chamber.md) and [ADR 0011 — Party Unity Score methodology](../adr/0011-party-unity-score-methodology.md).
13. All existing tests continue to pass.

## Non-goals

1. **Senate votes.** Zero Senate rows are loaded by 3a. `votes.chamber` allows `'senate'`; no row will satisfy the check until 3b lands. The CLI `--chambers senate` switch is wired as a no-op in 3a. senate.gov LIS XML is not parsed; the Bioguide↔LIS-ID mapping is not built.
2. **Vote FTS5 / Bills federation in /search.** `vote_question` is a small free-text field; the existing `/search` already grew last phase, and there is no clear query pattern that wants it (HR-number lookups already redirect via the bare-identifier path). Phase 7 may add it if it earns the slot. No `votes_fts` virtual table is created in 3a.
3. **Amendment entity table or `/amendments/...` route.** Phase 4. 3a stores `votes.amendment_id` as bare TEXT and renders amendment-vote rows with the amendment-vote affordances (a chip noting "Amendment HAMDT 85"), but there is no Amendment profile page to link to.
4. **Cosponsor / committee / lobbying-money cross-cuts.** Out of phase.
5. **Vote text search.** No tokenized full-text index.
6. **Auto-release / cron scheduling for vote refresh.** Same stance as Phase 1 and Phase 2a — the operator runs `concord run votes` daily out of band.
7. **Backfill before Congress 117.** The API only carries House votes back to Congress 116; the roadmap scopes to 117–119. 116 is left for a future backfill if useful.
8. **Per-member positions on the Vote profile page being paginated.** ~430 House members per vote; the entire roster renders inline. No infinite-scroll, no AJAX. Mitigated by tight row layout.
9. **Historical party-line trends across multiple Congresses on one chart.** Member page shows the current Congress prominently with a small history strip (numbers, not a chart). Visualizations belong to Phase 7.
10. **`Vote` as a Pydantic model with deep validation of every API field.** Cover only the fields the loader writes; pass everything else through verbatim into JSONL.
11. **A `votes_subject_kind` enum.** The `(bill_id, amendment_id)` pair already encodes the subject; an explicit enum would duplicate that information without enabling a query the columns can't answer.

## Relevant prior decisions

- [ADR 0001 — Python end-to-end for the web layer](../adr/0001-python-end-to-end-for-the-web-layer.md) — `/votes` and `/votes/{...}` extend the existing FastAPI+Jinja2+HTMX surface.
- [ADR 0002 — JSONL as canonical raw store](../adr/0002-jsonl-as-canonical-raw-store.md) — vote data goes to `data/house_votes.jsonl` and `data/house_vote_positions.jsonl`.
- [ADR 0003 — SQLite as derived store](../adr/0003-sqlite-as-derived-store.md) — `votes`, `vote_positions`, `member_party_unity` live in `data/proceedings.db`.
- [ADR 0005 — Chunks as the unit of retrieval](../adr/0005-chunks-as-unit-of-retrieval.md) — does **not** apply (votes aren't chunked).
- [ADR 0006 — Snapshot-on-fetch for mutable entities](../adr/0006-snapshot-on-fetch-for-mutable-entities.md) — Votes are recorded as snapshots; even though most votes are immutable post-close, errata corrections happen and uniformity beats special-casing.
- [ADR 0007 — Parallel pipelines per entity](../adr/0007-parallel-pipelines-per-entity.md) — `src/concord/scraper/votes.py`, `src/concord/pipeline/{load,index}_votes.py`, and the `concord <stage> votes` CLI verbs.
- [ADR 0008 — Chunks generalize beyond Proceedings](../adr/0008-chunks-generalize-beyond-proceedings.md) — not exercised.
- [ADR 0009 — Multi-endpoint entities split JSONL canonical store by sub-endpoint](../adr/0009-multi-endpoint-entities-split-jsonl.md) — 3a writes two of the four eventual files (the detail and members sub-endpoints for the House). 3b adds the Senate equivalents.
- **[ADR 0010 — Votes phased by chamber, not metadata-vs-positions](../adr/0010-votes-phased-by-chamber.md)** *(new, created with this plan)* — records the chamber-based split that replaced the roadmap's implied metadata-based one after the API spike revealed Senate has no API at all.
- **[ADR 0011 — Party Unity Score as the party-line methodology](../adr/0011-party-unity-score-methodology.md)** *(new, created with this plan)* — names CQ-style Party Unity Score as the methodology behind the Member-page stat and documents exclusion of unanimous / non-party-unity votes from the denominator.

## Relevant files and code

Files to **read** for context:

- [CONTEXT.md](../../CONTEXT.md) — new entries land in this plan: Vote, Roll-call number, Session, Vote question, Vote position, Party Unity Score. Existing Member / Chamber / Party / Bill / Amendment entries stay unchanged.
- [src/concord/api.py](../../src/concord/api.py) — `list_bills` and `get_bill_detail` (lines ~205 and ~280) are the pagination + detail-endpoint template for the four new vote-endpoint methods.
- [src/concord/models.py](../../src/concord/models.py) — `Bill` and `BillSnapshot` (~lines 350 and 430) demonstrate the camelCase aliasing and `@field_validator` lowercasing pattern.
- [src/concord/scraper/bills.py](../../src/concord/scraper/bills.py) — Phase 2a scraper. Mirror its `scrape_basic(...)` shape and `ScrapeProgressEvent` pattern.
- [src/concord/pipeline/load_bills.py](../../src/concord/pipeline/load_bills.py) — "latest snapshot per key" projection.
- [src/concord/pipeline/index_bills.py](../../src/concord/pipeline/index_bills.py) — truncate-then-repopulate pattern (3a's indexer follows it but writes computed-stat tables instead of FTS5).
- [src/concord/storage/sqlite.py](../../src/concord/storage/sqlite.py) — `_BASE_SCHEMA` (line 59) gets three new tables (`votes`, `vote_positions`, `member_party_unity`). `upsert_bill` (line 490) is the model for `upsert_vote`. The `_BILL_COLUMNS` (line 257) / `_BILL_UPSERT_SQL` (line 273) pattern repeats for votes.
- [src/concord/web/app.py](../../src/concord/web/app.py) — routes at line 159 (`/search`), 287 (`/members`), 320 (`/members/{id}`), 357 (`/bills`), 394 (`/bills/{...}`). `/votes*` routes are added; `/bills/{...}` and `/members/{...}` are extended.
- [src/concord/web/search.py](../../src/concord/web/search.py) — `search_bills` / `list_bills` / `get_bill` are templates for `list_votes`, `get_vote`, and the cross-link helpers.
- [src/concord/web/templates/bills/profile.html](../../src/concord/web/templates/bills/profile.html) — the Phase-3 placeholder card here is replaced with live data. Pattern for the new `votes/profile.html`.
- [src/concord/web/templates/members/profile.html](../../src/concord/web/templates/members/profile.html) — gets the two new sections (Recent votes, Party-unity score).
- [src/concord/web/templates/base.html](../../src/concord/web/templates/base.html) — global nav; gets a "Votes" link.
- [src/concord/cli.py](../../src/concord/cli.py) — Bills commands (the four `*_bills_command` functions added in Phase 2a) are the literal template.
- [tests/_snapshots.py](../../tests/_snapshots.py) — shared ADR 0006 envelope helper; 3a tests use it unchanged.
- [tests/test_scraper_bills.py](../../tests/test_scraper_bills.py), [tests/test_pipeline_bills.py](../../tests/test_pipeline_bills.py), [tests/test_web_bills.py](../../tests/test_web_bills.py) — patterns to follow.

Files to **create**:

- `src/concord/scraper/votes.py` — Tier 1 orchestrator (`scrape_house`); 3b adds `scrape_senate` to the same file.
- `src/concord/pipeline/load_votes.py` — Stage 1 loader; reads `data/house_votes.jsonl` + `data/house_vote_positions.jsonl`; projects to `votes` + `vote_positions`. 3b extends this file.
- `src/concord/pipeline/index_votes.py` — Stage 2 derived-stat indexer; writes `votes.is_party_unity` + `member_party_unity`. Truncate-then-repopulate.
- `src/concord/web/templates/votes/list.html`
- `src/concord/web/templates/votes/profile.html`
- `src/concord/web/templates/about/methodology.html` — methodology page; only `#party-unity` anchor in 3a.
- `src/concord/web/templates/_vote_row.html` — partial used on the Bill page's Vote history and the Member page's Recent votes (consistent row layout across surfaces).
- `tests/fixtures/api/votes/` — already populated by the spike. List, detail (bill / amendment / procedural variants), members.
- `tests/test_models_votes.py`
- `tests/test_storage_votes_sqlite.py`
- `tests/test_scraper_votes.py`
- `tests/test_pipeline_votes.py`
- `tests/test_index_votes.py`
- `tests/test_web_votes.py`
- `docs/adr/0010-votes-phased-by-chamber.md`
- `docs/adr/0011-party-unity-score-methodology.md`

Files to **modify**:

- `src/concord/api.py` — add `VOTES_PAGE_SIZE = 250` and four methods: `list_house_votes(congress, session) -> Iterator[dict]`, `get_house_vote_detail(congress, session, roll_number) -> dict`, `get_house_vote_members(congress, session, roll_number) -> dict`. (No senate-vote methods in 3a.)
- `src/concord/models.py` — add `Vote`, `VotePosition`, `VoteSnapshot`, `VotePositionsSnapshot`, plus `parse_vote_threshold(vote_type: str) -> str | None` and `vote_id_from_components(...) -> str`.
- `src/concord/storage/sqlite.py` — append three table definitions to `_BASE_SCHEMA`; add `upsert_vote`, `upsert_vote_positions` (bulk), `get_vote`, `list_vote_positions_for_vote`, `list_recent_votes_for_member`, `get_party_unity_for_member`.
- `src/concord/web/app.py` — register `/votes` and `/votes/{chamber}/{congress}/{session}/{roll}` routes; register `/about/methodology` route; extend `/bills/{...}` handler to fetch and pass vote history; extend `/members/{id}` handler to fetch and pass recent votes + party-unity score.
- `src/concord/web/search.py` — add `VoteHit`, `list_votes`, `get_vote`, `vote_history_for_bill`, `recent_votes_for_member`, `party_unity_for_member`.
- `src/concord/web/templates/bills/profile.html` — replace "Vote history (Phase 3)" placeholder with live section.
- `src/concord/web/templates/members/profile.html` — replace "Recent votes (Phase 3)" and "Party-line alignment (Phase 3)" placeholders with live sections for House members; keep Senate placeholders relabeled to "(Phase 3b)".
- `src/concord/web/templates/base.html` — add `/votes` link to global nav.
- `src/concord/cli.py` — add `scrape_votes_command`, `load_votes_command`, `index_votes_command`, `run_votes_command`. Each takes `--limit N` and `--chambers house,senate` (defaulting to `house`).
- `CONTEXT.md` — new Vote / Roll-call number / Session / Vote question / Vote position / Party Unity Score entries.

## Approach

### Domain model

A Vote is identified by `(chamber, congress, session, roll_number)`. The internal `vote_id` flattens to `"{chamber}-{congress}-{session}-{roll}"` (e.g. `"house-119-1-240"`). The flattened form is the SQLite PK and the `vote_positions` FK; URL routes still take the four components explicitly (`/votes/{chamber}/{congress}/{session}/{roll}`).

A Vote's *subject* — what was being decided — is captured by three fields stored independently:
- `bill_id TEXT` — populated whenever the API supplies `legislationType` + `legislationNumber`. On amendment votes the API returns the **underlying bill** in these fields (the spike's "amendment trap"); we capture that linkage directly.
- `amendment_id TEXT` — populated whenever the API supplies `amendmentType` + `amendmentNumber`. Formatted as `"{congress}-{amendment_type_lower}-{number}"` mirroring `bill_id` shape.
- `question TEXT NOT NULL` — the free-text `voteQuestion` field; always populated.

The combination cleanly distinguishes the four subject cases:

| Subject               | `bill_id`   | `amendment_id` | `question`                              |
|-----------------------|-------------|----------------|-----------------------------------------|
| Bill (passage, MTR)   | populated   | NULL           | "On Passage of the Bill"                |
| Amendment to a Bill   | populated   | populated      | "On Agreeing to the Amendment"          |
| Procedural / election | NULL        | NULL           | "Election of the Speaker" etc.          |
| (Treaty / nomination) | NULL        | NULL           | free text — out of scope for v1 anyway  |

A **Vote position** is one Member's recorded choice on one Vote. Stored in `vote_positions` keyed by `(vote_id, bioguide_id)`. `position` is free TEXT — not an enum — to accommodate election votes where the value is a candidate's surname (per the spike's Speaker-vote finding). `vote_party` and `vote_state` are denormalized onto the row from the API payload so party-unity computation doesn't have to join `terms`.

A **Party Unity Score** is per `(bioguide_id, congress)`. Definition lives in [ADR 0011](../adr/0011-party-unity-score-methodology.md):
- **Party-unity vote** = a Vote where a majority of Republican-party positions opposed a majority of Democratic-party positions on that Vote. Both majorities are computed from `vote_positions.vote_party` for that Vote, restricting to `position IN ('Yea', 'Nay')`. Independents do not count toward either majority.
- **Denominator** for a member = count of party-unity Votes in the Congress where the member cast `Yea` or `Nay`. Present / Not Voting excluded.
- **Numerator** = of those, count where the member's `position` agreed with their `vote_party` majority on that Vote.
- Independents (`vote_party = 'I'` across their entire Term in the Congress) get no row in `member_party_unity` — their Member page shows the muted "no party-unity score (Independent)" treatment.
- Members with denominator < 10 in a Congress get the "(not enough votes yet)" treatment — the row exists but the UI suppresses the percentage.

### Storage shape

Phase 3a writes two JSONL files. Per [ADR 0009](../adr/0009-multi-endpoint-entities-split-jsonl.md):

- `data/house_votes.jsonl` — one snapshot envelope per `/v3/house-vote/{c}/{s}/{roll}` detail fetch. Envelope:

```json
{"fetched_at": "2026-05-26T14:02:11Z",
 "key": {"chamber": "house", "congress": 119, "session": 1, "roll_number": 240},
 "payload": {... full houseRollCallVote body ...}}
```

- `data/house_vote_positions.jsonl` — one snapshot envelope per `/v3/house-vote/{c}/{s}/{roll}/members` fetch. Same `key` shape. `payload` is the full `houseRollCallVoteMemberVotes` body including the `results` array.

SQLite schema appended to `_BASE_SCHEMA` in [src/concord/storage/sqlite.py](../../src/concord/storage/sqlite.py):

```sql
CREATE TABLE IF NOT EXISTS votes (
  vote_id              TEXT PRIMARY KEY,            -- "{chamber}-{congress}-{session}-{roll}"
  chamber              TEXT NOT NULL
    CHECK (chamber IN ('house', 'senate')),
  congress             INTEGER NOT NULL,
  session              INTEGER NOT NULL
    CHECK (session IN (1, 2)),
  roll_number          INTEGER NOT NULL,
  vote_kind            TEXT NOT NULL
    CHECK (vote_kind IN ('standard', 'election')),
  start_date           TEXT NOT NULL,                -- ISO8601 with offset, as API supplies
  vote_question        TEXT NOT NULL,
  vote_type            TEXT NOT NULL,                -- raw, e.g. "2/3 Yea-And-Nay"
  threshold            TEXT
    CHECK (threshold IN ('simple_majority', 'two_thirds', 'three_fifths')
           OR threshold IS NULL),
  result               TEXT NOT NULL,                -- "Passed"/"Failed"/"Agreed to" or winner name
  yea_count            INTEGER,                      -- NULL on election votes
  nay_count            INTEGER,
  present_count        INTEGER,
  not_voting_count     INTEGER,
  bill_id              TEXT,                         -- bare TEXT, no FK
  amendment_id         TEXT,                         -- bare TEXT, no FK
  is_party_unity       INTEGER NOT NULL DEFAULT 0,   -- 0/1; populated by index_votes
  update_date          TEXT NOT NULL,                -- API's updateDate
  fetched_at           TEXT NOT NULL,
  UNIQUE (chamber, congress, session, roll_number)
);

CREATE INDEX IF NOT EXISTS idx_votes_bill         ON votes (bill_id);
CREATE INDEX IF NOT EXISTS idx_votes_amendment    ON votes (amendment_id);
CREATE INDEX IF NOT EXISTS idx_votes_date         ON votes (start_date DESC);
CREATE INDEX IF NOT EXISTS idx_votes_congress     ON votes (congress);

CREATE TABLE IF NOT EXISTS vote_positions (
  vote_id              TEXT NOT NULL,
  bioguide_id          TEXT NOT NULL,                -- bare TEXT, no FK
  position             TEXT NOT NULL,                -- free TEXT; standard: Yea/Nay/Present/Not Voting;
                                                     -- election: candidate surname
  vote_party           TEXT,                         -- "R" / "D" / "I" at time of vote
  vote_state           TEXT,                         -- state at time of vote
  PRIMARY KEY (vote_id, bioguide_id)
);

CREATE INDEX IF NOT EXISTS idx_vote_positions_member ON vote_positions (bioguide_id);

CREATE TABLE IF NOT EXISTS member_party_unity (
  bioguide_id              TEXT NOT NULL,
  congress                 INTEGER NOT NULL,
  party                    TEXT NOT NULL             -- "R" or "D"; Independents not written
    CHECK (party IN ('R', 'D')),
  party_unity_votes_cast   INTEGER NOT NULL,         -- denominator
  party_line_votes         INTEGER NOT NULL,         -- numerator
  PRIMARY KEY (bioguide_id, congress)
);
```

### Scraper structure

`src/concord/scraper/votes.py` exports one entry point in 3a — `scrape_house(client, congresses, storage_dir, *, fetched_at, sessions=(1, 2), limit=None, progress=None) -> ScrapeStats`. The driver:

1. For each `(congress, session)` pair, paginate `/v3/house-vote/{congress}/{session}` and collect roll-number stubs. Stubs are *not* persisted.
2. For each stub, fetch detail (`/v3/house-vote/{c}/{s}/{roll}`) and append one envelope to `data/house_votes.jsonl`. Then fetch the per-member positions (`.../members`) and append one envelope to `data/house_vote_positions.jsonl`. Two HTTP calls per vote.
3. **Stop after `limit` votes have been written**, if `limit` is set. The limit counts *detail* fetches; for each detail fetched, the corresponding members fetch always also runs (so JSONL files stay aligned).

Cost envelope for full 117–119 House backfill: ~5K votes × 2 calls = ~10K calls, ~2 hours at 5K req/hr. ~50 MB JSONL total (heavier from the `members.jsonl` side since each is ~100 KB).

3b adds `scrape_senate(client, ...)` to the same file alongside `scrape_house`.

### Loader

`src/concord/pipeline/load_votes.py` exports `load(storage_dir: Path, db_path: Path, *, limit: int | None = None) -> LoadStats`. The driver:

1. Read `data/house_votes.jsonl`, group by `(chamber, congress, session, roll_number)`, keep latest by `fetched_at`. For each latest snapshot:
   - Parse the `houseRollCallVote` payload into a `Vote` model.
   - Compute `bill_id` = `bill_id_from_components(congress, legislationType, legislationNumber)` if `legislationType` and `legislationNumber` are both present.
   - Compute `amendment_id` = `amendment_id_from_components(congress, amendmentType, amendmentNumber)` if `amendmentType` and `amendmentNumber` are both present.
   - Sum chamber totals from `votePartyTotal` entries. For election votes (detected via candidate-bucketed shape — see [Models](#models)), all four counts stay NULL and `vote_kind = 'election'`; otherwise `vote_kind = 'standard'`.
   - Parse `threshold` from `vote_type` via `parse_vote_threshold(vote_type)`.
   - Upsert into `votes`.
2. Read `data/house_vote_positions.jsonl`, group by `(chamber, congress, session, roll_number)`, keep latest. For each:
   - For each entry in `results`, upsert into `vote_positions` keyed `(vote_id, bioguide_id)`. `position` = `voteCast` verbatim; `vote_party` = `voteParty`; `vote_state` = `voteState`.
   - Use a bulk-INSERT-OR-REPLACE within a single transaction per vote (~430 rows).
3. Stop after `limit` votes loaded.

Idempotent. Re-running over unchanged JSONL is a no-op. 3b extends this file by adding a Senate branch that reads `data/senate_votes.jsonl` + `data/senate_vote_positions.jsonl` (different XML-derived shape, same target tables).

### Indexer

`src/concord/pipeline/index_votes.py` exports `index(db_path: Path, *, limit: int | None = None) -> IndexStats`. Truncate-then-repopulate. Two computed datasets:

1. **`votes.is_party_unity`** — `UPDATE votes SET is_party_unity = 0` followed by an `UPDATE` driven by a CTE that, for each standard vote, counts Yea/Nay positions per `vote_party` from `vote_positions` and flags the vote as party-unity iff a majority of R's voted opposite a majority of D's. Election votes always `is_party_unity = 0`.
2. **`member_party_unity`** — `DELETE FROM member_party_unity` followed by an `INSERT` driven by a query that joins `vote_positions` to `votes WHERE is_party_unity = 1 AND vote_positions.position IN ('Yea', 'Nay')`, groups by `(bioguide_id, congress)`, computes the numerator by comparing the member's position to the majority of their `vote_party` on each vote.
   - Only writes rows for `vote_party IN ('R', 'D')`.

A member's "vote_party" can differ across votes within a Congress (rare — Sinema's switch from D to I is the canonical example). The schema stores `vote_party` per position; `member_party_unity.party` is the member's *modal* `vote_party` across the Congress's party-unity votes. Members whose modal party is `I` get no `member_party_unity` row.

Wiped and rebuilt on each index run. Cost: at 3 Congresses × ~1.7K votes × ~430 members ≈ 2M rows in `vote_positions`. The party-unity CTE is bounded to standard votes (~95% of total) and runs in <30s on the v1 dataset.

### Web surface

**`/votes` index.** Template `votes/list.html`. Default sort `ORDER BY start_date DESC`. Filters: `chamber` (3a values: always `house`), `congress`, `result`, `vote_kind`, `bill` (substring match on `bill_id`). 50 per page. Row layout: chamber chip, identifier (`H 240`), date, question (truncated ~80 chars), result chip (passed/failed colored), totals `Y/N`.

**`/votes/{chamber}/{congress}/{session}/{roll}` profile.** Template `votes/profile.html`. If `chamber = 'senate'`, render a muted "Senate votes load in Phase 3b" placeholder body. Otherwise:

1. **Header.** Identifier, date, `Updated: {fetched_at}`.
2. **Result block.** Yea / Nay / Present / Not Voting counts, pass/fail chip, threshold formatted (`Required: simple majority` / `Required: 2/3` / etc.).
3. **Subject.** Branch on `(bill_id, amendment_id)`:
   - Both populated → "Amendment HAMDT 85 to [HR 3838](link)" (amendment chip + Bill link).
   - Only `bill_id` → "[HR 3424](link) — [policy area]".
   - Neither → free-text `question` displayed as the heading; small muted "Procedural / no bill subject" note.
4. **Vote question.** Always displayed verbatim.
5. **Position roster.** Full ~430 rows. Sortable client-side by party, position, state, name (CSS / small JS, no server round-trip). Each name links to `/members/{bioguide_id}`. For election votes, position values are surnames; no party / Yea-Nay grouping.

**`/votes` index page does not render an FTS-style search box.** Filters only.

**Bill page Vote history.** Replace the existing `bills/profile.html` Phase-3 placeholder. Query: `SELECT * FROM votes WHERE bill_id = ? ORDER BY start_date DESC`. Includes amendment votes (their `bill_id` is the underlying bill). Each row uses `_vote_row.html` partial. Empty state: "No votes recorded for this bill yet."

**Member page Recent votes.** Query: `SELECT v.* FROM votes v JOIN vote_positions p USING (vote_id) WHERE p.bioguide_id = ? ORDER BY v.start_date DESC LIMIT 25`. Each row uses `_vote_row.html`. Empty state: "No vote positions recorded for this member yet." Senate members in 3a always hit empty state — the row also shows a muted "Senate positions load in Phase 3b" affordance directly inside the empty state.

**Member page Party-unity score.** Query: `SELECT * FROM member_party_unity WHERE bioguide_id = ?` (returns one row per Congress). Layout:
- Current Congress prominent: "Voted with [party] majority on N% of party-unity votes (X/Y in 119th Congress)".
- History strip beneath: small `Congress 118: 89% (...)`, `Congress 117: 91% (...)`.
- If member is Independent (`vote_party = 'I'` modal across positions): muted "No party-unity score (Independent — caucuses with [D/R])" note, no number.
- If `member_party_unity` row missing for current Congress and member has positions: "Party-unity score not yet computed."
- If `party_unity_votes_cast < 10` in current Congress: "Voted on N party-unity votes — not enough for a stable score yet" (no percentage, no fraction).
- `?` icon on the right links to `/about/methodology#party-unity`.

**`/about/methodology` page.** Static template, one anchor `#party-unity`. Content drawn directly from ADR 0011 but rewritten for a public audience — define party-unity vote, define the denominator, define the numerator, name the methodology (CQ Party Unity Score), link to the ADR for the technical definition.

**Global nav.** Add a `<a href="/votes">Votes</a>` link to `base.html` between Bills and Proceedings.

### Models

`src/concord/models.py` additions:

- `Vote` — pydantic model with the columns of the `votes` table. Includes:
  - `@field_validator("chamber", "bill_type", "amendment_type", mode="before")` for lowercasing.
  - `@model_validator(mode="after")` to compute `vote_id`.
  - `@classmethod from_api_payload(cls, payload: dict, fetched_at: str) -> Vote` — handles the spike's gotchas (singular vs plural envelope keys, `votePartyTotal` summing, election-vote shape detection, amendment-trap handling).
- `VotePosition` — pydantic model for `vote_positions` rows.
- `VoteSnapshot` and `VotePositionsSnapshot` — generic ADR 0006 envelopes specialized for the two JSONL files.
- `vote_id_from_components(chamber: str, congress: int, session: int, roll_number: int) -> str`.
- `amendment_id_from_components(congress: int, amendment_type: str, amendment_number: int) -> str`.
- `parse_vote_threshold(vote_type: str) -> str | None` — case-insensitive substring match. Returns `'two_thirds'` if the string contains `"2/3"`; `'three_fifths'` if `"3/5"`; `'simple_majority'` for any of `"Yea-and-Nay"`, `"Recorded Vote"`, or `"Quorum"` (case-insensitive); `None` for unrecognized (loader logs a warning). Tested against both capitalization variants the spike found.

**Election-vote detection.** `Vote.from_api_payload` inspects the first entry of `votePartyTotal`: if it carries a `candidate` key (instead of `party` / `voteParty`), the loader sets `vote_kind = 'election'`, leaves totals NULL, and constructs `result` from the API's `result` field (which on election votes carries the winner's name verbatim).

### CLI

```
concord scrape votes [--congresses 117,118,119]
                     [--sessions 1,2]
                     [--chambers house]
                     [--storage-dir data/] [--limit N]
                     [--progress/--no-progress]
concord load votes   [--storage-dir data/] [--db data/proceedings.db] [--limit N]
concord index votes  [--db data/proceedings.db] [--limit N]
concord run votes    [scrape options] [--db ...] [--limit N]
```

`--chambers` defaults to `house` in 3a. If the user passes `senate` (or `house,senate`), the senate slice is a no-op that logs `"Senate ingest lands in Phase 3b — skipping."`. 3b will remove the no-op.

`--limit N` semantics:
- `scrape votes` — stop after N detail responses written (and their paired members responses).
- `load votes` — stop after N vote rows upserted.
- `index votes` — cap on rows processed in the party-unity CTE.
- `run votes` — passed through to scrape.

`_parse_csv` generalized in Phase 2a is reused for `--congresses`, `--sessions`, `--chambers`.

### Backfill operations

Phase 3a ships when the pipeline can scrape, load, index, and serve a `--limit`-bounded slice of House votes end-to-end. Cost envelope:

| Operation | Calls | Time @ 5K/hr | JSONL |
|---|---|---|---|
| Tier 1 backfill (117–119 House, all sessions, both endpoints) | ~10K | ~2h | ~50 MB |

Same operator-driven stance as Phase 2a — exercise against `--limit 50` locally, `--limit 500` in staging, unbounded on the VPS.

## Step-by-step plan

Eight sections — **ADRs**, **API client**, **Models & storage**, **Ingest**, **Indexer**, **CLI**, **Web — Vote pages**, **Web — cross-links & methodology**, **Verification**.

### Section 1 — ADRs

1. **Draft ADR 0010 — Votes phased by chamber.** Create [docs/adr/0010-votes-phased-by-chamber.md](../adr/0010-votes-phased-by-chamber.md) following the `docs/adr/` template. Context: roadmap implied a metadata/positions split; spike revealed Senate has no API and House has full positions, collapsing that split. Decision: phase by chamber — 3a House (API), 3b Senate (senate.gov LIS XML). Consequences: each phase has one source, asymmetric coverage between phases, party-unity score is House-only until 3b. Reject the original split.

2. **Draft ADR 0011 — Party Unity Score methodology.** Create [docs/adr/0011-party-unity-score-methodology.md](../adr/0011-party-unity-score-methodology.md). Context: roadmap calls out party-line stat as the first editorial-neutrality risk. Decision: CQ-style Party Unity Score — define party-unity vote, denominator, numerator. Consequences: requires `vote_positions.vote_party` denormalization; Independents not scored; <10-vote threshold; methodology page surfaced at `/about/methodology#party-unity`. Reject naive party-line %, reject side-by-side display.

### Section 2 — API client

3. **Add vote-endpoint constants and methods.** In [src/concord/api.py](../../src/concord/api.py): add `VOTES_PAGE_SIZE = 250`. Add three methods on `Client`:
   - `list_house_votes(congress: int, session: int) -> Iterator[dict]` — paginates `/v3/house-vote/{congress}/{session}`. Yields stubs from `houseRollCallVotes`. Honors `pagination.next`.
   - `get_house_vote_detail(congress: int, session: int, roll_number: int) -> dict` — returns the `houseRollCallVote` object from `/v3/house-vote/{c}/{s}/{roll}`.
   - `get_house_vote_members(congress: int, session: int, roll_number: int) -> dict` — returns the `houseRollCallVoteMemberVotes` object from `/v3/house-vote/{c}/{s}/{roll}/members`.

   No `list_senate_votes` etc. in 3a.

4. **Test the API client.** Extend [tests/test_api.py](../../tests/test_api.py) with one test class per new method using captured fixtures + `httpx.MockTransport`. Assert request paths, pagination termination (one page list fixture), and that the methods unwrap the outer envelope keys correctly (`houseRollCallVotes` / `houseRollCallVote` / `houseRollCallVoteMemberVotes`).

### Section 3 — Models & storage

5. **Add Vote pydantic models.** Extend [src/concord/models.py](../../src/concord/models.py):
   - `Vote` — fields per the `votes` schema. `@field_validator` lowercases `chamber`, `bill_type` (alias of `legislationType`), `amendment_type` (alias of `amendmentType`).
   - `VotePosition` — fields per the `vote_positions` schema. `position` is free `str`.
   - `VoteSnapshot`, `VotePositionsSnapshot` — generic ADR 0006 envelopes.
   - `Vote.from_api_payload(payload, fetched_at) -> Vote` — handles election-vote detection, `votePartyTotal` summing, amendment-trap (`bill_id` + `amendment_id` both set when amendment vote), threshold parsing.
   - `vote_id_from_components`, `amendment_id_from_components`, `parse_vote_threshold` helpers.

6. **Test the models.** Create [tests/test_models_votes.py](../../tests/test_models_votes.py). Cover: parsing each of the four captured fixture variants (bill / amendment / election / list); `vote_id_from_components` shape; `parse_vote_threshold` against `"Yea-and-Nay"`, `"2/3 Yea-And-Nay"`, `"Recorded Vote"`, unknown string (returns None); election-vote detection setting `vote_kind='election'` and leaving counts NULL; amendment-trap populating both `bill_id` and `amendment_id`.

7. **Add Votes schema to SqliteStorage.** Append the three table definitions (`votes`, `vote_positions`, `member_party_unity`) + indexes from [Approach > Storage shape](#storage-shape) to `_BASE_SCHEMA` in [src/concord/storage/sqlite.py](../../src/concord/storage/sqlite.py). Define `_VOTE_COLUMNS`, `_VOTE_UPSERT_SQL`, `_VOTE_POSITION_COLUMNS`, `_VOTE_POSITION_UPSERT_SQL`, `_MEMBER_PARTY_UNITY_COLUMNS`, `_MEMBER_PARTY_UNITY_UPSERT_SQL`.

8. **Add storage methods.** In `src/concord/storage/sqlite.py`:
   - `upsert_vote(vote: Vote, *, fetched_at: str) -> None`
   - `upsert_vote_positions(vote_id: str, positions: Iterable[VotePosition]) -> int` — bulk; wraps a single transaction. Returns row count.
   - `get_vote(vote_id: str) -> sqlite3.Row | None`
   - `list_vote_positions_for_vote(vote_id: str) -> list[sqlite3.Row]`
   - `list_recent_votes_for_member(bioguide_id: str, limit: int = 25) -> list[sqlite3.Row]` — joins `vote_positions` to `votes`.
   - `get_party_unity_for_member(bioguide_id: str) -> list[sqlite3.Row]` — one row per Congress.
   - `vote_history_for_bill(bill_id: str) -> list[sqlite3.Row]`

9. **Test storage.** [tests/test_storage_votes_sqlite.py](../../tests/test_storage_votes_sqlite.py): upsert idempotency on `vote_id`; the `chamber` / `session` / `vote_kind` / `threshold` / `party` CHECK constraints reject bad values; `vote_positions` PK uniqueness; bulk position upsert.

### Section 4 — Ingest

10. **Use the existing fixtures.** The spike already saved fixtures under `tests/fixtures/api/votes/` (see findings file). Confirm all six are present (`list_house_119_1.json`, `list_senate_119_1.json` [404 evidence], `detail_house_119_1_240.json`, `detail_house_119_1_subject_amendment.json`, `detail_house_119_1_subject_procedural.json`, `members_house_119_1_240.json`). Capture one additional `members` fixture for the amendment-vote roll if not already present (the loader test wants paired detail+members for the amendment case).

11. **Build the Votes scraper (Tier 1).** Create [src/concord/scraper/votes.py](../../src/concord/scraper/votes.py) exporting `scrape_house(client, congresses, storage_dir, *, fetched_at, sessions=(1,2), limit=None, progress=None) -> ScrapeStats`. Per [Approach > Scraper structure](#scraper-structure). Emit `ScrapeProgressEvent(chamber='house', congress, session, votes_seen, votes_written)` once per `(congress, session)` pair. Verify in [tests/test_scraper_votes.py](../../tests/test_scraper_votes.py):
    - Only the two House JSONL files are written.
    - One detail envelope per `votes_written`, one members envelope per `votes_written` — counts match.
    - `--limit` honored.
    - `chamber='house'` in every key.

12. **Build the Votes loader.** Create [src/concord/pipeline/load_votes.py](../../src/concord/pipeline/load_votes.py) exporting `load(storage_dir, db_path, *, limit=None) -> LoadStats`. Reads both files, groups by key, keeps latest per file, upserts `votes` and `vote_positions`. `LoadStats` carries `votes_written, positions_written, snapshots_read, malformed`. Verify in [tests/test_pipeline_votes.py](../../tests/test_pipeline_votes.py):
    - Basic load with one vote (bill).
    - Amendment vote — both `bill_id` and `amendment_id` populated.
    - Election vote — `vote_kind='election'`, counts NULL, `position` carries candidate surname.
    - Two snapshots same key — latest wins.
    - `--limit` honored.
    - Re-run is idempotent.

### Section 5 — Indexer

13. **Build the party-unity indexer.** Create [src/concord/pipeline/index_votes.py](../../src/concord/pipeline/index_votes.py) exporting `index(db_path, *, limit=None) -> IndexStats`. Implements the two-pass logic from [Approach > Indexer](#indexer). `IndexStats` carries `votes_flagged_party_unity, members_scored`. Use a single CTE for the `votes.is_party_unity` UPDATE and a single CTE for the `member_party_unity` INSERT.

14. **Test the indexer.** [tests/test_index_votes.py](../../tests/test_index_votes.py):
    - Two votes: one party-unity (R majority Yea, D majority Nay), one not (both parties Yea). After `index()`, `votes.is_party_unity` flagged correctly on the first only.
    - Three members: an R who voted Yea on the party-unity vote (numerator hits), an R who voted Nay (numerator misses), a D who voted Nay (numerator hits). Resulting `member_party_unity` rows have correct numerators/denominators.
    - Independent member with positions — no `member_party_unity` row written for them.
    - Election votes never flagged as party-unity.
    - Re-running `index()` is idempotent (truncate-then-repopulate).

### Section 6 — CLI

15. **Add `concord scrape votes`.** In [src/concord/cli.py](../../src/concord/cli.py) add `DEFAULT_VOTES_CHAMBERS = ("house",)` and `DEFAULT_VOTES_SESSIONS = (1, 2)`. Reuse `_parse_csv` for `--congresses`, `--sessions`, `--chambers`. Register `@scrape_app.command("votes")` with `--congresses` (default `117,118,119`), `--sessions` (default `1,2`), `--chambers` (default `house`), `--storage-dir`, `--limit`, `--progress/--no-progress`. If `senate` is in `--chambers`: log "Senate ingest lands in Phase 3b — skipping." Call `scrape_house(...)` for the house slice.

16. **Add `concord load votes` / `concord index votes` / `concord run votes`.** Mirror the Bills 2a CLI commands. `load votes` takes `--storage-dir`, `--db`, `--limit`. `index votes` takes `--db`, `--limit`. `run votes` chains scrape → load → index. Each no-ops with an informational message if its inputs are missing.

17. **Test the CLI.** Extend [tests/test_cli.py](../../tests/test_cli.py) with the help-smoke pattern. Add an end-to-end test: `scrape votes --congresses 119 --sessions 1 --limit 2` with a mocked `Client`, assert exactly 2 detail envelopes hit `data/house_votes.jsonl` and 2 positions envelopes hit `data/house_vote_positions.jsonl`. Assert `--chambers senate,house` logs the skip message and still runs the house slice.

### Section 7 — Web: Vote pages

18. **Add Votes web helpers.** Extend [src/concord/web/search.py](../../src/concord/web/search.py) with `VoteHit` (pydantic), `list_votes(db, *, chamber, congress, result, vote_kind, bill, limit, offset)`, `get_vote(db, chamber, congress, session, roll)`, `vote_history_for_bill(db, bill_id)`, `recent_votes_for_member(db, bioguide_id, limit=25)`, `party_unity_for_member(db, bioguide_id)`.

19. **Register `/votes` index route.** In [src/concord/web/app.py](../../src/concord/web/app.py) add `VOTES_PAGE_SIZE = 50`. Register `GET /votes` reading `chamber`, `congress`, `result`, `vote_kind`, `bill`, `page`; calls `list_votes`; renders `votes/list.html`.

20. **Register `/votes/{chamber}/{congress}/{session}/{roll}` profile route.** Validate `chamber ∈ ('house', 'senate')`, `session ∈ (1, 2)`, integer `roll`. Build `vote_id`. Call `get_vote`; 404 if missing AND chamber=house; render the "(Phase 3b)" placeholder body if chamber=senate; otherwise render `votes/profile.html`. Fetch positions via `list_vote_positions_for_vote` for the body.

21. **Register `/about/methodology` route.** Static; renders `about/methodology.html`.

22. **Add the templates.**
    - [src/concord/web/templates/votes/list.html](../../src/concord/web/templates/votes/list.html) — filter form (chamber, congress, result, vote_kind, bill); paginated row list using `_vote_row.html`.
    - [src/concord/web/templates/votes/profile.html](../../src/concord/web/templates/votes/profile.html) — header + result block + subject branch + position roster. For senate-chamber rolls render only the "Phase 3b" placeholder body.
    - [src/concord/web/templates/_vote_row.html](../../src/concord/web/templates/_vote_row.html) — shared row partial used here, on the Bill page, and on the Member page.
    - [src/concord/web/templates/about/methodology.html](../../src/concord/web/templates/about/methodology.html) — single static page; `#party-unity` anchor target with the definition.

23. **Add the global nav link.** In [src/concord/web/templates/base.html](../../src/concord/web/templates/base.html) add `<a href="/votes">Votes</a>` between Bills and Proceedings.

### Section 8 — Web: cross-links

24. **Update Bill profile — Vote history.** Edit [src/concord/web/templates/bills/profile.html](../../src/concord/web/templates/bills/profile.html). Replace "Vote history (Phase 3)" placeholder card with a live section that iterates `vote_history` (passed by the route handler). Empty state when none. Use `_vote_row.html` for each row. The handler at the Bills profile route fetches via `vote_history_for_bill(db, bill_id)`.

25. **Update Member profile — Recent votes + Party-unity score.** Edit [src/concord/web/templates/members/profile.html](../../src/concord/web/templates/members/profile.html):
    - Replace "Recent votes (Phase 3)" placeholder with a live list for House members; Senate members get "(Phase 3b)".
    - Replace "Party-line alignment (Phase 3)" placeholder with the party-unity score block (current Congress + history strip + Independent / low-N / missing variants). Senate members get "(Phase 3b)". Add `?` icon linking to `/about/methodology#party-unity`.
    - The handler at [src/concord/web/app.py](../../src/concord/web/app.py) (`/members/{bioguide_id}`) calls `recent_votes_for_member` and `party_unity_for_member` and passes both to the template. Decide House vs Senate by looking at the member's most-recent term `chamber`.

26. **Web tests.** Create [tests/test_web_votes.py](../../tests/test_web_votes.py). Cover:
    - `/votes` with each filter changing the result set.
    - `/votes/house/119/1/240` returns 200; shows correct totals + sponsor's Bill link.
    - `/votes/house/119/1/2` (election) renders with no totals, no party rows, candidates listed.
    - `/votes/senate/119/1/1` renders the Phase 3b placeholder body (no 404).
    - `/votes/house/119/1/999999` returns 404.
    - `/bills/119/hr/3424` profile contains a Vote history section listing the seeded vote.
    - `/members/{house_member_bioguide}` shows Recent votes and a Party-unity score.
    - `/members/{independent_bioguide}` shows the muted Independent treatment.
    - `/members/{senate_member_bioguide}` shows the "(Phase 3b)" placeholder.
    - `/about/methodology` returns 200 and contains the `#party-unity` heading.

### Section 9 — Verification

27. **End-to-end smoke test.** Extend [tests/test_smoke.py](../../tests/test_smoke.py). Write synthetic snapshot JSONL for one bill vote and one election vote (paired members) to a temp dir, run `load_votes.load()` → `index_votes.index()`, open the web app via `TestClient`, hit `/votes`, `/votes/house/119/1/240`, `/bills/119/hr/3424`, `/members/{sponsor_bioguide}`, `/about/methodology`.

28. **Manual smoke against live data.** With `CONGRESS_API_KEY` set: `concord run votes --congresses 119 --sessions 1 --limit 50`, then `concord serve`. Click through `/votes`, a Vote profile, a Bill that has votes, a House member's profile (verify Recent votes and party-unity score render), a Senate member's profile (verify Phase 3b placeholder).

29. **Run the full test suite.** `pytest` clean. `uv run ruff check`. `uv run ruff format --check`. **Phase 2a tests must continue to pass** — especially `tests/test_web_bills.py` (Vote history section is now live) and `tests/test_web_members.py` (Recent votes + Party-unity sections are now live for House members; Senate members still show placeholders).

30. **Update CONTEXT.md.** Add Vote / Roll-call number / Session / Vote question / Vote position / Party Unity Score entries to the Entities section. Cross-link from Party Unity Score to ADR 0011.

## Demo seed data

Concord's demo derives from `data/*.jsonl`; there is no checked-in seed file (matches Phase 1 / Phase 2a). Equivalent: after step 29 lands, the operator runs `concord run votes --congresses 119 --sessions 1 --limit 100` once locally to populate `data/house_votes.jsonl`, `data/house_vote_positions.jsonl`, and the three new SQLite tables. Demo mode now illustrates: a Vote profile with a full position roster, a Bill page showing Vote history, a House Member page with Recent votes and a Party-unity score, the methodology page, and a Senate Member page that correctly defers to Phase 3b.

## Testing strategy

**Unit tests:**

- [tests/test_models_votes.py](../../tests/test_models_votes.py) — `Vote`, `VotePosition`, `VoteSnapshot` round-trips; `vote_id_from_components` / `amendment_id_from_components`; `parse_vote_threshold` for all known values + unknown; election-vote detection; amendment-trap.
- [tests/test_storage_votes_sqlite.py](../../tests/test_storage_votes_sqlite.py) — schema applies cleanly; upsert idempotency; all CHECK constraints; bulk positions upsert.

**Integration tests:**

- [tests/test_api.py](../../tests/test_api.py) extended — `list_house_votes`, `get_house_vote_detail`, `get_house_vote_members` against `httpx.MockTransport` with spike fixtures.
- [tests/test_scraper_votes.py](../../tests/test_scraper_votes.py) — `scrape_house(...)` writes exactly two files, paired envelope counts, `--limit` honored.
- [tests/test_pipeline_votes.py](../../tests/test_pipeline_votes.py) — `load(...)` projects both JSONL files to `votes` + `vote_positions`; bill / amendment / election variants; latest-snapshot-per-key wins; idempotent re-runs.
- [tests/test_index_votes.py](../../tests/test_index_votes.py) — party-unity flag computation; `member_party_unity` numerator / denominator; Independents skipped; election votes never flagged; idempotent re-runs.
- [tests/test_web_votes.py](../../tests/test_web_votes.py) — all routes per step 26.

**Smoke test:** [tests/test_smoke.py](../../tests/test_smoke.py) extended per step 27.

**Manual checks** (no frontend component tests per [ADR 0001](../adr/0001-python-end-to-end-for-the-web-layer.md)):

- `/votes` renders; chamber/congress/result/vote_kind/bill filters change the result set; pagination works.
- A real House Vote profile renders header, result block, subject branch (bill or amendment or procedural), and the full ~430-row position roster.
- An election Vote (e.g. roll 2 in 119/1) renders with no totals, candidate-bucketed positions, winner highlighted.
- A Senate Vote URL (any) renders the "(Phase 3b)" placeholder, not a 404.
- A Bill page (one that had real votes — e.g. HR 3424 in 119) shows the Vote history section with live rows.
- A House Member page shows Recent votes and a Party-unity score with sensible numbers.
- An Independent Senator-or-House-member page shows the muted Independent treatment for Party-unity.
- A Senate Member page shows the "(Phase 3b)" placeholders for both new sections.
- `/about/methodology` page renders; the `?` icon link from a Member page jumps to the `#party-unity` anchor.

**Regression risk:**

- All Phase 2a tests must continue to pass — especially `tests/test_web_bills.py` (Vote history placeholder is now live) and `tests/test_web_members.py` (two placeholders are now live for House members, relabeled "(Phase 3b)" for Senate members).
- Phase 1 tests unchanged.

## Acceptance criteria

- [ ] `concord scrape votes --congresses 119 --sessions 1 --limit 10 --storage-dir /tmp/concord` writes exactly 10 detail envelopes to `data/house_votes.jsonl` and 10 members envelopes to `data/house_vote_positions.jsonl`; no other JSONL files exist.
- [ ] `concord load votes --storage-dir /tmp/concord --db /tmp/test.db` populates `votes` (10 rows) and `vote_positions` (~430 × 10 ≈ 4300 rows).
- [ ] `concord index votes --db /tmp/test.db` populates `votes.is_party_unity` and writes `member_party_unity` rows.
- [ ] `concord run votes --congresses 119 --sessions 1 --limit 10` does scrape + load + index in one shot.
- [ ] `concord scrape votes --chambers senate` logs the Phase-3b skip message and does no work.
- [ ] `concord scrape bills`, `concord scrape members`, `concord run proceedings` still work (Phase 1, 2a regression).
- [ ] `pytest` passes (full suite + new tests).
- [ ] `uv run ruff check` clean. `uv run ruff format --check` clean.
- [ ] `GET /votes` returns 200; chamber/congress/result/vote_kind/bill filters change the result set.
- [ ] `GET /votes/house/119/1/{roll}` returns 200 for a real roll; shows position roster of ~430 entries.
- [ ] `GET /votes/house/119/1/2` (election) renders without totals; candidates listed.
- [ ] `GET /votes/senate/119/1/{any}` renders the Phase 3b placeholder body (200, not 404).
- [ ] `GET /votes/house/119/1/999999` returns 404.
- [ ] `GET /bills/119/hr/{n}` for a bill that has votes shows the Vote history section with live rows (no longer a placeholder).
- [ ] `GET /members/{house_member_bioguide}` shows live Recent votes and a Party-unity score block.
- [ ] `GET /members/{independent_member_bioguide}` shows the muted Independent treatment for Party-unity.
- [ ] `GET /members/{senate_member_bioguide}` shows "(Phase 3b)" placeholders for both new sections (relabeled, not removed).
- [ ] `GET /about/methodology` returns 200; `#party-unity` section is present and explains denominator / numerator.
- [ ] [CONTEXT.md](../../CONTEXT.md) has the new entity entries (Vote / Roll-call number / Session / Vote question / Vote position / Party Unity Score).
- [ ] [docs/adr/0010-votes-phased-by-chamber.md](../adr/0010-votes-phased-by-chamber.md) exists.
- [ ] [docs/adr/0011-party-unity-score-methodology.md](../adr/0011-party-unity-score-methodology.md) exists.
- [ ] [docs/plans/.spike-votes-api.md](./.spike-votes-api.md) and [docs/plans/.spike-votes-api-findings.md](./.spike-votes-api-findings.md) deleted (their findings are baked into this plan and the ADRs).
- [ ] `src/concord/scraper/votes.py` (exporting `scrape_house`), `src/concord/pipeline/{load,index}_votes.py` exist; no Phase 1 / 2a modules were moved or deleted.

## Open questions

None — all design decisions resolved during grilling. The party-unity stat scoping ([ADR 0011](../adr/0011-party-unity-score-methodology.md)) is the most likely to attract refinement requests post-ship; revisions should land as ADR amendments, not silent code changes.

## Out-of-band work

- **Tier 1 full 117–119 House backfill.** `concord run votes --congresses 117,118,119` (no `--limit`) is a ~2-hour command-line job. Run on the VPS once 3a's surface review is done.
- **Phase 3b — Votes (Senate).** Sibling plan to be drafted via the `implementation-plan` skill immediately after 3a lands. Reads senate.gov LIS XML; builds a Bioguide↔LIS-ID mapping; populates the same `votes` + `vote_positions` tables for Senate rolls. Removes the `--chambers senate` no-op. Senate member pages get live Recent-votes + party-unity sections.
- **Phase 4 — Amendments.** Will turn `votes.amendment_id` into a live link to an `/amendments/{...}` profile page. 3a stores the ID as bare TEXT; the link target arrives in Phase 4 with no schema change.
- **Phase 7 — Universal search.** May add Votes to `/search` if a useful query pattern emerges; 3a deliberately defers FTS5.
- **Daily refresh cadence.** A cron job invoking `concord run votes --congresses 119` daily — out of band, operator-configured on the VPS. Not part of this plan.
