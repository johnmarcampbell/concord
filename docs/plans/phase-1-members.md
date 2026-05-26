# Phase 1 — Members ingest

> Ingest U.S. Congress Member records from api.congress.gov into Concord, surface them on the public web app as browseable profiles, and federate Members into the existing `/search` route alongside Proceedings.

## Source

- Roadmap: [docs/plans/members-bills-votes-roadmap.md](./members-bills-votes-roadmap.md) (Phase 1 section)
- Scoping conversation: [docs/plans/members-bills-votes-scope.md](./members-bills-votes-scope.md)

## Context

Concord today ingests one entity type — **Proceedings** (articles from the Congressional Record) — and surfaces them via `/search` and `/proceedings/{granule_id}` on a FastAPI+HTMX site ([CONTEXT.md](../../CONTEXT.md), [ADR 0001](../adr/0001-python-end-to-end-for-the-web-layer.md)). Phase 1 is the first step in expanding to the broader Congress domain: **Member** records (people who have served in Congress) become Concord's first peer entity to Proceedings.

Members are chosen first because they are (a) the smallest entity in the planned set, (b) almost entirely stable (Bioguide IDs are immutable; term history changes rarely), and (c) the prerequisite for every later phase — Bills cite sponsors, Votes cite per-member positions, Stage 3 Enrichment mentions members in Proceeding text. Shipping Members early de-risks the architectural shift to multi-entity ingest before the harder mutable entities (Bills, Votes) arrive.

Domain terms (Member, Bioguide ID, Term, Chamber, Party) are defined in the "Entities" section of [CONTEXT.md](../../CONTEXT.md), added as part of this plan's grilling pass.

## Goals

1. The scraper can fetch all Members for the last three congresses (117th, 118th, 119th) from `api.congress.gov/v3/member` and persist them to `data/members.jsonl` using the snapshot-on-fetch envelope from [ADR 0006](../adr/0006-snapshot-on-fetch-for-mutable-entities.md).
2. The loader projects the latest snapshot per Bioguide ID into a `members` table and per `(bioguide_id, congress, chamber)` into a `member_terms` table in `data/proceedings.db`.
3. The indexer populates a `members_fts` FTS5 virtual table over Member names so the search box can resolve names.
4. The web app serves `/members` (browseable list with filters) and `/members/{bioguide_id}` (profile page).
5. The existing `/search` route is federated: it queries both the existing chunks pipeline (Proceedings) and `members_fts` (Members) and renders results in two grouped sections with inline checkbox filters.
6. The CLI is renamed for entity-explicit verbs per [ADR 0007](../adr/0007-parallel-pipelines-per-entity.md): `concord pull` → `concord scrape proceedings`, `load` → `load proceedings`, `index` → `index proceedings`, `run` → `run proceedings`. New commands: `concord scrape members`, `load members`, `index members`, `run members`.
7. The code layout matches ADR 0007 literally: `src/concord/scraper/{proceedings,members}.py` and `src/concord/pipeline/{load,index}_{proceedings,members}.py` subpackages.
8. All existing tests continue to pass after the CLI rename and subpackage refactor.

## Non-goals

1. **Bill text RAG, Votes, Committees, Amendments** — those are Phases 2–5. The `/members/{bioguide_id}` profile page renders placeholder sections for them, but the data isn't ingested.
2. **Mentions of Members in Proceeding text.** That's Phase 6 (Stage 3 Enrich). The profile page has a placeholder section for it.
3. **Backfill to 1995.** Phase 1 ingests the last three congresses only (roadmap scope decision).
4. **Embeddings on Member names.** Name search uses FTS5 only ([Q3 grilling outcome](#approach)); semantic similarity over names isn't useful.
5. **Curated nickname/initialism aliases** (no "AOC" → Ocasio-Cortez mapping at v1). The API's name fields only. Curated aliases are a tiny follow-up after real traffic surfaces the demand.
6. **Per-state or per-district detail pages** (`/states/VT`, `/districts/CA-12`). `/members?state=VT` covers the use case via the index page's filter.
7. **Auto-release / cron scheduling for daily refresh.** The plan lands the commands; wiring them into a scheduler is a separate task.
8. **A `members_raw` SQLite table** holding raw API payloads. The JSONL is the recovery substrate per [ADR 0006](../adr/0006-snapshot-on-fetch-for-mutable-entities.md); duplicating snapshots into SQL adds maintenance without unlocking anything `jq` can't do.

## Relevant prior decisions

- [ADR 0001 — Python end-to-end for the web layer](../adr/0001-python-end-to-end-for-the-web-layer.md) — defines the FastAPI+Jinja2+HTMX surface that `/members` and `/members/{bioguide_id}` extend.
- [ADR 0002 — JSONL as canonical raw store](../adr/0002-jsonl-as-canonical-raw-store.md) — Members go to `data/members.jsonl`.
- [ADR 0003 — SQLite as derived store](../adr/0003-sqlite-as-derived-store.md) — Members tables live in the existing `proceedings.db`.
- [ADR 0005 — Chunks as the unit of retrieval](../adr/0005-chunks-as-unit-of-retrieval.md) — does **not** apply to Members; member name search uses its own FTS5 table, not chunks.
- [ADR 0006 — Snapshot-on-fetch for mutable entities](../adr/0006-snapshot-on-fetch-for-mutable-entities.md) (new, created with this plan's parent roadmap) — defines the JSONL envelope shape for Members.
- [ADR 0007 — Parallel Stage 0 + Stage 1 per entity type](../adr/0007-parallel-pipelines-per-entity.md) (new) — defines the per-entity subpackage layout and CLI verb shape.
- [ADR 0008 — Chunks generalize beyond Proceedings](../adr/0008-chunks-generalize-beyond-proceedings.md) (new) — **not exercised by Phase 1** (Members aren't chunked); referenced for completeness because Phase 5 will require the chunks-table migration this ADR specifies.

## Relevant files and code

Files to **read** for context:

- [CONTEXT.md](../../CONTEXT.md) — domain glossary, including the new "Entities" section.
- [src/concord/cli.py](../../src/concord/cli.py) — current typer CLI with `pull`/`load`/`index`/`run`/`serve` commands at lines 287, 371, 410, 440, 523.
- [src/concord/api.py](../../src/concord/api.py) — congress.gov HTTP client. The pagination + auth + retry patterns to mirror for the new `/member` calls.
- [src/concord/models.py](../../src/concord/models.py) — pydantic patterns to follow for the new `Member` and `Term` models.
- [src/concord/pipeline.py](../../src/concord/pipeline.py) — Stage 1 loader to be moved into `src/concord/pipeline/load_proceedings.py`.
- [src/concord/indexing.py](../../src/concord/indexing.py) — Stage 2 indexer to be moved into `src/concord/pipeline/index_proceedings.py`.
- [src/concord/storage/sqlite.py](../../src/concord/storage/sqlite.py) — existing schema, lines 60–134. New `members`/`member_terms`/`members_fts` schema goes alongside.
- [src/concord/web/app.py](../../src/concord/web/app.py) — FastAPI app; existing routes at lines 125, 131, 189, 207. New `/members*` routes added here.
- [src/concord/web/search.py](../../src/concord/web/search.py) — existing search query layer; will be extended for federated Member+Proceeding search.
- [tests/conftest.py](../../tests/conftest.py) — fixture helpers (`load_fixture`, `load_json_fixture`).
- [tests/test_storage_sqlite.py](../../tests/test_storage_sqlite.py) and [tests/test_pipeline.py](../../tests/test_pipeline.py) — test patterns to mirror.

Files to **create**:

- `src/concord/scraper/__init__.py`
- `src/concord/scraper/proceedings.py` (extracted from `cli.py` + `api.py`)
- `src/concord/scraper/members.py`
- `src/concord/pipeline/__init__.py`
- `src/concord/pipeline/load_proceedings.py` (moved from `pipeline.py`)
- `src/concord/pipeline/load_members.py`
- `src/concord/pipeline/index_proceedings.py` (moved from `indexing.py`)
- `src/concord/pipeline/index_members.py`
- `src/concord/web/templates/members/list.html`
- `src/concord/web/templates/members/profile.html`
- `tests/_snapshots.py` — shared helper for the ADR 0006 envelope shape.
- `tests/fixtures/api/members/current_house.json`
- `tests/fixtures/api/members/current_senate.json`
- `tests/fixtures/api/members/historical.json`
- `tests/test_models_members.py`
- `tests/test_storage_members_sqlite.py`
- `tests/test_scraper_members.py`
- `tests/test_pipeline_members.py`
- `tests/test_web_members.py`

Files to **delete** (after moves):

- `src/concord/pipeline.py` (contents moved to `src/concord/pipeline/load_proceedings.py` and any orchestration into `src/concord/pipeline/__init__.py`)
- `src/concord/indexing.py` (moved to `src/concord/pipeline/index_proceedings.py`)

## Approach

### Domain model

`Member` is per-person (fields stable across a career); `Term` is per-service-period. Splitting them avoids lying about historical state: a Member who was a Republican in one Congress and an Independent in the next has two Term rows with different `party` values. The same applies to Chamber — a Representative who later becomes a Senator has two Terms in different chambers, not a contradictory single Member record. (See [CONTEXT.md](../../CONTEXT.md) "Entities" section.)

`is_current` is computed, not stored: `end_date IS NULL OR end_date >= date('now')`. No nightly maintenance job to flip a boolean.

### Storage shape

`data/members.jsonl` follows [ADR 0006](../adr/0006-snapshot-on-fetch-for-mutable-entities.md):

```json
{"fetched_at": "2026-05-25T14:02:11Z", "key": {"bioguide_id": "O000172"}, "payload": {...raw API body...}}
```

SQLite schema (new, added to `src/concord/storage/sqlite.py`):

```sql
CREATE TABLE IF NOT EXISTS members (
  bioguide_id  TEXT PRIMARY KEY,
  first_name   TEXT NOT NULL,
  middle_name  TEXT,
  last_name    TEXT NOT NULL,
  suffix       TEXT,
  birth_year   INTEGER,
  death_year   INTEGER,
  display_name TEXT NOT NULL,   -- API's directOrderName
  photo_url    TEXT,             -- API's depiction.imageUrl
  biography    TEXT,             -- free-text biography
  fetched_at   TEXT NOT NULL     -- ISO-8601 UTC, latest snapshot's timestamp
);

CREATE TABLE IF NOT EXISTS member_terms (
  bioguide_id TEXT NOT NULL REFERENCES members(bioguide_id),
  congress    INTEGER NOT NULL,
  chamber     TEXT NOT NULL CHECK (chamber IN ('house', 'senate')),
  party       TEXT,
  state       TEXT NOT NULL,
  district    INTEGER,           -- NULL for senators
  start_date  TEXT,
  end_date    TEXT,              -- NULL = currently serving
  PRIMARY KEY (bioguide_id, congress, chamber)
);

CREATE INDEX IF NOT EXISTS idx_member_terms_congress ON member_terms(congress);
CREATE INDEX IF NOT EXISTS idx_member_terms_state ON member_terms(state);

CREATE VIRTUAL TABLE IF NOT EXISTS members_fts USING fts5(
  bioguide_id UNINDEXED,
  direct_order_name,
  inverted_order_name,
  last_name,
  tokenize = 'porter'
);
```

Name search uses FTS5 only — no embeddings. Embeddings over short proper nouns don't add over BM25 + porter stemming.

### Federated search

The existing `/search` route runs RRF over `chunks_fts` + `chunks_vec` for Proceedings. Phase 1 extends it to also query `members_fts` independently. Results render in two stacked sections: **Members (N)** with profile cards (photo, display name, current role), then **Proceedings (N)** with the existing snippet list. Score normalization across the two indexes is **not** attempted — each section uses its own ranking, which is honest about the data shapes and defers the harder unified-ranking problem to Phase 7.

Filters above the results: `☑ Members  ☑ Proceedings` checkboxes. Unchecking suppresses that section and skips its query. Inline (above the results), not in a sidebar.

`/members` itself has no search box — users searching by name use the global `/search`. The `/members` index is browse-only: paginated currently-serving list with **Chamber** (House/Senate/both) and **Party** (D/R/I/other) filter checkboxes, sorted `(state ASC, last_name ASC)`. Default page size: 50.

### Disambiguation

When `members_fts` returns multiple hits for an ambiguous query (`"Sanders"` matches several Members), results are ordered `(is_current DESC, last_active_congress DESC)` so the recognizable contemporary Sanders surfaces first. `last_active_congress` is computed as `MAX(congress) FROM member_terms WHERE bioguide_id = ?`.

### CLI rename

The rename pattern from [ADR 0007](../adr/0007-parallel-pipelines-per-entity.md) is `<stage> <entity>`. Applies to stage commands only:

| Old | New |
|---|---|
| `concord pull` | `concord scrape proceedings` |
| `concord load` | `concord load proceedings` |
| `concord index` | `concord index proceedings` |
| `concord run` | `concord run proceedings` |
| `concord serve` | unchanged (not stage-scoped) |

Plus new commands: `concord scrape members`, `concord load members`, `concord index members`, `concord run members`.

Bare verbs are deleted, not aliased — the muscle-memory cost is bounded and one-time.

### Subpackage refactor

Per the Q7 grilling decision (Option A): honor [ADR 0007](../adr/0007-parallel-pipelines-per-entity.md) literally. `src/concord/scraper/` and `src/concord/pipeline/` become subpackages. Existing Proceedings logic moves into them. This is real but bounded work — the alternative (flat modules with prefix naming) would leave the codebase shape out of step with the ADR from day one.

### Snapshot envelope helper

`tests/_snapshots.py` exports a small helper used by every entity test module from Phase 1 onward:

```python
def wrap_snapshot(payload: dict, *, fetched_at: datetime, key: dict) -> dict:
    return {"fetched_at": fetched_at.isoformat(), "key": key, "payload": payload}
```

Lands now (Phase 1 needs it) and is reused unchanged by Phase 2's Bills tests, Phase 3's Votes tests, etc.

## Step-by-step plan

The plan is grouped into five sections — **Refactor**, **Models & storage**, **Ingest**, **Web surface**, **Verification**. Steps within each section are ordered; sections are loosely orderable but the refactor must land before the new entity work to avoid moving moving-targets.

### Section 1 — Subpackage refactor (do this first)

1. **Create `src/concord/scraper/` subpackage.** Add empty `src/concord/scraper/__init__.py`. Create `src/concord/scraper/proceedings.py` and move the scrape-orchestration helper `_run_pull` from `src/concord/cli.py:113` into it as a public `scrape(...)` function. The pure HTTP client code stays in `src/concord/api.py` (it's a transport, not a scraper). Verify: `python -c "from concord.scraper.proceedings import scrape"` succeeds.

2. **Create `src/concord/pipeline/` subpackage.** Add `src/concord/pipeline/__init__.py`. Move `src/concord/pipeline.py` → `src/concord/pipeline/load_proceedings.py`. Move `src/concord/indexing.py` → `src/concord/pipeline/index_proceedings.py`. Delete the old flat files. Update every import site (tests, cli) to the new paths. Verify: `pytest tests/test_pipeline.py tests/test_indexing.py` passes unchanged.

3. **Rename existing CLI commands.** In `src/concord/cli.py`: rename `@app.command("pull")` → `@app.command("scrape proceedings")` (using typer subcommand groups; see typer docs for the `add_typer` pattern). Same for `load` → `load proceedings`, `index` → `index proceedings`, `run` → `run proceedings`. `serve` is unchanged. Update help strings and docstrings throughout `cli.py` to reflect new names. Verify: `concord scrape proceedings --help` works; bare `concord pull` errors with typer's "no such command".

4. **Update existing tests for the CLI rename.** Edit `tests/test_cli.py` to invoke `scrape proceedings`, `load proceedings`, etc. Verify: `pytest tests/test_cli.py` passes.

### Section 2 — Models & storage

5. **Add `Member` and `Term` pydantic models.** Extend `src/concord/models.py` with `Member`, `Term`, and `MemberSnapshot` (envelope wrapper per ADR 0006). Match the schema in [Approach > Storage shape](#approach), with API-field aliases (`directOrderName` → `display_name`, `depiction.imageUrl` → `photo_url`, etc.) mirroring the `Issue`/`Article` aliasing pattern at `src/concord/models.py:42-72`. Keep these in the same file for now — if `models.py` later exceeds ~400 lines, promote to a `src/concord/models/` subpackage in a separate refactor. Verify: `pytest tests/test_models_members.py` (created next step) passes.

6. **Write unit tests for the models.** Create `tests/test_models_members.py`. Cover: parsing a real API payload (use `current_house.json` fixture from step 13), the snapshot envelope wrapping, and serialization round-trip. Mirror the structure of `tests/test_models.py`.

7. **Add `members`, `member_terms`, `members_fts` schema.** Edit `src/concord/storage/sqlite.py` to append the three table definitions from [Approach > Storage shape](#approach) to the existing `_SCHEMA` constant (currently around line 60). The existing init function will apply them on next open. Verify: open a fresh DB, run `.schema members*` in `sqlite3`, see all three tables.

8. **Write storage tests for the new tables.** Create `tests/test_storage_members_sqlite.py` mirroring `tests/test_storage_sqlite.py`. Cover: insert + read-back of a Member with multiple Terms, conflict-on-duplicate-key behavior (snapshot-on-fetch means re-loading the same member must replace, not duplicate — UPSERT on Bioguide ID).

### Section 3 — Ingest

9. **Build the `/member` API client method.** Extend `src/concord/api.py`'s `Client` class with a `list_members(congress: int) -> Iterator[dict]` method that paginates `GET /v3/member/congress/{congress}` using the same auth + retry + rate-limit logic as the existing `list_issues` method. Return raw dicts; parsing into `Member` happens at the next layer. Verify: `tests/test_api.py` gains a test using a captured `/member` fixture and `httpx.MockTransport`.

10. **Build the Members scraper.** Create `src/concord/scraper/members.py` with a `scrape(client, congresses: list[int], storage_path: Path, *, fetched_at: datetime) -> int` function. For each congress, page through `client.list_members(congress)`, wrap each member dict in a snapshot envelope (`{fetched_at, key, payload}`), and append to `data/members.jsonl`. Return the number of snapshots written. Note: a member who served in multiple congresses will get one snapshot per congress that lists them — Stage 1 deduplicates by Bioguide ID.

11. **Build the Members loader.** Create `src/concord/pipeline/load_members.py` with a `load(jsonl_path: Path, db_path: Path) -> LoadStats` function. Read snapshots, group by `bioguide_id`, keep latest by `fetched_at`. For each kept snapshot: UPSERT a row into `members`, then DELETE-then-INSERT `member_terms` rows for that bioguide_id (avoids stale terms if the API ever drops one). Verify with `tests/test_pipeline_members.py` (next step).

12. **Build the Members FTS5 indexer.** Create `src/concord/pipeline/index_members.py` with `index(db_path: Path) -> IndexStats`. Truncate `members_fts`, repopulate by selecting `(bioguide_id, display_name AS direct_order_name, ..., last_name)` from `members`. Idempotent — re-running gives the same final state.

13. **Capture API fixtures.** Run a real `/member` call (with `CONGRESS_API_KEY` set) for three members and save the raw JSON to `tests/fixtures/api/members/`:
    - `current_house.json` — a currently-serving Representative with at least 2 Terms.
    - `current_senate.json` — a currently-serving Senator.
    - `historical.json` — a former member with `end_date` populated and ideally a party change between terms.

14. **Write scraper + loader integration tests.** Create `tests/test_scraper_members.py` (uses `httpx.MockTransport` with the captured fixtures to drive `scrape()` end-to-end) and `tests/test_pipeline_members.py` (loads the resulting JSONL into an in-memory SQLite and asserts the table contents).

15. **Add `tests/_snapshots.py` helper.** Export `wrap_snapshot(payload, *, fetched_at, key)` as in [Approach](#approach). Update the tests written in steps 6, 8, 14 to use it instead of inlining envelope dicts.

### Section 4 — CLI commands for Members

16. **Add `concord scrape members` command.** In `src/concord/cli.py`, register a new typer command at `scrape members` that takes `--congresses` (default: `117,118,119`), `--storage` (default: `data/members.jsonl`), and calls `concord.scraper.members.scrape(...)`. Mirror the option shape of `scrape proceedings`. Verify: `concord scrape members --help` shows the help text; smoke test in `tests/test_cli.py`.

17. **Add `concord load members` command.** Wires to `concord.pipeline.load_members.load(...)`. Same `--storage` / `--db` option pattern as `load proceedings`.

18. **Add `concord index members` command.** Wires to `concord.pipeline.index_members.index(...)`.

19. **Add `concord run members` command.** Mirrors `concord run proceedings` (step in step 3): chains `scrape members` → `load members` → `index members`. Useful for the cron path later.

### Section 5 — Web surface

20. **Add `/members` route.** In `src/concord/web/app.py`, add a `GET /members` handler that:
    - Reads query params `chamber` (default both), `party` (default all), `page` (default 1).
    - Queries `members` JOINed against `member_terms` for current terms only (`end_date IS NULL OR end_date >= date('now')`).
    - Applies chamber + party filters.
    - Orders by `(state ASC, last_name ASC)`, paginates 50/page.
    - Renders `src/concord/web/templates/members/list.html`.

21. **Add `/members/{bioguide_id}` route.** Handler queries one Member + all its Terms, renders `src/concord/web/templates/members/profile.html`. Sections: header (photo, name, current-role line, `fetched_at` tag), Biography (collapsed if >300 chars — pure HTMX, no JS framework), Term history table, future-phase placeholder sections labeled "Sponsored bills (Phase 2)", "Recent votes (Phase 3)", "Mentioned in proceedings (Phase 6)". Placeholders render as muted empty-state boxes — no dead links.

22. **Add Member-aware search to `src/concord/web/search.py`.** Add a `search_members(db, q: str, limit: int = 10) -> list[MemberHit]` function: runs `SELECT ... FROM members_fts WHERE members_fts MATCH ? ORDER BY bm25(members_fts)`, joins to `members` for display fields, joins to `member_terms` for `MAX(congress) AS last_active_congress`, applies the disambiguation sort `(is_current DESC, last_active_congress DESC)`. Returns hits as a structured type.

23. **Federate `/search`.** Update the `GET /search` handler in `src/concord/web/app.py` to:
    - Read new query params `members` and `proceedings` (both default `on`).
    - Run `search_members` and the existing proceedings search in parallel (or sequentially — performance isn't load-bearing at v1 scale).
    - Render results in two grouped sections in the template.
    - Render inline filter checkboxes above the results.

24. **Update the global nav.** Add a `/members` link to whatever shared nav fragment the existing templates use (likely in a `base.html` or similar). Verify by clicking through from `/` to `/members` and back.

### Section 6 — Verification

25. **End-to-end smoke test.** Create or extend `tests/test_smoke.py` (existing file) with a scenario that: writes 3 snapshots to a temp JSONL, runs `load_members.load()` + `index_members.index()`, opens the web app via `httpx.ASGITransport`, hits `/members`, `/members/{id}`, and `/search?q=<member name>`, asserts a 200 and presence of expected text on each.

26. **Update ADR 0007 with a clarifying note (optional).** Decided during the Q7 grilling that the literal subpackage paths in the ADR are authoritative. If during execution the subpackage refactor (Section 1) reveals any deviation, append a one-paragraph note to [ADR 0007](../adr/0007-parallel-pipelines-per-entity.md) describing the reality. If the refactor lands cleanly with no deviation, this step is a no-op.

27. **Run the full test suite + a manual smoke against live data.** `pytest` clean. Then with real keys set: `concord run proceedings --from <recent-date>` (regression check for the rename), then `concord run members --congresses 119`, then `concord serve` and click through `/members` + `/search?q=<known senator surname>`.

## Demo seed data

This project does not have a `backend/demo/seed.sql` (the template's example) — it's a Python project that derives its demo state from `data/proceedings.jsonl` and `data/proceedings.db`. The Phase 1 equivalent: after step 27 lands, the user runs `concord run members --congresses 119` once locally to populate `data/members.jsonl` and the new SQLite tables. No checked-in seed file to update.

If a future phase introduces a `data/demo/*.jsonl` fixture pattern for fresh-clone demo state, this plan's outputs should be included in it. Out of scope for Phase 1.

## Testing strategy

**Unit tests:**

- `tests/test_models_members.py` — parsing real API payloads into `Member`/`Term`, snapshot-envelope round-trip.
- `tests/test_storage_members_sqlite.py` — UPSERT behavior for Members, DELETE-then-INSERT for Terms, FTS5 population.
- `tests/test_scraper_members.py` — `httpx.MockTransport` driving the scraper end-to-end against fixtures.
- `tests/test_pipeline_members.py` — JSONL → SQLite projection, including latest-snapshot-wins semantics.
- `tests/test_web_members.py` — `/members` (with each filter combo), `/members/{id}` (existing + 404), `/search?q=…` with the new federated layout.

**Integration test:**

- Extend `tests/test_smoke.py` per step 25.

**Manual checks (frontend has no component tests per [ADR 0001](../adr/0001-python-end-to-end-for-the-web-layer.md)):**

- `/members` renders, pagination works, both filter checkboxes work in combination.
- `/members/{bioguide_id}` for a known House member, a known Senator, and a known historical member. Photo loads, biography collapse works, term history table is in chronological order.
- `/search?q=Sanders` shows both a Members section (multiple Sanders) and a Proceedings section.
- `/search?q=Sanders&members=on&proceedings=` shows only Members.
- `/search?q=infrastructure spending&members=&proceedings=on` shows only Proceedings (regression check for the existing flow).

**Regression risk:**

- All of `tests/test_pipeline.py`, `tests/test_indexing.py`, `tests/test_cli.py`, `tests/test_web_routes.py`, `tests/test_web_search.py` must continue to pass after the Section 1 refactor. These tests reference old module paths (`concord.pipeline`, `concord.indexing`) and old CLI command names (`pull`, `load`, `index`, `run`); updating imports and command invocations is part of Section 1.

## Acceptance criteria

- [ ] `concord scrape members --congresses 119 --storage /tmp/members.jsonl` writes a non-empty JSONL with the ADR 0006 envelope shape.
- [ ] `concord load members --storage /tmp/members.jsonl --db /tmp/test.db` populates `members` and `member_terms` tables with the right row counts.
- [ ] `concord index members --db /tmp/test.db` populates `members_fts` with one row per Member.
- [ ] `concord run members --congresses 119` does all three in one shot.
- [ ] `concord scrape proceedings --from 2026-05-20` works (regression check for the rename).
- [ ] `concord pull` (the old bare verb) errors with typer's "no such command".
- [ ] `pytest` passes (full suite, ~all existing tests + new ones from this plan).
- [ ] `GET /members` returns 200, renders a list, filters change the result set.
- [ ] `GET /members/{bioguide_id}` returns 200 for a real Bioguide ID and 404 for an unknown one.
- [ ] `GET /search?q=Sanders` shows two sections (Members + Proceedings); unchecking either suppresses it.
- [ ] [CONTEXT.md](../../CONTEXT.md) has an "Entities" section with Member, Bioguide ID, Term, Chamber, Party defined (this plan's grilling already added it — confirm it didn't get reverted).
- [ ] `tests/_snapshots.py` exists and is used by Members tests.
- [ ] `src/concord/scraper/` and `src/concord/pipeline/` subpackages exist; `src/concord/pipeline.py` and `src/concord/indexing.py` flat files no longer exist.

## Open questions

None — all design decisions resolved during the grilling pass. The seven topics covered: (1) Member vocabulary, (2) schema split and field set, (3) name-alias scope, (4) CLI rename strategy and verb alignment, (5) web surface + federated `/search`, (6) test patterns + helper + fixture count, (7) subpackage vs flat module layout.

## Out-of-band work

- **Cron / scheduler wiring for daily refresh of `data/members.jsonl`.** Not in this plan. The roadmap's risk section calls out staleness; whoever wires Phase 2 (Bills) should consider building a shared scheduling layer that both Proceedings and Members use, rather than per-entity cron scripts.
- **Curated alias CSV (nicknames, initialisms).** Deferred per Q3. If after a few weeks of real traffic the search logs show repeated misses for things like "AOC" or "Bernie", a small `data/member_aliases.csv` checked into the repo + a tweak to `index_members.py` to append those rows to `members_fts` is a ~1-hour follow-up.
- **Phase 2 (Bills) implementation plan.** Should reuse the patterns established here: `src/concord/scraper/bills.py`, `src/concord/pipeline/{load,index}_bills.py`, `tests/_snapshots.py` envelope helper, `concord {scrape,load,index,run} bills` commands.
