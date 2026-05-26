# Phase 3b — Votes (Senate) ingest

> Ingest Senate roll-call votes — metadata, totals, bill/amendment subject, and full per-member positions — from senate.gov LIS XML feeds into Concord, populating the same `votes` / `vote_positions` / `member_party_unity` tables Phase 3a defined for the House. Removes the `--chambers senate` no-op from the CLI, turns the "(Phase 3b)" placeholder boxes on Senate Member profiles into live data, and refines the party-unity methodology to be chamber-scoped.

## Source

- Roadmap: [docs/plans/members-bills-votes-roadmap.md](./members-bills-votes-roadmap.md) (Phase 3 section)
- Sibling plan (prerequisite): [docs/plans/phase-3a-votes-house.md](./phase-3a-votes-house.md) — defines the schema, scraper structure, indexer, and web surface this plan extends. **Phase 3a must be merged before 3b starts.**
- Spike that informed this plan: [docs/plans/.spike-senate-votes.md](./.spike-senate-votes.md) and [docs/plans/.spike-senate-votes-findings.md](./.spike-senate-votes-findings.md). **Both files should be deleted once this plan is approved** — their findings are reflected below.

## Context

Phase 3a ingests U.S. House roll-call votes from `api.congress.gov`'s `/v3/house-vote/...` endpoints. Phase 3b ingests U.S. Senate roll-call votes from senate.gov's **LIS XML feeds** — `LIS` standing for *Legislative Information System*, the Senate's internal data interchange format, exposed publicly per-roll at predictable URLs under `https://www.senate.gov/legislative/LIS/`.

The chamber-based split (Phase 3a House, Phase 3b Senate) is recorded in [ADR 0010 — Votes phased by chamber](../adr/0010-votes-phased-by-chamber.md). The driving fact is that `api.congress.gov` has no Senate vote endpoint at all (the API spike for 3a returned 404 on every `/senate-vote/...` variant tried) — so Phase 3b uses an entirely different source, parser, and HTTP client.

A Senate **roll call** is one recorded vote, identified by `(congress, session, roll_number)` — same shape as a House roll call. Internal `vote_id` flattens to `"senate-{congress}-{session}-{roll}"`. The Senate's three vote-related XML feeds (confirmed by the spike) are:

- **Menu XML** — `https://www.senate.gov/legislative/LIS/roll_call_lists/vote_menu_{congress}_{session}.xml`. One file per `(congress, session)` slot; lists every roll number plus a per-vote summary block. Used by Phase 3b only for **roll-number discovery** — not persisted.
- **Detail XML** — `https://www.senate.gov/legislative/LIS/roll_call_votes/vote{congress}{session}/vote_{congress}_{session}_{roll_padded5}.xml`. One file per roll; contains chamber totals AND per-member positions in a single document. Senate timestamps in this file are in Eastern Time (ET, `America/New_York`) without an explicit timezone offset — the loader localizes. *Persisted as snapshots.*
- **Roster XML** — `https://www.senate.gov/general/contact_information/senators_cfm.xml`. Current sitting Senate roster; needed for the **LIS↔Bioguide bridge** (see below). *Persisted as snapshots.*

The detail XML keys members by `lis_member_id` (e.g., `"S428"`) — **not by Bioguide ID**. Concord's `vote_positions` table keys on `bioguide_id`. The bridge is the `member_full` field (e.g., `"Alsobrooks (D-MD)"`), which appears in both detail XML and the roster XML, and which Phase 1's [src/concord/scraper/members.py](../../src/concord/scraper/members.py) doesn't currently track. Bridge logic is load-time and in-memory (no new SQLite table) — see [Approach > Member bridge](#member-bridge).

The party-unity methodology defined by [ADR 0011](../adr/0011-party-unity-score-methodology.md) was originally `(bioguide_id, congress)`-keyed. Phase 3b refines it to `(bioguide_id, congress, chamber)`-keyed: a Senate party-unity vote depends on Senate R-majority vs Senate D-majority and shouldn't pool with House votes from the same Congress. The ADR is **amended in place** to capture this refinement; no new ADR is created — see [Approach > Party-unity refinement](#party-unity-refinement).

The roadmap risks Phase 3 carries forward are unchanged from 3a:

- **Editorial neutrality** — already addressed by the chamber-scoped Party Unity Score; 3b just brings the Senate side online.
- **Staleness** — daily-cadence operator-driven `concord run votes` job, same stance as Phase 3a.

## Goals

1. The Tier 1 scraper fetches, for every Senate roll-call vote in Congresses 117, 118, and 119:
   - The menu XML for each `(congress, session)` slot, used in-process for roll-number discovery — **not persisted**.
   - The per-roll detail XML for each roll — appended to `data/senate_votes.jsonl` as one [ADR 0006](../adr/0006-snapshot-on-fetch-for-mutable-entities.md) snapshot envelope per fetch, payload = the raw XML bytes as a UTF-8 string.
   - The Senate roster XML (`senators_cfm.xml`) once per scrape run — appended to `data/senate_roster.jsonl` as one snapshot envelope per fetch.
2. The loader projects the latest detail-XML snapshot per `(chamber, congress, session, roll_number)` into the `votes` SQLite table and the latest per-position into `vote_positions`. Loader resolves each XML member entry's `member_full` to a Bioguide ID via the in-memory bridge built from the latest `senators_cfm.xml` snapshot plus Phase 1's `members` table.
3. The indexer (Stage 2) recomputes `votes.is_party_unity` and `member_party_unity` against the new chamber-scoped methodology — rebuilds `member_party_unity` from scratch with the new schema, covering both House and Senate.
4. The CLI's `concord scrape votes` default for `--chambers` becomes `house,senate`; the Senate no-op message and short-circuit logic added in 3a are removed.
5. The web app surfaces:
   - `GET /votes/senate/{congress}/{session}/{roll}` — Senate Vote profile (header, result block, subject branch, full position roster). Renders identically to the House version per the shared `votes/profile.html` template — no chamber-specific branches.
   - Member profile page: Senate members' "Recent votes" and "Party-unity score" sections now render live data; the "(Phase 3b)" placeholder body is removed.
   - Bill page Vote history sections include Senate votes alongside House votes (already wired in 3a — query is `WHERE bill_id = ?`, chamber-agnostic).
6. ADR 0011 (Party Unity Score) is amended in place with a Phase 3b refinement section documenting the `chamber` PK addition to `member_party_unity`.
7. CONTEXT.md gains entries for **LIS member ID** and **`member_full`** so the bridge concept is documented for future maintainers.
8. The two spike files (`.spike-senate-votes.md`, `.spike-senate-votes-findings.md`) are deleted; their findings live in this plan.
9. All existing tests (Phase 1, 2a, 3a) continue to pass.

## Non-goals

1. **`vote_matters` side table for en-bloc votes.** *En bloc* is Senate procedure for voting on multiple matters in a single roll call — typically a batch of nominations confirmed together. Vote 655 in 119/1 rolls 96 nominations into one roll call. Phase 3b stores en-bloc rolls as a single `votes` row with `<vote_title>` as the `question` field and ignores the per-matter `<en_bloc><matter>` breakdown. Future v2 work can add a `vote_matters` table without migration pain (purely additive). See [Approach > En-bloc handling](#en-bloc-handling).
2. **Persistent `senate_member_aliases` SQLite table.** The LIS↔Bioguide bridge is computed in memory at load time. No new table.
3. **Surfacing the three-level amendment nesting** (`amendment_to_amendment_to_amendment_number`). Phase 3b captures only the immediate amendment in `votes.amendment_id`; the chain is preserved in the JSONL raw payload but not modeled in SQLite.
4. **`vote_kind = 'trial'` for impeachment trials.** None present in 117–119. The `position` column is free TEXT and accepts `Guilty`/`Not Guilty` already; no new enum value.
5. **Treaty entity table.** Treaties surface as `votes` rows with both `bill_id` and `amendment_id` NULL and the treaty identity in `question` only. Same stance as Phase 3a for nominations.
6. **lxml or other non-stdlib XML parser.** Stdlib `xml.etree.ElementTree` is sufficient (no namespaces, no XPath complexity in the spike-captured fixtures).
7. **HTTP caching of senate.gov resources.** No `Last-Modified` header per spike; each scrape run does fresh fetches. Snapshot-on-fetch provides de-facto history.
8. **Senate vote text RAG.** Phase 5 territory.
9. **Backfill before Congress 117.** Same scope as 3a.
10. **A "live pair" / "announced for/against" UI affordance.** None observed in 119/1 sample per spike; if such position values appear in 117/118 historical data, the loader treats them as free-text positions and the party-unity computation drops them from the denominator (anything not in `{Yea, Nay}` is dropped).
11. **Removing `chamber` from `votes.UNIQUE` constraint** or any other schema rewrite. Phase 3a's `votes` and `vote_positions` schemas are sufficient as-is.

## Relevant prior decisions

- [ADR 0001 — Python end-to-end for the web layer](../adr/0001-python-end-to-end-for-the-web-layer.md) — Senate Vote pages use the same FastAPI+Jinja2 templates as House.
- [ADR 0002 — JSONL as canonical raw store](../adr/0002-jsonl-as-canonical-raw-store.md) — Senate data lands in `data/senate_votes.jsonl` and `data/senate_roster.jsonl`.
- [ADR 0003 — SQLite as derived store](../adr/0003-sqlite-as-derived-store.md) — `member_party_unity` PK migration is "delete the SQLite file and re-index" per this ADR; no online migration needed.
- [ADR 0006 — Snapshot-on-fetch for mutable entities](../adr/0006-snapshot-on-fetch-for-mutable-entities.md) — both Senate JSONL files carry the standard `{fetched_at, key, payload}` envelope; the Senate roster is treated as a mutable entity (senators leave / new ones arrive).
- [ADR 0007 — Parallel pipelines per entity](../adr/0007-parallel-pipelines-per-entity.md) — `src/concord/senate_xml.py` is a *new HTTP client* sibling to `src/concord/api.py`; `scrape_senate` lands in `src/concord/scraper/votes.py` alongside `scrape_house`; loader / indexer / CLI all extend their Phase 3a modules in place.
- [ADR 0009 — Multi-endpoint entities split JSONL by sub-endpoint](../adr/0009-multi-endpoint-entities-split-jsonl.md) — Senate writes two of the four eventual vote-files; together with 3a's two, the fanout matches the ADR's "one fetch = one snapshot" framing.
- [ADR 0010 — Votes phased by chamber, not metadata-vs-positions](../adr/0010-votes-phased-by-chamber.md) — recorded in 3a; this plan is the 3b side it predicted.
- **[ADR 0011 — Party Unity Score methodology](../adr/0011-party-unity-score-methodology.md)** — **amended in place** with this plan to capture the chamber-scoping refinement (the `member_party_unity` PK gains `chamber`).

No new ADRs are created with this plan.

## Relevant files and code

Files to **read** for context:

- [CONTEXT.md](../../CONTEXT.md) — new entries land in this plan: **LIS member ID**, **`member_full`**. Existing Vote / Roll-call number / Session / Vote question / Vote position / Party Unity Score entries (added in 3a) stay unchanged.
- [docs/adr/0011-party-unity-score-methodology.md](../adr/0011-party-unity-score-methodology.md) — gets a Phase 3b refinement section appended.
- [src/concord/api.py](../../src/concord/api.py) — the `USER_AGENT` constant and the retry-with-backoff pattern (lines ~10–22) are the template for `senate_xml.py`. Do not extend this client with Senate methods; create a new module.
- [src/concord/scraper/votes.py](../../src/concord/scraper/votes.py) — Phase 3a's `scrape_house` is the structural template. `scrape_senate` lands alongside it in the same file.
- [src/concord/pipeline/load_votes.py](../../src/concord/pipeline/load_votes.py) — Phase 3a's `load(...)` function. Gets a Senate branch that handles XML payloads and the in-memory member bridge.
- [src/concord/pipeline/index_votes.py](../../src/concord/pipeline/index_votes.py) — Phase 3a's indexer. The `member_party_unity` rebuild query is generalized to group by `(bioguide_id, congress, chamber)`.
- [src/concord/storage/sqlite.py](../../src/concord/storage/sqlite.py) — `_BASE_SCHEMA` (line 59); the `member_party_unity` CREATE TABLE statement gains a `chamber` column and the PK changes. `upsert_vote` / `upsert_vote_positions` (added in 3a) are reused.
- [src/concord/scraper/members.py](../../src/concord/scraper/members.py) — Phase 1 scraper; its output (`data/members.jsonl` projected to `members` table) is consulted by the load-time bridge for historical senators.
- [src/concord/cli.py](../../src/concord/cli.py) — the Phase 3a `scrape_votes_command` carries the `--chambers house` default and the Senate skip message; both change in this plan.
- [src/concord/web/templates/members/profile.html](../../src/concord/web/templates/members/profile.html) — "(Phase 3b)" placeholder boxes get removed; the live Recent-votes + Party-unity sections become chamber-agnostic.
- [src/concord/web/templates/votes/profile.html](../../src/concord/web/templates/votes/profile.html) — the senate-chamber Phase-3b body branch gets removed; one shared template for both chambers.
- [src/concord/web/app.py](../../src/concord/web/app.py) — the `/votes/{chamber}/{congress}/{session}/{roll}` handler (registered per Phase 3a plan, Section 7 step 20) drops its senate-placeholder branch; the `/members/{bioguide_id}` handler (already at line 320 pre-3a; gains vote-related calls per Phase 3a step 25) drops its chamber check.
- [tests/fixtures/senate/](../../tests/fixtures/senate/) — already populated by the spike: `vote_menu_119_1.xml`, `detail_119_1_00001_cloture.xml`, `detail_119_1_00002_motion.xml`, `detail_119_1_00003_amendment.xml`, `detail_119_1_00007_bill.xml`, `detail_119_1_00008_nomination.xml`, `senators_cfm.xml`.
- [tests/test_pipeline_votes.py](../../tests/test_pipeline_votes.py), [tests/test_index_votes.py](../../tests/test_index_votes.py), [tests/test_web_votes.py](../../tests/test_web_votes.py) — Phase 3a's test files; Phase 3b adds parallel Senate cases.

Files to **create**:

- `src/concord/senate_xml.py` — new HTTP client (`SenateClient`) + XML parsing helpers. Exposes `list_roll_call_numbers`, `get_roll_call_xml`, `get_current_senators_xml`, plus parsing functions `parse_vote_menu`, `parse_vote_detail`, `parse_senate_roster`.
- `tests/test_senate_xml.py` — unit tests for the new module, exercising the spike-captured fixtures.

Files to **modify**:

- `src/concord/scraper/votes.py` — add `scrape_senate(client_xml, congresses, storage_dir, *, fetched_at, sessions=(1,2), limit=None, progress=None) -> ScrapeStats`.
- `src/concord/pipeline/load_votes.py` — add a Senate branch reading both Senate JSONL files; add `_build_member_bridge(...)` helper; integrate into the `load(...)` driver.
- `src/concord/pipeline/index_votes.py` — change `member_party_unity` rebuild to group by `(bioguide_id, congress, chamber)`.
- `src/concord/storage/sqlite.py` — update `member_party_unity` schema in `_BASE_SCHEMA` (add `chamber` column, change PK); update `_MEMBER_PARTY_UNITY_COLUMNS` and `_MEMBER_PARTY_UNITY_UPSERT_SQL` accordingly; update `get_party_unity_for_member` to return rows ordered by `(congress DESC, chamber)`.
- `src/concord/cli.py` — change `DEFAULT_VOTES_CHAMBERS` from `("house",)` to `("house", "senate")`; remove the "Senate ingest lands in Phase 3b — skipping." log path; wire `scrape_senate` into the senate slice of `scrape_votes_command` and `run_votes_command`.
- `src/concord/web/templates/members/profile.html` — remove the chamber-based template branch for the Recent-votes and Party-unity sections; both sections render uniformly. Remove "(Phase 3b)" labels.
- `src/concord/web/templates/votes/profile.html` — remove the senate-chamber Phase-3b body branch; one template path for both chambers.
- `src/concord/web/app.py` — drop the senate Phase-3b placeholder branch in the vote-profile handler; drop the chamber check in the member-profile handler that gated Recent-votes / Party-unity display.
- `docs/adr/0011-party-unity-score-methodology.md` — append a "Phase 3b refinement" section explaining the `chamber` PK addition. Update front-matter Status line: `Status: Accepted 2026-05-25; refined 2026-05-26 (Phase 3b)`.
- `CONTEXT.md` — add **LIS member ID** and **`member_full`** entries under the Entities section.

Files to **delete**:

- `docs/plans/.spike-senate-votes.md`
- `docs/plans/.spike-senate-votes-findings.md`

## Approach

### Source overview

Senate vote data lives at three URLs under senate.gov:

```
https://www.senate.gov/legislative/LIS/roll_call_lists/vote_menu_{congress}_{session}.xml   ← discovery
https://www.senate.gov/legislative/LIS/roll_call_votes/vote{congress}{session}/vote_{congress}_{session}_{roll5}.xml   ← detail (positions + totals)
https://www.senate.gov/general/contact_information/senators_cfm.xml   ← roster (LIS↔Bioguide bridge)
```

All three return XML. No auth. No documented rate limit (the spike observed clean responses at ~25 requests with no 429s; the scraper uses a 0.1s inter-request sleep as defensive padding). User-Agent is `concord/{version} (+https://github.com/johnmarcampbell/concord)` — same as the existing `api.py` client.

The menu XML lists every roll in a `(congress, session)` slot with summary fields. Phase 3b uses it **only for roll-number discovery** — not persisted. The roll numbers in `<vote_number>` come back zero-padded (`"00007"`); the detail-XML URLs need them zero-padded (`"00007"`); the detail XML itself unpads (`"7"`). The scraper preserves the menu's padded form for URL construction.

### Storage shape

**Phase 3b writes two JSONL files**, both following [ADR 0006](../adr/0006-snapshot-on-fetch-for-mutable-entities.md):

- `data/senate_votes.jsonl` — one snapshot per detail-XML fetch. `key`: `{"chamber": "senate", "congress": 119, "session": 1, "roll_number": 7}`. `payload`: the raw XML bytes decoded as UTF-8 string, stored verbatim. (Unlike House where `payload` is the parsed JSON object, here `payload` is the raw XML — see [Approach > Why XML is stored raw](#why-xml-is-stored-raw).)
- `data/senate_roster.jsonl` — one snapshot per `senators_cfm.xml` fetch. `key`: `{"source": "senators_cfm"}`. `payload`: raw XML string. The Senate roster doesn't have a natural primary key beyond "the current snapshot" — `key.source` is a constant disambiguator.

**No SQLite schema changes other than `member_party_unity`.** The Phase 3a `votes` and `vote_positions` tables accept Senate rows as-is. The `member_party_unity` table schema becomes:

```sql
CREATE TABLE IF NOT EXISTS member_party_unity (
  bioguide_id              TEXT NOT NULL,
  congress                 INTEGER NOT NULL,
  chamber                  TEXT NOT NULL
    CHECK (chamber IN ('house', 'senate')),
  party                    TEXT NOT NULL
    CHECK (party IN ('R', 'D')),
  party_unity_votes_cast   INTEGER NOT NULL,
  party_line_votes         INTEGER NOT NULL,
  PRIMARY KEY (bioguide_id, congress, chamber)
);
```

The PK gains `chamber`. Existing 3a rows are dropped during the schema rebuild. Per [ADR 0003](../adr/0003-sqlite-as-derived-store.md), the SQLite file is regenerable — the migration is "delete the SQLite file and re-load + re-index everything" (`rm data/proceedings.db && concord load proceedings && concord load members && concord load bills && concord load votes && concord index votes`). Operators do this once when 3b lands; no online migration code is written.

### Why XML is stored raw

Phase 3a stores parsed JSON as the snapshot `payload` because the api.congress.gov client returns parsed dicts. Phase 3b stores raw XML as the snapshot `payload` because the Senate XML parser is part of the load step, not the scrape step. Two reasons:

1. **Robustness to schema changes.** If senate.gov adds a new element next year, the JSONL still loads (raw XML is forward-compatible); re-parsing is a load-step change, not a re-scrape.
2. **Loader symmetry across data shapes.** The Phase 3a House loader unpacks `payload` as a dict; the 3b Senate loader unpacks `payload` as an XML string and parses it. Each has one shape; neither file mixes.

Round-trip cost: storing XML as a string adds ~5% to the file size vs JSON. Trivial at v1 scale (~700 rolls × 28 KB × 3 Congresses = ~60 MB).

### HTTP client structure

A new module `src/concord/senate_xml.py` houses `SenateClient` and the parsing functions. The client class:

```python
class SenateClient:
    def __init__(self, *, user_agent: str = USER_AGENT) -> None: ...
    def list_roll_call_numbers(self, congress: int, session: int) -> list[int]: ...
    def get_roll_call_xml(self, congress: int, session: int, roll_number: int) -> bytes: ...
    def get_current_senators_xml(self) -> bytes: ...
```

Implementation notes:

- Uses `httpx.Client(timeout=30.0)` directly — no auth, no API key. User-Agent in headers.
- **Retry policy**: 3 retries on transport errors (DNS, connection reset, read timeout), with exponential backoff (1s, 2s, 4s). No special 429 handling — senate.gov hasn't shown rate limits in the spike, but the standard backoff covers transient 5xx.
- **HTML-404-disguised-as-200 detection**: after each fetch, check `Content-Type`. If it's `text/html`, raise `SenateXmlError(f"Got HTML response, expected XML, at {url}")` — the spike found `senators_cfm_historical.xml` returns this trap.
- **Inter-request padding**: 0.1s sleep between detail-XML fetches. Defensive; senate.gov hasn't rate-limited, but a Senate-only backfill is ~700 requests × 3 Congresses × 2 sessions = ~4200 detail fetches, and the padding keeps load gentle.

Parsing functions (also in `senate_xml.py`):

```python
def parse_vote_menu(xml_bytes: bytes) -> list[int]:
    """Return roll numbers from vote_menu_{c}_{s}.xml, oldest-first."""

def parse_vote_detail(xml_bytes: bytes) -> ParsedVoteDetail:
    """Parse vote_{c}_{s}_{rrrrr}.xml. Returns a typed object with chamber
    totals, subject (bill_id / amendment_id / question), threshold, vote_kind,
    start_date (ISO8601 with ET offset), and a list of ParsedVotePosition records."""

def parse_senate_roster(xml_bytes: bytes) -> dict[str, str]:
    """Parse senators_cfm.xml. Returns dict mapping member_full -> bioguide_id."""
```

`ParsedVoteDetail` and `ParsedVotePosition` are pydantic models in [src/concord/models.py](../../src/concord/models.py), added alongside the 3a `Vote` and `VotePosition`.

Stdlib `xml.etree.ElementTree` is the parser. No namespaces in the Senate XML files; `.findtext()`, `.findall()`, `.iter()` are sufficient.

### Scraper structure

`src/concord/scraper/votes.py` gains a second entry point alongside 3a's `scrape_house`:

```python
def scrape_senate(
    client_xml: SenateClient,
    congresses: Iterable[int],
    storage_dir: Path,
    *,
    fetched_at: datetime,
    sessions: Iterable[int] = (1, 2),
    limit: int | None = None,
    progress: Callable[[ScrapeProgressEvent], None] | None = None,
) -> ScrapeStats: ...
```

Driver behavior:

1. Fetch `senators_cfm.xml` **once** at the start; append one snapshot envelope to `data/senate_roster.jsonl` with `key={"source": "senators_cfm"}`.
2. For each `(congress, session)` pair:
   a. Fetch the menu XML; parse into roll numbers.
   b. For each roll, fetch the detail XML; append one snapshot envelope to `data/senate_votes.jsonl` with `key={"chamber": "senate", "congress": c, "session": s, "roll_number": n}`.
   c. Sleep 0.1s between detail fetches.
   d. Emit `ScrapeProgressEvent(chamber='senate', congress, session, votes_seen, votes_written)` once per `(congress, session)` after all rolls in that pair complete.
3. **Stop after `limit` detail fetches**, if set. The roster is always fetched regardless of `--limit`.

Cost envelope: 3 Congresses × 2 sessions × ~600 rolls/session ≈ 3600 detail fetches × 0.4s = ~25 min. Plus 1 roster fetch + 6 menu fetches. ~60 MB JSONL total.

### Loader

`src/concord/pipeline/load_votes.py`'s existing `load(...)` driver gains a Senate branch:

```python
def load(storage_dir: Path, db_path: Path, *, limit: int | None = None) -> LoadStats:
    # Existing 3a flow for House — unchanged
    _load_house(storage_dir, db_path, limit=limit)
    # New 3b flow for Senate
    _load_senate(storage_dir, db_path, limit=limit)
```

The Senate branch:

1. Read `data/senate_roster.jsonl`; build the in-memory `roster_bridge: dict[str, str]` (member_full → bioguide_id) from the latest snapshot.
2. Read `data/senate_votes.jsonl`; group by `(chamber, congress, session, roll_number)`; keep latest by `fetched_at`.
3. For each latest snapshot:
   - Parse the XML payload via `parse_vote_detail`.
   - Build the `Vote` model from the parsed detail.
   - Upsert into `votes`.
   - For each position in the parsed detail:
     - Resolve `member_full` → `bioguide_id` via `_resolve_bioguide(member_full, vote_date, roster_bridge, members_table_conn)`.
     - If resolved: upsert into `vote_positions`.
     - If not resolved: log a structured warning (`unresolved_member`) and skip the position. The vote totals still load.
4. Idempotent. Re-running over unchanged JSONL is a no-op.

### Member bridge

The `_resolve_bioguide(member_full, vote_date, roster_bridge, db_conn)` helper:

1. **Direct hit in roster:** if `member_full` is in `roster_bridge`, return that Bioguide ID. Covers current senators.
2. **Historical fallback:** parse `member_full` into `last_name` + `state` + `party`:
   - Regex `^(?P<last>[^\(]+?)\s+\((?P<party>[RDI])-(?P<state>[A-Z]{2})\)$` (handles `"Blunt Rochester (D-DE)"`, `"Van Hollen (D-MD)"`, `"Hyde-Smith (R-MS)"`).
   - Query Phase 1's `members` table: `SELECT bioguide_id FROM members m JOIN member_terms t ON m.bioguide_id = t.bioguide_id WHERE t.chamber = 'senate' AND m.last_name = ? AND t.state = ? AND t.start_date <= ? AND (t.end_date IS NULL OR t.end_date >= ?)`. The double-date guard handles term-overlap with the vote date.
   - If exactly one match: return it. If zero or multiple: bridge fails.
3. **Failure:** return `None`; caller logs and skips.

Tolerance for mid-Congress party-switch: the regex captures party but the historical query doesn't filter on it — `(last_name, state, date-overlap)` is sufficient. If a member's `(D-XX)` flips to `(I-XX)` mid-term, the bridge still resolves.

Multi-word surnames: the regex's `[^\(]+?` non-greedy match against `\s+\(`-prefix handles spaces in surnames. The bridge does not parse `member_full` itself for "last name" — Phase 1's `members.last_name` is the authoritative split.

### Vote-detail XML parsing

`parse_vote_detail` returns a `ParsedVoteDetail` with these fields ready for the loader:

- `congress`, `session`, `roll_number` — from `<congress>`, `<session>`, `<vote_number>` (unpadded in detail XML).
- `start_date` — from `<vote_date>` (`"January 20, 2025,  06:12 PM"`), parsed via `datetime.strptime(s.strip().replace("  ", " "), "%B %d, %Y, %I:%M %p")` and localized via `zoneinfo.ZoneInfo("America/New_York")`. Formatted as ISO8601 with offset.
- `update_date` — from `<modify_date>`, same parser.
- `vote_question` — from `<vote_question_text>`.
- `vote_type` — from `<question>` (short label).
- `threshold` — from `<majority_requirement>`: `"1/2"` → `simple_majority`, `"3/5"` → `three_fifths`, `"2/3"` → `two_thirds`. Unknown values → `None` and a structured warning.
- `result` — from `<vote_result>` (short form without count).
- `vote_kind` — always `'standard'` for Senate in 3b. The detail XML has no election-vote analog.
- `yea_count`, `nay_count`, `present_count` — from `<count><yeas>`, `<nays>`, `<present>`.
- `not_voting_count` — from `<count><absent>` (vocabulary mapping: senate calls it "absent", Phase 3a's column is "not voting").
- `bill_id` — see subject branching below.
- `amendment_id` — see subject branching below.
- `positions: list[ParsedVotePosition]` — one entry per `<member>` child, with `member_full`, `last_name`, `first_name`, `party`, `state`, `vote_cast`, `lis_member_id`.

**Subject branching** (in `parse_vote_detail`, mirroring the House "amendment trap" logic but for the Senate XML schema):

```python
amendment_number = root.findtext("amendment/amendment_number") or ""
if amendment_number.strip():
    # Amendment vote: amendment_id from <amendment>, bill_id from <amendment_to_document_*>
    amendment_id = _build_amendment_id(congress, amendment_number)
    bill_id = _build_bill_id(
        congress,
        root.findtext("amendment/amendment_to_document_type"),
        root.findtext("amendment/amendment_to_document_number"),
    )
else:
    amendment_id = None
    doc_type = (root.findtext("document/document_type") or "").strip()
    if doc_type in BILL_TYPES_SENATE:  # see below
        bill_id = _build_bill_id(congress, doc_type, root.findtext("document/document_number"))
    else:
        # Nomination (doc_type="PN"), treaty, or empty — both FKs NULL
        bill_id = None
```

`BILL_TYPES_SENATE` is the set of document-type codes that map to Concord's bill types. From spike: `{"S.", "S.J.Res.", "S.Res.", "S.Con.Res.", "H.R.", "H.J.Res.", "H.Res.", "H.Con.Res."}`. The mapping function lowercases and strips dots/spaces: `"S.J.Res."` → `"sjres"`, `"H.R."` → `"hr"`, etc. Same canonical form as Phase 2a's `bills.bill_type`. Codes outside this set (notably `"PN"` for nominations — *Presidential Nomination* — and treaty types) intentionally return `None` so those votes get NULL FK columns and carry their identity in `question` text only.

`_build_amendment_id(congress, amendment_number)` parses the XML form `"S.Amdt. 14"` into `"119-samdt-14"`. House amendment IDs use `hamdt`; Senate uses `samdt`. The amendment number is always extracted from `<amendment><amendment_number>` — the higher-degree nesting (`amendment_to_amendment_number`, etc.) is preserved in JSONL but not parsed into SQLite columns.

### En-bloc handling

Detection: if `<en_bloc>` is present AND `<question>` is empty:

- `question` field on the `votes` row = `<vote_title>` (e.g., `"Confirmation of 96 Nominations en bloc"`).
- `bill_id` = NULL, `amendment_id` = NULL.
- Position roster loaded normally.
- The `<en_bloc><matter>` children are present in the raw XML payload (preserved in JSONL) but not surfaced in SQLite for v1. A future `vote_matters` table can be added purely-additively.

### Party-unity refinement

[ADR 0011](../adr/0011-party-unity-score-methodology.md) is **amended in place** with a "Phase 3b refinement (2026-05-26)" section that documents:

- The `member_party_unity` PK gains `chamber TEXT NOT NULL CHECK (chamber IN ('house', 'senate'))`.
- The score is now per `(bioguide_id, congress, chamber)`.
- Rationale: a Senate party-unity vote's R-majority-vs-D-majority is computed over Senate positions only; pooling with House votes from the same Congress would mix two different denominators and dilute the score's meaning.
- Members who served in both chambers within one Congress (rare chamber-switchers like a House member appointed to fill a Senate vacancy) get two rows. UI renders both labeled.
- Members in one chamber per Congress (~99% case) see no visible change.

The amendment block carries its own date; the ADR's original "Accepted, 2026-05-25" line is preserved with a "; refined 2026-05-26 (Phase 3b)" suffix.

### Indexer changes

[src/concord/pipeline/index_votes.py](../../src/concord/pipeline/index_votes.py) — the `member_party_unity` rebuild CTE generalizes:

```sql
-- Phase 3a (current)
GROUP BY vp.bioguide_id, v.congress

-- Phase 3b (new)
GROUP BY vp.bioguide_id, v.congress, v.chamber
```

The `votes.is_party_unity` UPDATE is unchanged — it's already per-vote (chamber-implicit via the `vote_positions` JOIN).

### CLI changes

`src/concord/cli.py`:

- `DEFAULT_VOTES_CHAMBERS = ("house", "senate")` (was `("house",)`).
- The Senate skip-message branch in `scrape_votes_command` is removed.
- The senate slice now calls `scrape_senate(client_xml=SenateClient(), ...)` where the 3a code had a no-op log.
- `run_votes_command` is updated to construct both `Client` (api.congress.gov) and `SenateClient` as needed.

`concord scrape votes` (no args) now runs both chambers for the default congress set. `concord scrape votes --chambers house` still works for House-only scrapes; `--chambers senate` for Senate-only.

### Web surface changes

**Vote profile.** `votes/profile.html` already handles both chambers in 3a — Phase 3b just removes the senate-chamber "(Phase 3b)" body branch. The shared template renders header / result / subject / position roster identically for both chambers.

**Member profile.** `members/profile.html` had chamber-based template branches in 3a (House members got live sections; Senate members got "(Phase 3b)" placeholders). Phase 3b removes those branches — both chambers render the same live sections.

The `/members/{bioguide_id}` handler in `app.py` no longer needs to look up the member's chamber to decide template behavior. The handler simply queries `recent_votes_for_member(bioguide_id)` and `party_unity_for_member(bioguide_id)`; the templates iterate whatever returns.

**Party-unity display for chamber-switchers.** The handler returns one row per Congress per chamber the member served in. Template iterates and renders each as its own labeled stat ("Senate: voted with Democratic majority on …", "House: voted with Democratic majority on …"). For the ~99% of members who served in one chamber per Congress, the layout is visually identical to Phase 3a's single-row display.

**Bill profile Vote history.** Already chamber-agnostic in 3a — query is `WHERE bill_id = ?`. Senate votes appear alongside House votes automatically once they load. No template change.

**`/about/methodology` page.** Reflects the chamber-scoping refinement. Update the `#party-unity` section to note: "Each chamber's score is computed independently from positions cast in that chamber. Members who served in both chambers in a single Congress see two scores per Congress, one per chamber."

## Step-by-step plan

Eight sections — **ADR amendment & CONTEXT.md**, **HTTP client & parsing**, **Models**, **Storage**, **Scraper**, **Loader**, **Indexer**, **CLI**, **Web**, **Verification**.

### Section 1 — ADR amendment & CONTEXT.md

1. **Amend ADR 0011 with the Phase 3b chamber refinement.** Append a "Phase 3b refinement (2026-05-26)" section to [docs/adr/0011-party-unity-score-methodology.md](../adr/0011-party-unity-score-methodology.md). Document the PK change to `(bioguide_id, congress, chamber)`, the rationale (chamber-specific majorities), and the UI implication (chamber-switchers see two rows per Congress). Update Status line: `Status: Accepted 2026-05-25; refined 2026-05-26 (Phase 3b)`.

2. **Add LIS member ID + member_full entries to CONTEXT.md.** In the Entities section of [CONTEXT.md](../../CONTEXT.md), add:
   - **LIS member ID** — Senate-internal stable identifier for a senator, used in senate.gov LIS XML feeds. Format `"S\d+"` (e.g., `"S428"`). Concord does not store LIS IDs in SQLite; they are used only as a transient join key inside the Senate vote loader.
   - **`member_full`** — Senate display string in the form `"Surname (Party-State)"` (e.g., `"Alsobrooks (D-MD)"`). Appears in both senate.gov vote-detail XML and `senators_cfm.xml`. Concord uses it as the bridge string for LIS↔Bioguide resolution at vote-load time.

### Section 2 — HTTP client & parsing

3. **Create `src/concord/senate_xml.py` skeleton.** Create the module with the `USER_AGENT` import, `SenateClient` class skeleton (init + three method signatures stubs), and `SenateXmlError` exception. No fetch logic yet. Confirm import works: `python -c "from concord.senate_xml import SenateClient"`.

4. **Implement `SenateClient.get_current_senators_xml()`.** Single GET to `https://www.senate.gov/general/contact_information/senators_cfm.xml`. User-Agent set; 30s timeout. Detect Content-Type mismatch (raise `SenateXmlError` if `text/html`). Return raw bytes.

5. **Implement `SenateClient.list_roll_call_numbers(congress, session)`.** GET the menu URL; parse with stdlib `xml.etree.ElementTree`. Iterate `<vote>` children; extract `<vote_number>` (zero-padded string); convert to int; return sorted ascending. Skip entries with empty/missing `<vote_number>` and log a warning. (Note: the menu XML returns newest-first per spike; sort ascending for stable scrape order.)

6. **Implement `SenateClient.get_roll_call_xml(congress, session, roll_number)`.** Build URL with zero-padded 5-digit roll number; GET; detect HTML 404 trap; return raw bytes.

7. **Implement `parse_senate_roster(xml_bytes)`.** Parse `senators_cfm.xml`; iterate `<member>` children; extract `member_full` from constructed `f"{last_name} ({party}-{state})"` (the spike found this XML carries last_name / first_name / party / state / bioguide_id separately — reconstruct `member_full` to match the format used in vote XML); return `dict[member_full, bioguide_id]`. Skip entries missing bioguide_id and log.

8. **Implement `parse_vote_menu(xml_bytes)`.** Return list of int roll numbers sorted ascending.

9. **Implement `parse_vote_detail(xml_bytes)`.** Returns a `ParsedVoteDetail` model (added in Section 3). The function:
   - Extracts top-level scalars (`congress`, `session`, `vote_number` — convert to int).
   - Parses `<vote_date>` and `<modify_date>` via ET-timezone helper.
   - Maps `<majority_requirement>` to threshold enum.
   - Sets `vote_kind='standard'`.
   - Runs subject branching: amendment-vote precedence, then bill-vote, then NULL FKs.
   - Iterates `<members><member>` children into `ParsedVotePosition` list.
   - Maps `<count><absent>` to `not_voting_count`.
   - Detects `<en_bloc>` + empty `<question>`: sets `question = vote_title`, both FKs NULL.

10. **Helper: `_parse_senate_date(text)`.** Parses `"January 20, 2025,  06:12 PM"` (note double space). Localize via `zoneinfo.ZoneInfo("America/New_York")`. Return ISO8601 string with offset. Handle None/empty input gracefully (return None).

11. **Helper: `_build_amendment_id_from_xml(congress, amendment_number_text)`.** Parses `"S.Amdt. 14"` → `"119-samdt-14"`. Handles whitespace variation.

12. **Helper: `_build_bill_id_from_xml(congress, document_type, document_number)`.** Canonicalizes the senate.gov XML type forms (`"S."`, `"S.J.Res."`, `"H.R."`, etc.) to the Concord bill-type codes (`"s"`, `"sjres"`, `"hr"`, etc.). Returns `None` if `document_type` is `"PN"`, treaty type, or unrecognized.

13. **Test the senate_xml module.** Create `tests/test_senate_xml.py`. Cover with the spike-captured fixtures:
    - `parse_vote_menu(vote_menu_119_1.xml)` returns 659 entries ascending.
    - `parse_senate_roster(senators_cfm.xml)` returns a non-empty dict with sample senators present.
    - `parse_vote_detail(detail_119_1_00007_bill.xml)` returns: `bill_id="119-s-5"`, `amendment_id=None`, `threshold="simple_majority"`, `yea_count=64`, `nay_count=35`, `vote_kind="standard"`, positions list with 99+ entries including `member_full="Alsobrooks (D-MD)"`, `lis_member_id="S428"`.
    - `parse_vote_detail(detail_119_1_00003_amendment.xml)` returns: `amendment_id="119-samdt-14"`, `bill_id="119-s-5"` (the underlying), `threshold` per fixture.
    - `parse_vote_detail(detail_119_1_00008_nomination.xml)` returns: both `bill_id` and `amendment_id` NULL, `question` carrying the nomination text.
    - `parse_vote_detail(detail_119_1_00001_cloture.xml)` returns: `threshold="three_fifths"`, `bill_id="119-s-5"`, `amendment_id=None`, `vote_type="On the Cloture Motion"`.
    - `_parse_senate_date("January 20, 2025,  06:12 PM")` returns `"2025-01-20T18:12:00-05:00"`.
    - `SenateClient.list_roll_call_numbers` against a mock transport returns the expected list.
    - `SenateClient.get_roll_call_xml` raises `SenateXmlError` when the mock transport returns `Content-Type: text/html`.

### Section 3 — Models

14. **Add `ParsedVoteDetail` and `ParsedVotePosition` pydantic models** to [src/concord/models.py](../../src/concord/models.py). These mirror Phase 3a's `Vote` and `VotePosition` shape but carry an unresolved `member_full` instead of `bioguide_id` (the loader resolves later).

### Section 4 — Storage

15. **Update `member_party_unity` schema in `_BASE_SCHEMA`.** In [src/concord/storage/sqlite.py](../../src/concord/storage/sqlite.py), change the CREATE TABLE block to add `chamber TEXT NOT NULL CHECK (chamber IN ('house', 'senate'))` and update the PK to `(bioguide_id, congress, chamber)`.

16. **Update `_MEMBER_PARTY_UNITY_COLUMNS` and `_MEMBER_PARTY_UNITY_UPSERT_SQL`** to include the new column.

17. **Update `get_party_unity_for_member`** to return rows ordered by `congress DESC, chamber`.

18. **Test storage changes.** Extend [tests/test_storage_votes_sqlite.py](../../tests/test_storage_votes_sqlite.py) with:
    - Schema CHECK rejects rows with invalid `chamber`.
    - PK accepts the same `(bioguide_id, congress)` with different `chamber`.
    - `get_party_unity_for_member` returns ordered rows correctly when both chambers present.

### Section 5 — Scraper

19. **Add `scrape_senate` to `src/concord/scraper/votes.py`.** Function signature in [Approach > Scraper structure](#scraper-structure). Driver: fetch roster once at start; for each `(c, s)` pair fetch the menu; for each roll fetch the detail XML; sleep 0.1s between detail fetches; append envelopes to the two JSONL files; emit `ScrapeProgressEvent(chamber='senate', ...)`.

20. **Verify in `tests/test_scraper_votes.py`** (extended from 3a). Cases:
    - Senate scrape writes exactly one roster envelope and N detail envelopes for N rolls.
    - `--limit N` honored on detail fetches; roster always fetched.
    - `chamber='senate'` in every detail-envelope key.
    - Mock transport simulates a roll that returns HTML 404 — the scraper raises `SenateXmlError` and does not corrupt the JSONL files (no partial line).

### Section 6 — Loader

21. **Add `_build_member_bridge(storage_dir, db_conn)` helper to `src/concord/pipeline/load_votes.py`.** Reads `data/senate_roster.jsonl` latest snapshot, parses, builds the `member_full → bioguide_id` dict.

22. **Add `_resolve_bioguide(member_full, vote_date, roster_bridge, db_conn)` helper.** Per [Approach > Member bridge](#member-bridge). Returns Bioguide ID or None.

23. **Add `_load_senate(storage_dir, db_path, limit)` driver.** Reads `data/senate_votes.jsonl`, groups by key, keeps latest, parses XML, upserts `votes` row, resolves positions, upserts `vote_positions` rows. Logs structured warnings on unresolved members. Returns sub-counts merged into the overall `LoadStats`.

24. **Wire `_load_senate` into the main `load(...)` function.** Calls happen after the House branch. Both branches share the same `LoadStats` accumulator.

25. **Test the loader with Senate fixtures.** Extend [tests/test_pipeline_votes.py](../../tests/test_pipeline_votes.py). Use the spike fixtures + a synthetic `senators_cfm.xml`-shaped JSONL snapshot:
    - Bill vote (roll 7) loads with `bill_id="119-s-5"`.
    - Amendment vote (roll 3) loads with both FKs populated.
    - Nomination vote (roll 8) loads with both FKs NULL.
    - Cloture vote (roll 1) loads with threshold `three_fifths`.
    - Position resolution succeeds for a senator in the roster.
    - Position resolution falls back to `members`-table query for a synthetic "former senator" not in the roster but present in `members`.
    - Position resolution fails gracefully for a senator missing from both — vote loads, position is skipped, warning logged.
    - Re-running `load()` over unchanged JSONL is idempotent.

### Section 7 — Indexer

26. **Update `index_votes.py` to group party-unity computation by chamber.** Change the `member_party_unity` rebuild CTE: `GROUP BY vp.bioguide_id, v.congress, v.chamber`. Insert into the new 4-column table.

27. **Test the indexer with mixed-chamber data.** Extend [tests/test_index_votes.py](../../tests/test_index_votes.py):
    - Synthetic data: one House party-unity vote + one Senate party-unity vote + members in each chamber.
    - After index: `member_party_unity` rows exist for both chambers; each row's numerator/denominator reflects only their chamber's votes.
    - Synthetic chamber-switcher (rare case): same `bioguide_id` has positions in both chambers in one Congress → two rows post-index.

### Section 8 — CLI

28. **Update CLI defaults and remove the no-op.** In [src/concord/cli.py](../../src/concord/cli.py): change `DEFAULT_VOTES_CHAMBERS` to `("house", "senate")`; remove the "Senate ingest lands in Phase 3b — skipping." log path; wire `scrape_senate(client_xml=SenateClient(), ...)` into the senate slice; update `run_votes_command` to construct both `Client` and `SenateClient` as needed.

29. **Test the CLI changes.** Extend [tests/test_cli.py](../../tests/test_cli.py):
    - `concord scrape votes --congresses 119 --sessions 1 --limit 2` (no `--chambers` flag) hits both api.congress.gov and senate.gov (verified via mocked transports), writing to `data/house_votes.jsonl`, `data/house_vote_positions.jsonl`, `data/senate_votes.jsonl`, `data/senate_roster.jsonl`.
    - `concord scrape votes --chambers house` only writes House files.
    - `concord scrape votes --chambers senate` only writes Senate files.

### Section 9 — Web

30. **Remove the senate-chamber Phase-3b body branch in `votes/profile.html`.** The template now renders both chambers identically.

31. **Remove the chamber-based template branches in `members/profile.html`.** Both Recent-votes and Party-unity-score sections render uniformly. Drop all "(Phase 3b)" labels.

32. **Update the route handlers in `app.py`.** Drop the senate-placeholder branch in the vote-profile handler; drop the chamber check in the member-profile handler that gated Recent-votes / Party-unity display.

33. **Render multiple party-unity rows per Congress** (one per chamber the member served in). Template iterates the list returned by `get_party_unity_for_member`. Labels: "House: voted with…" or "Senate: voted with…" per row. For members in one chamber per Congress, looks identical to Phase 3a.

34. **Update `/about/methodology` page.** Add a paragraph explaining the chamber-scoping refinement: each chamber's score is computed independently; chamber-switchers see two scores per Congress.

35. **Web tests.** Extend [tests/test_web_votes.py](../../tests/test_web_votes.py):
    - `/votes/senate/119/1/7` returns 200, renders subject + positions (no more "(Phase 3b)" body).
    - `/members/{senator_bioguide}` shows live Recent votes + Party-unity sections (no more placeholder).
    - `/about/methodology` mentions chamber-scoping.

### Section 10 — Verification

36. **End-to-end smoke test.** Extend `tests/test_smoke.py`. Synthesize one Senate vote + one House vote + a synthetic roster, run load + index, open the web app via `TestClient`, hit `/votes`, `/votes/senate/119/1/7`, `/votes/house/119/1/240`, `/members/{senator_bioguide}`, `/bills/119/s/5` (vote history should now include the Senate vote).

37. **Manual smoke against live data.** Delete `data/proceedings.db`; run `concord load proceedings && concord load members && concord load bills && concord load votes && concord index votes`. Then `concord run votes --congresses 119 --sessions 1 --limit 50` (now hits both chambers by default). Then `concord serve`. Click through: a Senate Vote profile, a senator's Member profile (verify Recent votes + Party-unity render), a bill that had Senate votes (verify Vote history includes them).

38. **Delete the spike files.** `rm docs/plans/.spike-senate-votes.md docs/plans/.spike-senate-votes-findings.md`.

39. **Run the full test suite.** `pytest` clean. `uv run ruff check`. `uv run ruff format --check`. **Phase 1, 2a, and 3a tests must continue to pass** — especially `tests/test_web_members.py` (Senate-member placeholder branch is gone), `tests/test_web_votes.py` (senate-chamber Phase-3b body branch is gone), and `tests/test_index_votes.py` (`member_party_unity` rows now have a `chamber` column).

40. **Update CONTEXT.md and ADR 0011 final pass.** Verify the new CONTEXT.md entries are present; verify ADR 0011 has its refinement section.

## Demo seed data

Concord's demo derives from `data/*.jsonl`. After step 39 lands, the operator runs `concord run votes --congresses 119 --sessions 1 --limit 100` once locally — now hits both House (via the API) and Senate (via senate.gov). Populates `data/house_votes.jsonl`, `data/house_vote_positions.jsonl`, `data/senate_votes.jsonl`, `data/senate_roster.jsonl`, and the three SQLite tables. Demo mode now illustrates: a Senate Vote profile with a full 100-row position roster, a Bill page with both House and Senate vote history, a Senate Member page with live Recent votes and a chamber-labeled Party-unity score.

## Testing strategy

**Unit tests:**

- [tests/test_senate_xml.py](../../tests/test_senate_xml.py) (new) — every parsing function exercised against spike fixtures; every position value across captured fixtures present in `parse_vote_detail` output; date parser handles the double-space format; XML/HTML content-type detection.
- [tests/test_storage_votes_sqlite.py](../../tests/test_storage_votes_sqlite.py) (extended) — new `chamber` CHECK constraint; PK uniqueness across chambers.

**Integration tests:**

- [tests/test_scraper_votes.py](../../tests/test_scraper_votes.py) (extended) — `scrape_senate(...)` writes exactly two files (roster + votes); `--limit` honored on detail fetches but roster always written; chamber='senate' in keys; HTML-404 trap raises cleanly.
- [tests/test_pipeline_votes.py](../../tests/test_pipeline_votes.py) (extended) — Senate bill / amendment / nomination / cloture / en-bloc loads; member-bridge resolution (current via roster, historical via members table, unresolved skip-and-warn); idempotency.
- [tests/test_index_votes.py](../../tests/test_index_votes.py) (extended) — chamber-scoped `member_party_unity`; chamber-switcher case.
- [tests/test_cli.py](../../tests/test_cli.py) (extended) — both chambers run by default; `--chambers house` / `--chambers senate` work independently.
- [tests/test_web_votes.py](../../tests/test_web_votes.py) (extended) — Senate Vote profile renders; Senate Member profile renders; methodology page updated.

**Smoke test:** [tests/test_smoke.py](../../tests/test_smoke.py) extended per step 36.

**Manual checks** (per [ADR 0001](../adr/0001-python-end-to-end-for-the-web-layer.md)):

- `/votes/senate/119/1/7` shows full position roster, correct totals (64-35), threshold "simple majority", subject linking to S. 5.
- `/votes/senate/119/1/3` shows amendment chip "Amendment SAMDT 14" + link to S. 5.
- `/votes/senate/119/1/1` shows threshold "3/5" (cloture).
- `/votes/senate/119/1/8` shows nomination — no Bill link.
- A senator's `/members/{bioguide_id}` profile shows live Recent votes + Party-unity score.
- A chamber-switcher's profile shows two party-unity rows per Congress they served in both.
- `/bills/119/s/5` shows Senate votes in the Vote history section alongside any related amendment / motion votes.
- `/about/methodology` mentions the chamber-scoping.

**Regression risk:**

- Phase 3a tests must all pass. The `member_party_unity` schema rebuild means existing House-only rows are dropped and re-indexed on first 3b load+index — operators run the full re-index once when 3b lands.
- The `/votes/{chamber}/{congress}/{session}/{roll}` route no longer has a senate-chamber Phase-3b body branch. A test that asserted "GET /votes/senate/... shows the Phase 3b placeholder" must be updated to assert real rendering or be removed.
- Member-profile templates: tests asserting "(Phase 3b)" markers on Senate-member profiles must be updated.

## Acceptance criteria

- [ ] `concord scrape votes --congresses 119 --sessions 1 --limit 5 --storage-dir /tmp/concord` writes 5 detail envelopes to `data/senate_votes.jsonl`, 1 roster envelope to `data/senate_roster.jsonl`, AND (because the default is now both chambers) 5 detail+positions envelopes for House too.
- [ ] `concord scrape votes --chambers senate --congresses 119 --sessions 1 --limit 5` writes only the two Senate files; no House files.
- [ ] `concord scrape votes --chambers house --congresses 119 --sessions 1 --limit 5` writes only House files; no Senate files.
- [ ] `concord load votes --storage-dir /tmp/concord --db /tmp/test.db` populates `votes` with mixed-chamber rows and `vote_positions` with bioguide_id-keyed rows (verify a sample senator's bioguide_id is correctly resolved via the member bridge).
- [ ] `concord index votes --db /tmp/test.db` populates `votes.is_party_unity` for both chambers' votes and writes `member_party_unity` rows with the `chamber` column distinguishing senate and house entries for any chamber-switcher in the synthetic data.
- [ ] `concord run votes --congresses 119 --sessions 1 --limit 5` does scrape + load + index for both chambers in one shot.
- [ ] `concord scrape bills`, `concord scrape members`, `concord run proceedings` still work (Phase 1, 2a regression).
- [ ] `pytest` passes (full suite + new tests).
- [ ] `uv run ruff check` clean. `uv run ruff format --check` clean.
- [ ] `GET /votes/senate/119/1/7` returns 200 and renders subject (`S. 5`), totals (64-35), threshold (simple majority), full position roster (~100 senators), no "(Phase 3b)" placeholder body.
- [ ] `GET /votes/senate/119/1/3` returns 200 with amendment subject chip and Bill link.
- [ ] `GET /votes/senate/119/1/1` returns 200 with threshold "3/5".
- [ ] `GET /votes/senate/119/1/8` returns 200 for the nomination, with no Bill subject link, `question` carries the nominee.
- [ ] `GET /members/{senator_bioguide}` shows live Recent votes and a Party-unity score block; no "(Phase 3b)" placeholder.
- [ ] `GET /bills/119/s/5` Vote history section includes the Senate votes (rolls 1, 2, 3, 7 from the spike fixtures all linking back to S. 5).
- [ ] `GET /about/methodology` mentions chamber-scoping in the party-unity section.
- [ ] [CONTEXT.md](../../CONTEXT.md) has new entries for **LIS member ID** and **`member_full`**.
- [ ] [docs/adr/0011-party-unity-score-methodology.md](../adr/0011-party-unity-score-methodology.md) has a Phase 3b refinement section and an updated Status line.
- [ ] [docs/plans/.spike-senate-votes.md](./.spike-senate-votes.md) and [docs/plans/.spike-senate-votes-findings.md](./.spike-senate-votes-findings.md) deleted.
- [ ] `src/concord/senate_xml.py` exists with `SenateClient` + three parsing functions; no Phase 1 / 2a / 3a modules were moved or deleted.

## Open questions

None — all design decisions resolved during grilling.

The party-unity computation for **chamber-switchers within a Congress** (rare case: a House member appointed mid-term to fill a Senate vacancy) is handled by the schema (two `member_party_unity` rows, one per chamber) and the UI (two stat lines). This case is bookkeeping-correct but may need a UX revisit if it produces visually confusing profile pages — track as a UX nice-to-have, not a blocker.

## Out-of-band work

- **Full 117–119 Senate backfill.** `concord run votes --congresses 117,118,119 --chambers senate` (no `--limit`) is a ~30-minute command-line job (3600 detail fetches at ~0.4s). Run on the VPS once 3b's surface review is done.
- **Combined Senate + House backfill.** `concord run votes --congresses 117,118,119` (default chambers, no `--limit`) is the ~12.5h job (~12h House from 3a + ~0.5h Senate). One operator command.
- **SQLite re-index on 3b deploy.** Operators must delete `data/proceedings.db` and re-run all `concord load *` + `concord index *` commands once when 3b lands, to pick up the new `member_party_unity` schema. Document in the deploy checklist.
- **`vote_matters` side table for en-bloc votes.** Deferred to v2; purely additive when it lands.
- **Phase 4 — Committees + Amendments.** Will turn `votes.amendment_id` into a live link to an `/amendments/{...}` profile page. Senate amendment IDs (`samdt`) and House amendment IDs (`hamdt`) both resolve through that surface.
- **Daily refresh cadence.** A cron job invoking `concord run votes --congresses 119` daily — out of band, operator-configured on the VPS. Not part of this plan.
