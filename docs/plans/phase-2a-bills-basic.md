# Phase 2a — Bills (basic) ingest

> Ingest U.S. Congress Bill identity records (Bill metadata + sponsor + latest action) from api.congress.gov into Concord, surface them on the public web app as `/bills` (index) and `/bills/{congress}/{bill_type}/{bill_number}` (profile), federate Bills into the existing `/search` route, and add a "Sponsored bills" cross-link on the Member profile page.

## Source

- Roadmap: [docs/plans/members-bills-votes-roadmap.md](./members-bills-votes-roadmap.md) (Phase 2 section)
- Sibling plan (follow-up): [docs/plans/phase-2b-bills-enrichment.md](./phase-2b-bills-enrichment.md) — adds cosponsors, actions, subjects, titles, summaries on top of 2a's foundation.
- Phase 1 exemplar (template for this plan's structure and patterns): [docs/plans/phase-1-members.md](./phase-1-members.md)
- Scoping conversation: [docs/plans/members-bills-votes-scope.md](./members-bills-votes-scope.md)

## Context

Concord's first peer-entity expansion — Phase 1 — added **Members**. Phase 2 adds **Bills**, the second peer entity. The full Phase 2 work has been split into two parts so each ships an independently-useful surface and stays reviewable:

- **Phase 2a (this plan).** Bulk-fetch the identity of every Bill in 117–119 from two endpoints (the list endpoint plus the per-bill detail endpoint). Render `/bills` index, a basic Bill profile page (header + sponsor block, with placeholder sections for everything 2b adds), and federate Bills into `/search`. After 2a a journalist can browse every Bill, find them by title or identifier, and click through from a Member's profile to the bills they introduced.
- **Phase 2b.** Per-Bill enrichment via the five sub-endpoints (cosponsors, actions, subjects, titles, summaries), fetched ad-hoc. Adds the political-graph data — cosponsor lists, action history, CRS summaries — and the Cosponsored cross-link on the Member page.

A Bill in the U.S. Congress is a piece of legislation introduced in either chamber. It has one **Sponsor** (the introducing Member) and one **Policy area** (a CRS-assigned single subject). Both terms are defined in the "Entities" section of [CONTEXT.md](../../CONTEXT.md), added as part of this plan's grilling pass. Bills also have cosponsors, actions, and subjects — *those* are Phase 2b's domain.

## Goals

1. The Tier 1 scraper fetches, for every Bill in Congresses 117, 118, and 119, two endpoints — the list endpoint `/v3/bill/{c}/{t}` (stubs, for discovery; not persisted) and the detail endpoint `/v3/bill/{c}/{t}/{n}` (full identity record). Appends one ADR 0006 snapshot envelope per detail fetch to `data/bills.jsonl`.
2. The loader projects the latest snapshot per Bill into a `bills` SQLite table and populates a `bills_fts` FTS5 virtual table over searchable Bill text (title, identifier, policy area).
3. The web app serves:
   - `GET /bills` — paginated browse, default sort by latest-action-date DESC, filters for `chamber`, `policy_area`, `congress`, `sponsor`.
   - `GET /bills/{congress}/{bill_type}/{bill_number}` — Bill profile with title + sponsor + policy area + latest action + an "Updated:" staleness header. Five Phase-2b sections render as muted placeholder boxes (matching the Phase 1 placeholder convention).
4. The existing `/members/{bioguide_id}` profile gets a **Sponsored bills** section (live query on `bills.sponsor_bioguide_id`). The Cosponsored section remains a Phase-2b placeholder.
5. The existing `/search` route gains a third section, **Bills (N)**, alongside Members and Proceedings. A bare-identifier query (e.g. `"HR 1234"` or `"S 47"`) parses to a direct lookup and redirects to the matching Bill page.
6. CLI follows the [ADR 0007](../adr/0007-parallel-pipelines-per-entity.md) pattern set in Phase 1: `concord scrape bills`, `concord load bills`, `concord index bills`, `concord run bills`. **Every command takes a `--limit N`** for dev-time slicing.
7. The code layout matches ADR 0007 literally: `src/concord/scraper/bills.py` (with one entry point `scrape_basic`; `scrape_enrichment` lands in 2b alongside it) and `src/concord/pipeline/{load,index}_bills.py`.
8. Bill→Member columns store bare-TEXT `bioguide_id` (no `REFERENCES members`) so ingest is robust to any Phase 1 gap.
9. All existing tests continue to pass.

## Non-goals

1. **All Phase 2b scope.** Cosponsors, actions, subjects, titles, summaries — neither scraped, loaded, nor surfaced beyond placeholders. The five sub-endpoint API client methods aren't added in 2a. The five child tables don't exist yet. The five `*_fetched_at` columns on `bills` aren't added until 2b.
2. **Bill text RAG.** Phase 5 generalizes the chunks table per [ADR 0008](../adr/0008-chunks-generalize-beyond-proceedings.md). Phase 2a is metadata only; no `chunks.source_type = 'bill'` rows are written.
3. **Votes, Committees, Amendments.** Phases 3 and 4. Bill page placeholders only.
4. **CBO cost estimates, constitutional authority statements, notes, committee reports, related bills, text versions, amendments references.** Available via `/v3/bill/{c}/{t}/{n}` but not surfaced; preserved in the JSONL payload for any future use.
5. **Denormalized sponsor counts on `members`.** The Sponsored cross-link uses a live query with the `idx_bills_sponsor` index — sub-millisecond at v1 scale.
6. **Auto-release / cron scheduling.** Plan lands commands; scheduling is out-of-band, same stance as Phase 1.
7. **`Bill` as a Pydantic model with deep validation of every API field.** Cover only the fields the loader writes; pass everything else through verbatim into JSONL.

## Relevant prior decisions

- [ADR 0001 — Python end-to-end for the web layer](../adr/0001-python-end-to-end-for-the-web-layer.md) — `/bills` and `/bills/{...}` extend the existing FastAPI+Jinja2+HTMX surface.
- [ADR 0002 — JSONL as canonical raw store](../adr/0002-jsonl-as-canonical-raw-store.md) — Bills go to `data/bills.jsonl`.
- [ADR 0003 — SQLite as derived store](../adr/0003-sqlite-as-derived-store.md) — Bills tables live in the existing `data/proceedings.db`. Important here: 2b will add schema; cost is "delete the SQLite file and re-load from JSONL," which is what this ADR explicitly enables.
- [ADR 0005 — Chunks as the unit of retrieval](../adr/0005-chunks-as-unit-of-retrieval.md) — does **not** apply to Phase 2a or 2b (Bills aren't chunked here; that's Phase 5).
- [ADR 0006 — Snapshot-on-fetch for mutable entities](../adr/0006-snapshot-on-fetch-for-mutable-entities.md) — Bills are mutable; every fetch appends a snapshot. The `key` is `{"congress", "bill_type", "bill_number"}` per the ADR.
- [ADR 0007 — Parallel pipelines per entity](../adr/0007-parallel-pipelines-per-entity.md) — `src/concord/scraper/bills.py`, `src/concord/pipeline/{load,index}_bills.py`, and the `concord <stage> bills` CLI verbs.
- [ADR 0008 — Chunks generalize beyond Proceedings](../adr/0008-chunks-generalize-beyond-proceedings.md) — not exercised; referenced because the `bill_id` PK shape (`"<congress>-<type>-<number>"`) is chosen to match ADR 0008's named `source_id` format so Phase 5 chunks linkage is mechanical.
- [ADR 0009 — Multi-endpoint entities split their JSONL canonical store by sub-endpoint](../adr/0009-multi-endpoint-entities-split-jsonl.md) — committed with this work. Phase 2a only writes one of the six files (`data/bills.jsonl`); Phase 2b adds the other five. The naming and key convention is settled now so 2b is purely additive.

## Relevant files and code

Files to **read** for context:

- [CONTEXT.md](../../CONTEXT.md) — new Bill / Bill type / Sponsor / Policy area entries (the Cosponsor / Bill action entries are already in CONTEXT.md but stay unused until 2b).
- [src/concord/api.py](../../src/concord/api.py) — `list_members` (line 181) is the pagination template for `list_bills`.
- [src/concord/models.py](../../src/concord/models.py) — `Member` and `Term` (~lines 190 and 280) demonstrate the API-field aliasing pattern.
- [src/concord/scraper/members.py](../../src/concord/scraper/members.py) — Phase 1 scraper. Mirror the `scrape(...) -> int` shape and `ScrapeProgressEvent` pattern.
- [src/concord/pipeline/load_members.py](../../src/concord/pipeline/load_members.py) — "latest snapshot per key" projection pattern, replicated 1× in 2a (six times eventually in 2b).
- [src/concord/pipeline/index_members.py](../../src/concord/pipeline/index_members.py) — truncate-then-repopulate FTS5 pattern.
- [src/concord/storage/sqlite.py](../../src/concord/storage/sqlite.py) — `_BASE_SCHEMA` (line ~59) gets two new tables (`bills`, `bills_fts`). The `upsert_member` (line ~375) is the model for `upsert_bill`.
- [src/concord/web/app.py](../../src/concord/web/app.py) — routes at lines 139 (`/search`), 234 (`/members`), 267 (`/members/{id}`). `/bills*` routes added here.
- [src/concord/web/search.py](../../src/concord/web/search.py) — `search_members` (line ~279) and `list_current_members` (line ~367) are templates for `search_bills` and `list_bills`.
- [src/concord/web/templates/members/](../../src/concord/web/templates/members/) — Phase 1 template layout. A parallel `bills/` directory is added in 2a.
- [src/concord/web/templates/base.html](../../src/concord/web/templates/base.html) — global nav; gets a "Bills" link.
- [src/concord/cli.py](../../src/concord/cli.py) — Members commands (lines 682–805) are the literal template.
- [tests/_snapshots.py](../../tests/_snapshots.py) — shared ADR 0006 envelope helper; 2a tests use it unchanged.
- [tests/test_scraper_members.py](../../tests/test_scraper_members.py), [tests/test_pipeline_members.py](../../tests/test_pipeline_members.py) — patterns.

Files to **create**:

- `src/concord/scraper/bills.py` — Tier 1 orchestrator (`scrape_basic`); 2b adds `scrape_enrichment` to the same file.
- `src/concord/pipeline/load_bills.py` — Stage 1 loader; reads `data/bills.jsonl`, projects to the `bills` table. 2b extends this same file.
- `src/concord/pipeline/index_bills.py` — Stage 2 FTS5 indexer.
- `src/concord/web/templates/bills/list.html`
- `src/concord/web/templates/bills/profile.html` — header + sponsor + 5 placeholder section boxes.
- `tests/fixtures/api/bills/` — captured fixtures (list page + 2 detail responses).
- `tests/test_models_bills.py`
- `tests/test_storage_bills_sqlite.py`
- `tests/test_scraper_bills.py`
- `tests/test_pipeline_bills.py`
- `tests/test_web_bills.py`

Files to **modify**:

- `src/concord/api.py` — add `BILLS_PAGE_SIZE = 250` and two methods: `list_bills(congress, bill_type) -> Iterator[dict]`, `get_bill_detail(congress, bill_type, bill_number) -> dict`.
- `src/concord/models.py` — add `Bill` and `BillSnapshot`.
- `src/concord/storage/sqlite.py` — append two table definitions to `_BASE_SCHEMA`; add `upsert_bill`, `get_bill`, supporting writers/readers.
- `src/concord/web/app.py` — register `/bills` and `/bills/{c}/{t}/{n}` routes; extend `/search` handler to call `search_bills`; parse bare-identifier queries.
- `src/concord/web/search.py` — add `search_bills`, `list_bills`, `get_bill`, `sponsored_bills_for_member`.
- `src/concord/web/templates/members/profile.html` — replace the Sponsored placeholder with a live list; leave Cosponsored as a placeholder labeled "(Phase 2b)".
- `src/concord/web/templates/base.html` — add `/bills` link.
- `src/concord/cli.py` — add `scrape_bills_command`, `load_bills_command`, `index_bills_command`, `run_bills_command`. Each takes `--limit N`.

## Approach

### Domain model

A Bill is identified by `(congress, bill_type, bill_number)` per [ADR 0006](../adr/0006-snapshot-on-fetch-for-mutable-entities.md). Phase 2a introduces a flattened internal id, `bill_id = "{congress}-{bill_type_lower}-{bill_number}"` (e.g. `"119-hr-1234"`), as the SQLite primary key. The flattened form serves three purposes: child tables in 2b need a single FK column; the format matches the named `source_id` shape in [ADR 0008](../adr/0008-chunks-generalize-beyond-proceedings.md) for Phase 5; URL routes still take the three components explicitly (`/bills/{congress}/{bill_type}/{bill_number}`).

`bill_type` is canonicalized to lowercase on ingest. The API returns `"HR"`, `"S"`, `"HJRES"`, etc.; everything downstream uses the lowercase form.

`sponsor` lives on the `bills` row as a single `sponsor_bioguide_id TEXT` column — bills have one sponsor by Congress's rules. The cosponsor M:N edge is Phase 2b's domain.

`latest_action_date` and `latest_action_text` are denormalized onto `bills` from the `latestAction` field on the detail endpoint. This lets the `/bills` index sort by recency without joining anything else.

### Storage shape

Phase 2a writes one JSONL file:

- `data/bills.jsonl` — `payload` is the `/v3/bill/{c}/{t}/{n}` detail body's `bill` object. ADR 0006 envelope:

```json
{"fetched_at": "2026-05-25T14:02:11Z", "key": {"congress": 119, "bill_type": "hr", "bill_number": 1234}, "payload": {...}}
```

SQLite schema appended to `_BASE_SCHEMA` in [src/concord/storage/sqlite.py](../../src/concord/storage/sqlite.py):

```sql
CREATE TABLE IF NOT EXISTS bills (
  bill_id              TEXT PRIMARY KEY,            -- "{congress}-{bill_type}-{bill_number}"
  congress             INTEGER NOT NULL,
  bill_type            TEXT NOT NULL
    CHECK (bill_type IN ('hr', 'hres', 'hjres', 'hconres', 's', 'sres', 'sjres', 'sconres')),
  bill_number          INTEGER NOT NULL,
  origin_chamber       TEXT NOT NULL
    CHECK (origin_chamber IN ('House', 'Senate')),
  title                TEXT NOT NULL,
  introduced_date      TEXT,                        -- ISO YYYY-MM-DD
  policy_area          TEXT,                        -- single CRS policy area name
  sponsor_bioguide_id  TEXT,                        -- bare TEXT, no FK
  latest_action_date   TEXT,                        -- ISO; denormalized from /bill detail
  latest_action_text   TEXT,                        -- denormalized from /bill detail
  update_date          TEXT NOT NULL,               -- API's updateDate
  fetched_at           TEXT NOT NULL,               -- latest detail-snapshot timestamp
  UNIQUE (congress, bill_type, bill_number)
);

CREATE INDEX IF NOT EXISTS idx_bills_sponsor       ON bills (sponsor_bioguide_id);
CREATE INDEX IF NOT EXISTS idx_bills_latest_action ON bills (latest_action_date DESC);
CREATE INDEX IF NOT EXISTS idx_bills_policy_area   ON bills (policy_area);
CREATE INDEX IF NOT EXISTS idx_bills_congress      ON bills (congress);

CREATE VIRTUAL TABLE IF NOT EXISTS bills_fts USING fts5(
  bill_id UNINDEXED,
  identifier,        -- "hr 1234"
  title,
  policy_area,
  tokenize = 'porter'
);
```

Phase 2b will ALTER this schema by adding five `*_fetched_at` columns and five new child tables (`bill_cosponsors`, `bill_actions`, `bill_subjects`, `bill_titles`, `bill_summaries`). The schema change is purely additive; per [ADR 0003](../adr/0003-sqlite-as-derived-store.md) the SQLite file is regenerable, so the 2b migration is `rm data/proceedings.db && concord load proceedings && concord load bills`. The `bills_fts` virtual table also gains `short_title` and `subjects` columns in 2b; that requires `DROP VIRTUAL TABLE bills_fts` and recreating, but `index_bills` already repopulates from scratch so it's a no-op for the operator.

### Scraper structure

`src/concord/scraper/bills.py` exports one entry point in 2a — `scrape_basic(client, congresses, storage_dir, *, fetched_at, bill_types=None, limit=None, progress=None) -> ScrapeStats`. The driver:

1. For each `(congress, bill_type)` pair (default: all 8 types × passed congresses), paginate `/v3/bill/{congress}/{bill_type}` and collect bill stubs. The stub list is *not* persisted — it's a discovery mechanism.
2. For each stub, fetch `/v3/bill/{c}/{t}/{n}` and append one snapshot envelope to `data/bills.jsonl`.
3. **Stop after `limit` bills have been written**, if `limit` is set. The CLI uses this for dev-time slicing (`concord scrape bills --limit 10`).

Cost for full 117–119 backfill: ~60K calls, ~12 hours at 5K req/hr, ~500 MB JSONL.

Phase 2b adds `scrape_enrichment` to the same file, side-by-side with `scrape_basic`.

### Loader

`src/concord/pipeline/load_bills.py` exports `load(storage_dir: Path, db_path: Path, *, limit: int | None = None) -> LoadStats`. Reads `data/bills.jsonl`, groups by `(congress, bill_type, bill_number)`, keeps the latest snapshot per key, and upserts into `bills`. Idempotent. Stops after `limit` bills if set.

Phase 2b extends this loader to also read the five tier-2 JSONL files and write the child tables. The 2a loader behavior is unchanged in 2b — re-running over an unchanged `bills.jsonl` is still a no-op.

### Indexer

`src/concord/pipeline/index_bills.py` exports `index(db_path: Path, *, limit: int | None = None) -> IndexStats`. Truncates and repopulates `bills_fts` with `(bill_id, identifier, title, policy_area)` from `bills`. The `identifier` is `f"{bill_type} {bill_number}"` (e.g. `"hr 1234"`).

Phase 2b extends the indexer to also populate `short_title` and `subjects` columns. 2a's index does not include them.

### Web surface

**`/bills` index.** Template `bills/list.html`. Default sort `ORDER BY latest_action_date DESC NULLS LAST`. Filters: `chamber`, `policy_area`, `congress`, `sponsor`. 50 per page. Row layout: chamber badge, identifier, title (truncated ~120 chars), policy area, latest action date + first line of latest action text, sponsor name linked to `/members/{bioguide_id}`.

**`/bills/{congress}/{bill_type}/{bill_number}` profile.** Template `bills/profile.html`. Sections:

1. **Header.** Identifier, full title, chamber badge, status (derived from `latest_action_text`), `Updated: {fetched_at}`.
2. **Sponsor.** Member card linking to `/members/{sponsor_bioguide_id}`; non-linked `Bioguide {id} (not indexed)` if the Member isn't in `members`.
3. **Policy area + introduced date.** Small metadata block.
4. **Phase 2b placeholders.** Five muted empty-state boxes labeled: "Cosponsors (Phase 2b)", "Action history (Phase 2b)", "Subjects (Phase 2b)", "Titles (Phase 2b)", "Summaries (Phase 2b)". Same muted styling as the Phase 1 placeholders on the Member profile.
5. **Later-phase placeholders.** "Vote history (Phase 3)", "Committee path (Phase 4)", "Search within this bill (Phase 5)".

**`/search` federation.** Add a Bills section between Members and Proceedings. `search_bills(db, query, limit=10)` runs FTS5 over `bills_fts` (`title + identifier + policy_area` in 2a). Also detect bare-identifier queries via regex `^\s*(HR|HRES|HJRES|HCONRES|S|SRES|SJRES|SCONRES)\.?\s*(\d+)\s*$` (case-insensitive). If matched and exactly one `bills` row exists across the three Congresses, redirect 307 to that Bill's page; if multiple, render all in the Bills section.

**Member profile cross-link.** Replace `members/profile.html`'s "Sponsored bills (Phase 2)" placeholder with a live list: top 25 by introduced date desc; "View all →" link to `/bills?sponsor={bioguide_id}`. The Cosponsored placeholder stays — relabeled to "Cosponsored bills (Phase 2b)" so the seam is honest.

### CLI

```
concord scrape bills [--congresses 117,118,119] [--bill-types hr,s,...]
                     [--storage-dir data/] [--limit N]
concord load bills   [--storage-dir data/] [--db data/proceedings.db] [--limit N]
concord index bills  [--db data/proceedings.db] [--limit N]
concord run bills    [--congresses 117,118,119] [--bill-types ...]
                     [--storage-dir data/] [--db ...] [--limit N]
```

Every command accepts `--limit N`. Semantics:

- `scrape bills` — stop after N detail responses written.
- `load bills` — stop after N bills rows upserted.
- `index bills` — cap on rows written to `bills_fts`.
- `run bills` — passed through to the inner scrape. Load and index run unbounded over whatever was written.

`--bill-types` defaults to all eight; the CSV parser added in `_parse_congresses` is generalized into `_parse_csv(raw, *, name, coerce)` to reuse for `--bill-types`.

`--storage-dir` (not `--storage`) because the 2b plan adds five more files in this directory. The dir defaults to `./data/`. Filenames are constants.

### Backfill operations

Phase 2a ships when the pipeline can scrape, load, index, and serve a `--limit`-bounded slice of Bills end-to-end. Cost envelope:

| Operation | Calls | Time @ 5K/hr | JSONL |
|---|---|---|---|
| Tier 1 backfill (117–119, all bill types) | ~60K | ~12h | ~500 MB |

Plan deliverables target *exercising* the pipeline against `--limit 50` locally, then `--limit 500` in staging, then unbounded on the VPS. Same operator-driven stance as Phase 1.

## Step-by-step plan

Seven sections — **API client**, **Models & storage**, **Ingest**, **CLI**, **Web — Bill pages**, **Web — federation & cross-links**, **Verification**.

### Section 1 — API client

1. **Add Bills constants and two endpoint methods.** In [src/concord/api.py](../../src/concord/api.py): add `BILLS_PAGE_SIZE = 250` next to `MEMBERS_PAGE_SIZE` (line 48). Add two methods to `Client`:
   - `list_bills(congress: int, bill_type: str) -> Iterator[dict]` — paginates `/v3/bill/{congress}/{bill_type}`. Yields stubs.
   - `get_bill_detail(congress, bill_type, bill_number) -> dict` — returns the `bill` object from `/v3/bill/{c}/{t}/{n}`.

   Both methods accept the API's uppercase `bill_type` and canonicalize to lowercase before URL-formatting. Reuse the existing `_get` retry/auth logic.

2. **Test the API client extensions.** Extend [tests/test_api.py](../../tests/test_api.py) with one test class per new method. Use captured fixtures (next section) + `httpx.MockTransport`; assert request paths, pagination termination, lowercase canonicalization.

### Section 2 — Models & storage

3. **Add Bill pydantic models.** Extend [src/concord/models.py](../../src/concord/models.py) with:
   - `Bill` — `bill_id`, `congress`, `bill_type`, `bill_number`, `origin_chamber`, `title`, `introduced_date`, `policy_area`, `sponsor_bioguide_id`, `latest_action_date`, `latest_action_text`, `update_date`. Includes a `@field_validator("bill_type", mode="before")` that lowercases.
   - `BillSnapshot` — generic ADR 0006 envelope (`fetched_at`, `key: dict[str, str | int]`, `payload: dict`).
   - `bill_id_from_components(congress: int, bill_type: str, bill_number: int) -> str` helper returning `f"{congress}-{bill_type.lower()}-{bill_number}"`.

4. **Test the models.** Create [tests/test_models_bills.py](../../tests/test_models_bills.py). Cover: parsing each captured fixture, `bill_id_from_components` round-trip, `bill_type` lowercase canonicalization, `BillSnapshot` round-trip.

5. **Add Bills schema to SqliteStorage.** Append the `bills` table + indexes + `bills_fts` virtual table from [Approach > Storage shape](#storage-shape) to `_BASE_SCHEMA` in [src/concord/storage/sqlite.py](../../src/concord/storage/sqlite.py). Add `_BILL_COLUMNS` tuple + `_BILL_UPSERT_SQL` mirroring the `_MEMBER_COLUMNS` pattern.

6. **Add storage methods.** In [src/concord/storage/sqlite.py](../../src/concord/storage/sqlite.py), add:
   - `upsert_bill(bill: Bill, *, fetched_at: str) -> None`
   - `get_bill(bill_id: str) -> sqlite3.Row | None`

   Write tests in [tests/test_storage_bills_sqlite.py](../../tests/test_storage_bills_sqlite.py): UPSERT idempotency on `bill_id`, the `bill_type` CHECK rejecting invalid values, the `origin_chamber` CHECK.

### Section 3 — Ingest

7. **Capture API fixtures.** With `CONGRESS_API_KEY` set, fetch and save under `tests/fixtures/api/bills/`:
   - `list_hr_119.json` — `/v3/bill/119/hr?limit=2` (two stubs)
   - `detail_119_hr_1.json` — `/v3/bill/119/hr/1` (enacted bill)
   - `detail_119_hr_22.json` — `/v3/bill/119/hr/22` (different sponsor, different policy area)

8. **Build the Bills scraper (Tier 1).** Create [src/concord/scraper/bills.py](../../src/concord/scraper/bills.py) with `scrape_basic(client, congresses, storage_dir, *, fetched_at, bill_types=None, limit=None, progress=None) -> ScrapeStats`. Per [Approach > Scraper structure](#scraper-structure): paginate the list endpoint, fetch detail per stub, append one envelope to `data/bills.jsonl` per bill, stop at `limit`. Emit `ScrapeProgressEvent(congress, bill_type, bills_seen, bills_written)` once per `(congress, bill_type)` pair. Verify in [tests/test_scraper_bills.py](../../tests/test_scraper_bills.py): (a) only `bills.jsonl` is written, (b) one envelope per detail-fetched bill, (c) `bill_type` lowercased in keys, (d) `--limit` honored.

9. **Build the Bills loader.** Create [src/concord/pipeline/load_bills.py](../../src/concord/pipeline/load_bills.py) with `load(storage_dir, db_path, *, limit=None) -> LoadStats`. Read `data/bills.jsonl`, group by key, keep latest per key, upsert into `bills`. `LoadStats` carries `bills_written, snapshots_read, malformed`. Verify in [tests/test_pipeline_bills.py](../../tests/test_pipeline_bills.py): (a) basic load, (b) two snapshots same key — latest wins, (c) `--limit` honored, (d) re-run is idempotent.

10. **Build the Bills FTS5 indexer.** Create [src/concord/pipeline/index_bills.py](../../src/concord/pipeline/index_bills.py) with `index(db_path, *, limit=None) -> IndexStats`. Truncate and repopulate `bills_fts` with `(bill_id, identifier, title, policy_area)`. `identifier = f"{bill_type} {bill_number}"`. Verify: FTS5 row count = `bills` row count.

### Section 4 — CLI commands

11. **Add `concord scrape bills`.** In [src/concord/cli.py](../../src/concord/cli.py), add `DEFAULT_BILLS_STORAGE_DIR = Path("./data")` and `DEFAULT_BILL_TYPES = ("hr", "hres", "hjres", "hconres", "s", "sres", "sjres", "sconres")`. Generalize `_parse_congresses` to `_parse_csv(raw, *, name, coerce)` and add `_parse_bill_types`. Register `@scrape_app.command("bills")` with options `--congresses` (default `117,118,119`), `--bill-types` (default all eight), `--storage-dir`, `--limit`, `--progress/--no-progress`. Mirror `Progress` wiring from `scrape_members_command`. Calls `scrape_basic(...)`.

12. **Add `concord load bills` / `concord index bills` / `concord run bills`.** Mirror the Members commands (cli.py lines 718–805). `load bills` takes `--storage-dir`, `--db`, `--limit`. `index bills` takes `--db`, `--limit`. `run bills` takes the scrape options plus `--db` and `--limit`; chains `scrape_basic` → `load` → `index`. Each no-ops with an informational message if its inputs are missing (matches Phase 1 `load_members`).

13. **Test the CLI.** Extend [tests/test_cli.py](../../tests/test_cli.py) with the help-smoke pattern. Add an end-to-end test: invoke `scrape bills --limit 2` with a mocked `Client`, assert exactly 2 envelopes hit `data/bills.jsonl`.

### Section 5 — Web: Bill pages

14. **Add Bills web search helpers.** Extend [src/concord/web/search.py](../../src/concord/web/search.py) with `BillHit` (pydantic), `search_bills(db, query, limit=10)`, `list_bills(db, *, chamber, policy_area, congress, sponsor_bioguide_id, limit, offset)`, `get_bill(db, congress, bill_type, bill_number)`, `sponsored_bills_for_member(db, bioguide_id, limit=25)`.

15. **Register `/bills` index route.** In [src/concord/web/app.py](../../src/concord/web/app.py), add `BILLS_PAGE_SIZE = 50`. Register `GET /bills` reading `chamber`, `policy_area`, `congress`, `sponsor`, `page`; calls `list_bills`; renders `bills/list.html`. Mirror the `/members` route at line 234.

16. **Register `/bills/{congress}/{bill_type}/{bill_number}` profile route.** Validate `bill_type ∈ DEFAULT_BILL_TYPES`, build `bill_id`, call `get_bill`, 404 if missing; otherwise render `bills/profile.html`.

17. **Add the templates.** Create [src/concord/web/templates/bills/list.html](../../src/concord/web/templates/bills/list.html) and [src/concord/web/templates/bills/profile.html](../../src/concord/web/templates/bills/profile.html). Profile template shows: header + sponsor block + policy-area/introduced-date metadata + the five Phase-2b placeholder boxes + the Phase-3/4/5 placeholders. Match the Phase 1 placeholder styling.

18. **Add the global nav link.** In [src/concord/web/templates/base.html](../../src/concord/web/templates/base.html), add `<a href="/bills">Bills</a>` next to the `/members` link.

### Section 6 — Web: federation & cross-links

19. **Federate `/search`.** Update `search_endpoint` at [src/concord/web/app.py:139](../../src/concord/web/app.py). New query param `bills` (default `"on"`). Run `search_bills(db, query, limit=10)` when on; pass `bill_hits` to context.

20. **Add the bare-identifier redirect.** In the same handler, regex-match `^\s*(HR|HRES|HJRES|HCONRES|S|SRES|SJRES|SCONRES)\.?\s*(\d+)\s*$` (case-insensitive). If matched: `SELECT congress FROM bills WHERE bill_type = ? AND bill_number = ? ORDER BY congress DESC`. Exactly one row → `307 → /bills/{c}/{t}/{n}`. Multiple → fall through and render all in the Bills section. Zero → fall through to FTS search.

21. **Update `/search` template.** In `src/concord/web/templates/search.html` and `_results.html`, add a **Bills (N)** section between Members and Proceedings. Add a `☑ Bills` checkbox next to the existing two. Section card: chamber chip, identifier, title, sponsor name, latest-action date.

22. **Update Member profile.** In [src/concord/web/templates/members/profile.html](../../src/concord/web/templates/members/profile.html), replace the "Sponsored bills (Phase 2)" placeholder with a live list (top 25 by introduced date desc; "View all →" link to `/bills?sponsor={bioguide_id}`). The Cosponsored placeholder stays — relabel to "Cosponsored bills (Phase 2b)". The handler at `app.py:267` calls `sponsored_bills_for_member` and passes it to the template.

### Section 7 — Verification

23. **End-to-end smoke test.** Extend [tests/test_smoke.py](../../tests/test_smoke.py) (or add `tests/test_smoke_bills.py`). Write synthetic snapshot JSONL for one bill to a temp dir, run `load_bills.load()` + `index_bills.index()`, open the web app via `TestClient`, hit:
   - `GET /bills` → 200, contains the bill's title.
   - `GET /bills/119/hr/1` → 200, contains sponsor name + Phase-2b placeholder markers.
   - `GET /search?q=hr+1` → 200 with Bills section OR 307 redirect to `/bills/119/hr/1`.
   - `GET /members/{sponsor_bioguide_id}` → 200, contains "Sponsored bills" section listing the bill.

24. **Manual smoke against live data.** With `CONGRESS_API_KEY` set: `concord scrape bills --congresses 119 --bill-types hr --limit 50`, then `concord load bills`, `concord index bills`, `concord serve`. Click through `/bills`, a Bill profile (verify the Phase-2b placeholders look right), `/search?q=infrastructure`, and a known sponsor's `/members/{id}`.

25. **Run the full test suite.** `pytest` clean. `uv run ruff check`. `uv run ruff format --check`.

## Demo seed data

No checked-in seed file — Concord's demo derives from `data/*.jsonl`. Equivalent: after step 25 lands, the operator runs `concord run bills --congresses 119 --limit 100` once locally to populate `data/bills.jsonl` and the new SQLite tables.

## Testing strategy

**Unit tests:**

- `tests/test_models_bills.py` — `Bill` and `BillSnapshot` round-trips, lowercase canonicalization, `bill_id_from_components`.
- `tests/test_storage_bills_sqlite.py` — schema applies cleanly, `upsert_bill` UPSERTs, `bill_type` and `origin_chamber` CHECK constraints reject bad values.

**Integration tests:**

- `tests/test_api.py` extended — `list_bills` + `get_bill_detail` against `httpx.MockTransport`.
- `tests/test_scraper_bills.py` — `scrape_basic(...)` writes one file, correct envelopes, `--limit` honored.
- `tests/test_pipeline_bills.py` — `load(...)` projects `bills.jsonl` to `bills`; latest-snapshot-per-key wins; idempotent re-runs.
- `tests/test_web_bills.py` — `/bills` with each filter combo; `/bills/{...}` for a real bill, invalid `bill_type`, unknown number (404); federated `/search` includes Bills section; bare-identifier redirect; Member profile Sponsored section renders.

**Smoke test:** `tests/test_smoke.py` extended per step 23.

**Manual checks** (no frontend component tests per [ADR 0001](../adr/0001-python-end-to-end-for-the-web-layer.md)):

- `/bills` renders; all four filter controls change the result set; pagination works.
- A real Bill profile renders header, sponsor link, policy-area block, and the five Phase-2b placeholder boxes with their expected muted styling.
- `/search?q=infrastructure` shows three sections (Members, Bills, Proceedings); each section's checkbox suppresses independently.
- `/search?q=hr+1` redirects (single match) or shows three cards (one per Congress).
- `/members/{known-sponsor}` shows Sponsored bills; Cosponsored shows the "(Phase 2b)" placeholder.

**Regression risk:**

- All Phase 1 tests must continue to pass — especially `tests/test_web_members.py` (Sponsored section now real, Cosponsored placeholder relabeled) and `tests/test_web_search.py` (`/search` handler gains Bills params).

## Acceptance criteria

- [ ] `concord scrape bills --congresses 119 --bill-types hr --limit 10 --storage-dir /tmp/concord` writes exactly 10 envelopes to `data/bills.jsonl` (no other JSONL files exist).
- [ ] `concord load bills --storage-dir /tmp/concord --db /tmp/test.db` populates `bills` with the right row count.
- [ ] `concord index bills --db /tmp/test.db` populates `bills_fts` (one row per Bill).
- [ ] `concord run bills --congresses 119 --bill-types hr --limit 10` does all three in one shot.
- [ ] `concord scrape members --congresses 119` still works (Phase 1 regression).
- [ ] `pytest` passes (full suite + new tests).
- [ ] `uv run ruff check` clean.
- [ ] `GET /bills` returns 200; chamber/policy_area/congress/sponsor filters change the result set.
- [ ] `GET /bills/119/hr/1` returns 200, shows sponsor data, renders five visible "(Phase 2b)" placeholder boxes.
- [ ] `GET /bills/119/hr/999999` returns 404; `GET /bills/119/xyz/1` returns 400 or 404.
- [ ] `GET /search?q=infrastructure` returns three sections; unchecking `bills=` suppresses the Bills section.
- [ ] `GET /search?q=hr+1` returns a 307 redirect (single-match) or renders multiple Bill cards (multi-match).
- [ ] `GET /members/{sponsor_bioguide_id}` shows a live "Sponsored bills" section; the "Cosponsored bills (Phase 2b)" placeholder is visible.
- [ ] [CONTEXT.md](../../CONTEXT.md) has the Bill / Bill type / Sponsor / Policy area entries (the Cosponsor / Bill action entries are also there but only exercised by 2b).
- [ ] [docs/adr/0009-multi-endpoint-entities-split-jsonl.md](../adr/0009-multi-endpoint-entities-split-jsonl.md) exists.
- [ ] `src/concord/scraper/bills.py` (exporting `scrape_basic`), `src/concord/pipeline/{load,index}_bills.py` exist; no Phase 1 modules were moved or deleted.

## Open questions

None — all design decisions resolved during grilling and the post-grilling tier-split refinement. See [phase-2b-bills-enrichment.md](./phase-2b-bills-enrichment.md) for the Phase 2b scope.

## Out-of-band work

- **Tier 1 full 117–119 backfill.** `concord scrape bills --congresses 117,118,119` (no `--limit`) is a ~12-hour command-line job. Run on the VPS once 2a's surface review is done.
- **Phase 2b (enrichment).** The sibling plan. Starts from 2a's `bills.jsonl` + populated `bills` table and adds the five sub-endpoint scrapers, child tables, enrichment-aware UI sections, and the Cosponsored cross-link.
- **`/bills?policy_area=…` UI dropdown.** Phase 2a supports the URL param; the dropdown enumerating the ~30 CRS policy areas is a small follow-up: `SELECT DISTINCT policy_area FROM bills`.
- **Phase 5 chunks linkage rehearsal.** Phase 5 will add chunks rows with `source_type='bill'`, `source_id=bills.bill_id`. Phase 2a's PK choice matches [ADR 0008](../adr/0008-chunks-generalize-beyond-proceedings.md); a Phase 5 PR diff will confirm by mechanical join.
