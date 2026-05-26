# Phase 2b — Bills (enrichment) ingest

> Enrich the Bills loaded by Phase 2a with their five sub-endpoint resources — cosponsors, action history, CRS subjects, titles, summaries — via an ad-hoc per-bill scraper. Surface the new data on the Bill profile page (with explicit "fetched X ago" / "not yet fetched" labels per section) and on the Member profile's Cosponsored bills cross-link.

## Source

- Roadmap: [docs/plans/members-bills-votes-roadmap.md](./members-bills-votes-roadmap.md) (Phase 2 section)
- **Prerequisite plan:** [docs/plans/phase-2a-bills-basic.md](./phase-2a-bills-basic.md) — Phase 2b assumes 2a's `bills.jsonl`, `bills` table, `bills_fts` index, `/bills` index, and basic Bill profile are in place and stable.
- Phase 1 exemplar (structural template): [docs/plans/phase-1-members.md](./phase-1-members.md)

## Context

Phase 2a delivered Bill identity ingest. Every Bill in 117–119 has a row in `bills` with sponsor, policy area, latest action, and introduced date; users can browse `/bills`, find Bills via federated `/search`, and click from a Member page to bills they sponsored. What Phase 2a deliberately deferred: cosponsors (M:N edge that drives the political-graph use case), full action history (the bill's legislative trajectory), CRS-assigned legislative subjects (the multi-valued sibling of `policy_area`), all title variants (short titles, popular names), and CRS-written summaries (the only narrative-form description of what a bill does).

Phase 2b adds all five, fetched per-bill from five separate sub-endpoints. The scrape pattern is deliberately ad-hoc rather than bulk: full enrichment of 117–119 is ~700K API calls (~140 hours) and ~30 GB of JSONL (summaries dominate — ~600KB per bill of HTML). Most Bills don't justify the cost; a sensible operator strategy is to enrich enacted bills first (`WHERE bills.laws IS NOT NULL`), then visible-on-the-surface bills, then on-demand thereafter. The pipeline is built to tolerate this — every Bill profile page works with or without enrichment, and the UI is explicit about which sections have been fetched vs not.

## Goals

1. The Tier 2 scraper exports `scrape_enrichment(client, bill_keys, storage_dir, *, fetched_at, sections=None, limit=None, progress=None) -> ScrapeStats`. For each bill × section, it fetches the corresponding sub-endpoint (handling pagination by concatenation) and writes one ADR 0006 snapshot envelope per (bill, section) to `data/bill_<section>.jsonl`.
2. The CLI gains `concord scrape bills enrich` with `--bill-ids` (explicit selection), `--sections` (subset of five, default all), `--limit N`, and `--db` (when omitted, requires `--bill-ids`; when provided, auto-selects un-enriched bills via `cosponsors_fetched_at IS NULL ORDER BY introduced_date DESC LIMIT N`).
3. The schema gains five child tables (`bill_cosponsors`, `bill_actions`, `bill_subjects`, `bill_titles`, `bill_summaries`) and five `*_fetched_at` columns on `bills`. The `bills_fts` virtual table gains `short_title` and `subjects` columns.
4. The loader is extended to read the five new JSONL files when present, project to child tables (DELETE-then-INSERT per `bill_id`), and stamp the corresponding `*_fetched_at` columns. **Tolerance:** a Bill with only tier-1 data loads with all five `*_fetched_at = NULL` and empty child tables — no errors, no missing rows in `bills`. Re-running the loader after enrichment converges the SQL state.
5. The Bill profile page renders each tier-2 section with an explicit state — full data when `*_fetched_at` is non-NULL, "Cosponsors not yet fetched — run `concord scrape bills enrich {bill_id}` to populate" when NULL. The header "Updated:" line reflects `MAX(fetched_at, *_fetched_at)`.
6. The Member profile's **Cosponsored bills** placeholder (left in place by 2a) becomes a live list, with the same empty-state honesty as the Bill profile when no enrichment has run.
7. The federated `/search` Bills section ranking uses `bills_fts.short_title` and `bills_fts.subjects` when available; bills with no enrichment fall back to title + identifier + policy_area (the 2a columns).
8. All existing tests — Phase 1, Phase 2a, and 2b's new ones — continue to pass.

## Non-goals

1. **Bulk enrichment of every Bill in 117–119.** That's an operator decision and a ~140-hour batch job. 2b lands the pipeline; the operator decides what to enrich and when.
2. **Background or on-visit lazy enrichment from the web layer.** Tempting (visit a Bill page → fire a background enrichment job → render with fresh data) but requires a background-task framework that doesn't exist in the codebase. The enrichment-prompt empty state explicitly invokes the CLI instead.
3. **Bill text RAG.** Still Phase 5. Phase 2b ingests the *summaries* (CRS-written narrative) but doesn't chunk them or write `chunks` rows.
4. **Per-Member roll-up stats** (e.g. "cosponsored 47 bills last quarter"). The data is now in `bill_cosponsors`; computing rollups is Phase 7 polish.
5. **Withdrawn-cosponsor reverse history.** `sponsorship_withdrawn_date` is captured, but the UI shows the current state (struck-through name). A timeline of "who joined and who left when" is a follow-up.
6. **Migration of pre-2b SQLite files.** Per [ADR 0003](../adr/0003-sqlite-as-derived-store.md), SQLite is regenerable from JSONL. The 2b upgrade path is: delete `data/proceedings.db`, re-run `concord load proceedings` + `concord load bills`. No in-place ALTER scripts to maintain.

## Relevant prior decisions

- **Phase 2a** ([docs/plans/phase-2a-bills-basic.md](./phase-2a-bills-basic.md)) — defines the `bills` table, `bill_id` PK shape, `data/bills.jsonl`, `scrape_basic`, and the placeholder convention this plan replaces.
- [ADR 0006 — Snapshot-on-fetch for mutable entities](../adr/0006-snapshot-on-fetch-for-mutable-entities.md) — every sub-endpoint fetch appends one snapshot.
- [ADR 0007 — Parallel pipelines per entity](../adr/0007-parallel-pipelines-per-entity.md) — Tier 2 stays inside `src/concord/scraper/bills.py` and `src/concord/pipeline/load_bills.py`; not new modules.
- [ADR 0008 — Chunks generalize beyond Proceedings](../adr/0008-chunks-generalize-beyond-proceedings.md) — not exercised. The `bill_summaries.summary_text` column is a *future* chunking input for Phase 5.
- [ADR 0009 — Multi-endpoint entities split their JSONL canonical store by sub-endpoint](../adr/0009-multi-endpoint-entities-split-jsonl.md) — committed in Phase 2a; Phase 2b is what *exercises* the multi-file structure (2a only wrote `bills.jsonl`; 2b adds the other five).

## Relevant files and code

Files to **read** for context (in addition to everything 2a touched):

- [docs/plans/phase-2a-bills-basic.md](./phase-2a-bills-basic.md) — full prerequisite context, including the `bills` schema 2b builds on.
- `src/concord/scraper/bills.py` (created by 2a) — extend with `scrape_enrichment` alongside `scrape_basic`.
- `src/concord/pipeline/load_bills.py` (created by 2a) — extend to also read the five tier-2 JSONL files.
- `src/concord/pipeline/index_bills.py` (created by 2a) — extend to populate `short_title` and `subjects` columns.
- `src/concord/web/templates/bills/profile.html` (created by 2a) — replace the five Phase-2b placeholder boxes with live sections.
- `src/concord/web/templates/members/profile.html` — Phase 2a left the "Cosponsored bills (Phase 2b)" placeholder for 2b to replace.

Files to **create**:

- `src/concord/web/templates/bills/_action_row.html` — partial for one row of the action history list (centralizes the dimming-CSS heuristic).
- `tests/fixtures/api/bills/cosponsors_119_hr_22.json` — bill with ≥3 cosponsors (e.g. HR 22, 110 cosponsors).
- `tests/fixtures/api/bills/cosponsors_119_hr_22_withdrawn.json` — bill with at least one `sponsorshipWithdrawnDate` row (find via `pagination.countIncludingWithdrawnCosponsors > count`).
- `tests/fixtures/api/bills/actions_119_hr_1.json` — 59-action history.
- `tests/fixtures/api/bills/subjects_119_hr_1.json` — multi-subject response (HR 1 has 240).
- `tests/fixtures/api/bills/titles_119_hr_1.json` — 11 titles spanning all major `titleType` variants.
- `tests/fixtures/api/bills/summaries_119_hr_1.json` — 5 versioned summaries.

Files to **modify**:

- `src/concord/api.py` — add five methods: `get_bill_cosponsors`, `get_bill_actions`, `get_bill_subjects`, `get_bill_titles`, `get_bill_summaries`. Each handles its endpoint's pagination idiom (cosponsors/actions paginate at 20/page; subjects paginate; titles/summaries don't).
- `src/concord/models.py` — add `Cosponsor`, `BillAction`, `BillSubject`, `BillTitle`, `BillSummary`.
- `src/concord/storage/sqlite.py` — append five child-table definitions + five `*_fetched_at` columns to `_BASE_SCHEMA`; extend `bills_fts` columns; extend `upsert_bill` signature; add per-child storage methods (`cosponsors_for_bill`, `actions_for_bill`, …).
- `src/concord/scraper/bills.py` — add `scrape_enrichment(...)`.
- `src/concord/pipeline/load_bills.py` — read five additional JSONL files; project to child tables; stamp `*_fetched_at` columns.
- `src/concord/pipeline/index_bills.py` — populate `short_title` (first `bill_titles` row matching `title_type LIKE 'Short Title%'`) and `subjects` (pipe-joined from `bill_subjects`).
- `src/concord/web/search.py` — add `cosponsored_bills_for_member(db, bioguide_id, limit=25)`; extend `get_bill` callers to also fetch child rows via the existing `*_for_bill` methods.
- `src/concord/web/app.py` — `/bills/{...}` handler passes child rows + `*_fetched_at` columns to the template.
- `src/concord/web/templates/bills/profile.html` — replace the five "(Phase 2b)" placeholder boxes with live sections that branch on NULL `*_fetched_at`.
- `src/concord/web/templates/members/profile.html` — replace the "Cosponsored bills (Phase 2b)" placeholder.
- `src/concord/cli.py` — register `@scrape_app.command("bills enrich")` (or move existing `scrape bills` into a sub-typer; see step 6 for the typer mechanic).

## Approach

### Storage shape (additions)

Five child tables, all keyed by `bill_id` with `ON DELETE CASCADE` so a Bill removal wipes its enrichment:

```sql
ALTER TABLE bills ADD COLUMN cosponsors_fetched_at TEXT;
ALTER TABLE bills ADD COLUMN actions_fetched_at    TEXT;
ALTER TABLE bills ADD COLUMN subjects_fetched_at   TEXT;
ALTER TABLE bills ADD COLUMN titles_fetched_at     TEXT;
ALTER TABLE bills ADD COLUMN summaries_fetched_at  TEXT;
-- (In practice these are added to the _BASE_SCHEMA CREATE TABLE,
--  and the operator re-derives the DB from JSONL — no ALTER runs.)

CREATE TABLE IF NOT EXISTS bill_cosponsors (
  bill_id                     TEXT NOT NULL REFERENCES bills(bill_id) ON DELETE CASCADE,
  bioguide_id                 TEXT NOT NULL,                  -- bare TEXT, no FK
  sponsorship_date            TEXT,
  sponsorship_withdrawn_date  TEXT,
  is_original_cosponsor       INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (bill_id, bioguide_id)
);
CREATE INDEX IF NOT EXISTS idx_bill_cosponsors_bioguide ON bill_cosponsors (bioguide_id);

CREATE TABLE IF NOT EXISTS bill_actions (
  bill_id        TEXT NOT NULL REFERENCES bills(bill_id) ON DELETE CASCADE,
  ord            INTEGER NOT NULL,
  action_date    TEXT NOT NULL,
  action_text    TEXT NOT NULL,
  action_code    TEXT,
  source_system  TEXT,
  PRIMARY KEY (bill_id, ord)
);
CREATE INDEX IF NOT EXISTS idx_bill_actions_date ON bill_actions (action_date DESC);

CREATE TABLE IF NOT EXISTS bill_subjects (
  bill_id  TEXT NOT NULL REFERENCES bills(bill_id) ON DELETE CASCADE,
  subject  TEXT NOT NULL,
  PRIMARY KEY (bill_id, subject)
);

CREATE TABLE IF NOT EXISTS bill_titles (
  bill_id     TEXT NOT NULL REFERENCES bills(bill_id) ON DELETE CASCADE,
  ord         INTEGER NOT NULL,
  title_type  TEXT NOT NULL,
  title_text  TEXT NOT NULL,
  chamber     TEXT,
  PRIMARY KEY (bill_id, ord)
);

CREATE TABLE IF NOT EXISTS bill_summaries (
  bill_id        TEXT NOT NULL REFERENCES bills(bill_id) ON DELETE CASCADE,
  version_code   TEXT NOT NULL,
  action_date    TEXT,
  action_desc    TEXT,
  summary_text   TEXT NOT NULL,
  PRIMARY KEY (bill_id, version_code)
);
```

The `bills_fts` virtual table is dropped and recreated with two extra columns (`short_title`, `subjects`). Since `index_bills` repopulates from scratch every run, this is an operator no-op.

### Scraper structure (extension)

`scrape_enrichment(client, bill_keys, storage_dir, *, fetched_at, sections=None, limit=None, progress=None) -> ScrapeStats`:

1. `bill_keys` is an iterable of `(congress, bill_type, bill_number)` tuples. The CLI populates this from `--bill-ids` (explicit) or from a `bills` query (auto-select).
2. `sections` defaults to `("cosponsors", "actions", "subjects", "titles", "summaries")`. The CLI's `--sections` flag scopes this.
3. For each bill × section, call the corresponding API method, concatenate paginated responses into one combined payload, and append one envelope to `data/bill_<section>.jsonl`.
4. Per [ADR 0009](../adr/0009-multi-endpoint-entities-split-jsonl.md), a partial fetch (3 of 5 sections succeeded) leaves the JSONL coherent — the loader projects whatever's there.
5. Stop after `limit` bills enriched.

### Loader (extension)

`load(storage_dir, db_path, *, limit=None)` in 2b reads up to six JSONL files instead of one. For each `bill_id`:

- The `bills` row is upserted from `data/bills.jsonl` (as in 2a).
- For each of the five tier-2 files that exists and has snapshots for this `bill_id`:
  - Pick the latest snapshot by `fetched_at`.
  - DELETE all child rows for `bill_id` in the corresponding table.
  - INSERT the fresh rows.
  - UPDATE `bills.<section>_fetched_at` to the snapshot's `fetched_at`.

If a tier-2 file has snapshots for a `bill_id` *not* in `data/bills.jsonl`, log + skip (the FK on `bill_cosponsors.bill_id REFERENCES bills(bill_id)` would block the INSERT anyway). Counted as `tier2_orphans_skipped` in `LoadStats`.

The loader's tolerance to missing tier-2 is the load-bearing property: re-running 2a's `concord load bills` on a database that's never been enriched is unchanged; running it on a database with some bills enriched and some not produces a mixed state where each bill is honestly labeled in the UI.

### Indexer (extension)

`index_bills.index(db_path, *, limit=None)` extends to populate `short_title` and `subjects`:

- `short_title` — `SELECT title_text FROM bill_titles WHERE bill_id = ? AND title_type LIKE 'Short Title%' ORDER BY ord LIMIT 1`. NULL for tier-1-only bills.
- `subjects` — `SELECT GROUP_CONCAT(subject, '|') FROM bill_subjects WHERE bill_id = ?`. Empty string for tier-1-only.

Bills are still in `bills_fts` even without enrichment — they just match on the 2a columns (title, identifier, policy_area).

### Web surface

**Bill profile, tier-2 sections.** Each of the five sections checks its `*_fetched_at` value:

- `cosponsors_fetched_at` is NULL → render: *"Cosponsors not yet fetched. Run `concord scrape bills enrich {bill_id}` to populate."* Same pattern for actions / subjects / titles / summaries.
- Non-NULL → render the section body (cosponsor table, action list, subject chips, title variants, summary blocks) with a small footer line: *"Fetched {how_long_ago}."*

The header "Updated:" line is `MAX(fetched_at, cosponsors_fetched_at, actions_fetched_at, subjects_fetched_at, titles_fetched_at, summaries_fetched_at)`.

**Cosponsor list rendering.** Sort by `(is_original_cosponsor DESC, sponsorship_date ASC)`. Withdrawn cosponsors render with `<del>` styling + a small "(withdrawn YYYY-MM-DD)" suffix. Each row links to `/members/{bioguide_id}` if the Member is indexed; bare-text Bioguide ID otherwise.

**Action list rendering.** Reverse-chronological. The `_action_row.html` partial applies CSS class `action--minor` to rows whose `action_code` matches a procedural-noise pattern (referrals, motions to recommit). The pattern is defined in the template — changing it doesn't require a re-scrape.

**Summary rendering.** Each `bill_summaries` row renders as a `<details>` block titled with `action_desc` and the `action_date`. Latest version (by `action_date`) is open; older versions collapsed. `summary_text` is API-supplied HTML; render with Jinja's `|safe` (the source is trusted CRS).

**Member profile, Cosponsored bills.** Replace the 2a placeholder with a live list: `SELECT b.* FROM bills b JOIN bill_cosponsors c USING (bill_id) WHERE c.bioguide_id = ? ORDER BY b.introduced_date DESC LIMIT 25`. For a Member whose Bills have all stayed tier-1-only, this list is empty — render an empty-state line: *"No cosponsored bills found yet. Try `concord scrape bills enrich --db data/proceedings.db --limit 100` to populate."*

### CLI

```
concord scrape bills enrich  [--bill-ids 119-hr-1,119-hr-22 | (auto-select via --db)]
                             [--sections cosponsors,actions,subjects,titles,summaries]
                             [--storage-dir data/]
                             [--db data/proceedings.db]
                             [--limit N]
                             [--progress/--no-progress]
```

Mechanics:

- If `--bill-ids` is provided: parse the CSV, use those bills literally.
- If `--db` is provided without `--bill-ids`: auto-select via `SELECT bill_id, congress, bill_type, bill_number FROM bills WHERE cosponsors_fetched_at IS NULL ORDER BY introduced_date DESC LIMIT N`. Choosing `cosponsors_fetched_at` as the canonical "is this bill enriched?" signal is a convention — typically all five sections fetch together.
- Neither provided → error with a clear message.
- `--sections` honors a CSV; only the listed sections are fetched.

`concord run bills` is unchanged from 2a — still tier-1-only. Tier-2 stays an explicit operator step.

### Operator workflows

Three workflows the plan supports cleanly:

1. **Quick dev exercise.** `concord scrape bills enrich --bill-ids 119-hr-1 --sections cosponsors --limit 1 --storage-dir /tmp/concord`. One bill, one section. Validates the pipeline.
2. **Targeted enrichment.** `concord scrape bills enrich --bill-ids 119-hr-1,119-hr-22,119-s-47`. Three named bills, all five sections each. ~15 calls.
3. **Stalest-first sweep.** `concord scrape bills enrich --db data/proceedings.db --limit 100`. Top 100 un-enriched bills (ordered by introduced date desc) get all five sections. ~500 calls, ~6 minutes. Repeatable as a cron-style nightly batch later.

After any enrichment scrape, the operator runs `concord load bills` to project the new snapshots into SQLite. The loader is idempotent — running it again with no new snapshots is a no-op.

## Step-by-step plan

Six sections — **API client**, **Models & schema**, **Scraper & loader**, **CLI**, **Web surface**, **Verification**.

### Section 1 — API client

1. **Add five sub-endpoint methods to `Client`.** In [src/concord/api.py](../../src/concord/api.py): `get_bill_cosponsors`, `get_bill_actions`, `get_bill_subjects`, `get_bill_titles`, `get_bill_summaries`. Each accepts `(congress, bill_type, bill_number)`; cosponsors/actions/subjects paginate at 20/page (the API default for sub-endpoints) and the method concatenates all pages into a single returned dict. Titles and summaries don't paginate. All five return the full sub-endpoint response (dict), not just the inner array — the scraper writes the dict verbatim.

2. **Test the new methods.** Extend `tests/test_api.py` with one class per method. Use captured fixtures + `httpx.MockTransport`. Verify pagination termination (a multi-page cosponsors response yields all rows concatenated) and the path canonicalization (lowercase `bill_type`).

### Section 2 — Models & schema

3. **Add five pydantic models.** Extend [src/concord/models.py](../../src/concord/models.py) with `Cosponsor`, `BillAction`, `BillSubject` (single-string wrapper), `BillTitle`, `BillSummary`. Models carry exactly the columns their target tables have.

4. **Test the models.** Extend `tests/test_models_bills.py` with parsing tests for each, using the new fixtures.

5. **Extend SqliteStorage schema and methods.** In [src/concord/storage/sqlite.py](../../src/concord/storage/sqlite.py): append the five `ALTER TABLE bills ADD COLUMN <section>_fetched_at TEXT` columns as additions to the `CREATE TABLE bills` definition in `_BASE_SCHEMA` (no ALTER runs in practice — operators re-derive). Append the five child-table CREATEs. Update the `bills_fts` definition to include `short_title` and `subjects`. Add `_COSPONSOR_COLUMNS`, `_ACTION_COLUMNS`, `_SUBJECT_COLUMNS`, `_TITLE_COLUMNS`, `_SUMMARY_COLUMNS` tuples + per-table INSERT SQL. Extend `upsert_bill(...)` signature to accept the five child collections; keep its existing transaction shape (UPSERT parent, then per-child DELETE-then-INSERT). Add reader methods: `cosponsors_for_bill`, `actions_for_bill`, `subjects_for_bill`, `titles_for_bill`, `summaries_for_bill`.

6. **Test the storage extensions.** Extend `tests/test_storage_bills_sqlite.py` with: per-child UPSERT idempotency (DELETE-then-INSERT semantics), FK ON DELETE CASCADE behavior (delete a `bills` row → child rows vanish), `bills.cosponsors_fetched_at` (and the other four) round-trip.

### Section 3 — Scraper & loader

7. **Add `scrape_enrichment` to the Bills scraper.** Same file, [src/concord/scraper/bills.py](../../src/concord/scraper/bills.py), side-by-side with `scrape_basic`. Implement per [Approach > Scraper structure](#scraper-structure-extension). Emit `ScrapeProgressEvent(bill_key, sections_written, partial_failures)` per bill. Verify in `tests/test_scraper_bills.py`: (a) `--sections cosponsors,actions` writes exactly those two files, (b) pagination concatenates correctly, (c) one section's failure for a bill doesn't prevent the other four from being written, (d) `--limit 2` honored.

8. **Extend the Bills loader.** In [src/concord/pipeline/load_bills.py](../../src/concord/pipeline/load_bills.py), add tier-2 reading. After the existing `bills.jsonl` projection, for each tier-2 section in turn: open `data/bill_<section>.jsonl` if it exists, project latest-per-key, DELETE-then-INSERT child rows for matching `bills` rows, set the corresponding `bills.<section>_fetched_at`. Tier-2 snapshots for a `bill_id` not in `bills` are counted into `LoadStats.tier2_orphans_skipped` (no FK violation surfaces — the loader filters before INSERT). Verify in `tests/test_pipeline_bills.py`: (a) tier-1-only load — five `*_fetched_at` are NULL; (b) tier-2 added — projections converge; (c) tier-2 alone (no bills.jsonl) — counted as orphans; (d) re-running is idempotent.

9. **Extend the Bills indexer.** In [src/concord/pipeline/index_bills.py](../../src/concord/pipeline/index_bills.py): after the 2a path, also populate `short_title` and `subjects` for each `bill_id`. Tier-1-only bills get NULL `short_title` and empty `subjects`. Verify: a search for a bill's short title returns the bill when it's enriched; a search for the same short title returns nothing when only tier 1 is loaded (matches expectations — the column isn't populated yet).

### Section 4 — CLI

10. **Add `concord scrape bills enrich`.** In [src/concord/cli.py](../../src/concord/cli.py): the typer command structure built in Phase 2a registers `scrape bills` as a single command. For 2b, convert it into a sub-typer (`bills_app = typer.Typer(...)`, then `scrape_app.add_typer(bills_app, name="bills")`); the existing `scrape bills` becomes `bills_app.callback(invoke_without_command=True)` and the new tier-2 verb becomes `bills_app.command("enrich")`. The `enrich` command takes `--bill-ids` (CSV; e.g. `119-hr-1,119-hr-22`), `--sections` (CSV of five), `--storage-dir`, `--db`, `--limit`, `--progress/--no-progress`. Auto-selection query per [Approach > CLI](#cli). Error clearly when neither `--bill-ids` nor `--db` is provided.

11. **Test the CLI.** Extend `tests/test_cli.py` with: help-text checks for the new command; an end-to-end test driving `scrape bills enrich --bill-ids 119-hr-1 --limit 1` against a mocked `Client` and asserting one envelope per section JSONL written.

### Section 5 — Web surface

12. **Extend the Bills profile route.** In [src/concord/web/app.py](../../src/concord/web/app.py), the `/bills/{c}/{t}/{n}` handler (created in 2a) now also calls `cosponsors_for_bill`, `actions_for_bill`, `subjects_for_bill`, `titles_for_bill`, `summaries_for_bill`, and reads the five `*_fetched_at` columns. All seven values pass into the template context.

13. **Update the Bill profile template.** In [src/concord/web/templates/bills/profile.html](../../src/concord/web/templates/bills/profile.html), replace each of the five "(Phase 2b)" placeholder boxes with a live section. Each section's Jinja branches on `<section>_fetched_at is none` to render the empty-state prompt vs the section body. Add [src/concord/web/templates/bills/_action_row.html](../../src/concord/web/templates/bills/_action_row.html) partial; use it for both the chronological action list and any future surfaces of single actions.

14. **Add `cosponsored_bills_for_member`.** In [src/concord/web/search.py](../../src/concord/web/search.py): `SELECT b.* FROM bills b JOIN bill_cosponsors c USING (bill_id) WHERE c.bioguide_id = ? ORDER BY b.introduced_date DESC LIMIT ?`. Returns a list of row dicts.

15. **Update Member profile template.** In [src/concord/web/templates/members/profile.html](../../src/concord/web/templates/members/profile.html), replace the "Cosponsored bills (Phase 2b)" placeholder with a live list. Empty result → an empty-state line referencing `concord scrape bills enrich`. The handler at `app.py:267` calls `cosponsored_bills_for_member`.

16. **Update federated `/search`.** The `search_bills` function in `src/concord/web/search.py` is unchanged in interface; it now benefits from `bills_fts.short_title` and `bills_fts.subjects` being populated for enriched bills. Verify: a query matching a bill's short title surfaces it even when the long title doesn't match.

### Section 6 — Verification

17. **End-to-end smoke test.** Extend `tests/test_smoke.py` (or `tests/test_smoke_bills.py` if separate). Scenario: write synthetic tier-1 JSONL for two bills + tier-2 snapshots (all five sections) for ONE of them. Run `load_bills.load()` + `index_bills.index()`. Open the web app via `TestClient` and assert:
   - `GET /bills/119/hr/{enriched}` → 200, contains a cosponsor's name, first action text, and at least one subject chip.
   - `GET /bills/119/hr/{tier1_only}` → 200, contains the five "not yet fetched" empty-state markers.
   - `GET /members/{cosponsor_bioguide_id}` → 200, the Cosponsored section lists the enriched bill.
   - `GET /members/{sponsor_of_tier1_only_bill}` → 200, the Cosponsored section is the empty-state.

18. **Manual smoke against live data.** With `CONGRESS_API_KEY` set, starting from a 2a-loaded database:
    - `concord scrape bills enrich --bill-ids 119-hr-1,119-hr-22 --storage-dir data/`
    - `concord load bills`
    - `concord serve`
    - Click through: `/bills/119/hr/1` (verify cosponsors empty, actions populated, summaries populated), `/bills/119/hr/22` (verify 110 cosponsors render; sort by original; if any withdrawn, verify struck-through styling), a known cosponsor's `/members/{id}` (verify Cosponsored section), `/search?q=infrastructure` (verify short-title hits surface).

19. **Run the full test suite.** `pytest` clean. `uv run ruff check`. `uv run ruff format --check`.

## Demo seed data

No checked-in seed file. After step 19 lands, the operator regenerates the local DB (`rm data/proceedings.db && concord load proceedings && concord load bills`) and runs `concord scrape bills enrich --db data/proceedings.db --limit 50` to seed a representative set.

## Testing strategy

**Unit tests** (extend the 2a files):

- `tests/test_models_bills.py` — `Cosponsor`, `BillAction`, `BillSubject`, `BillTitle`, `BillSummary` round-trips.
- `tests/test_storage_bills_sqlite.py` — child-table UPSERTs, FK ON DELETE CASCADE, `*_fetched_at` round-trip, `bills_fts` short_title/subjects population.

**Integration tests:**

- `tests/test_api.py` extended — five new methods with `httpx.MockTransport`.
- `tests/test_scraper_bills.py` extended — `scrape_enrichment` partial-section, partial-failure, `--limit` honored, `--sections` subset.
- `tests/test_pipeline_bills.py` extended — tier-2 projection, tier-tolerance (tier-1-only load), tier-2 orphan handling, idempotent re-runs across tiers.
- `tests/test_web_bills.py` extended — enriched Bill page renders sections; tier-1-only Bill page renders empty-states; Member Cosponsored section renders both states.

**Smoke test:** extended per step 17.

**Manual checks:**

- Enriched Bill page renders all five sections with their data + "Fetched X ago" footers.
- Tier-1-only Bill page renders five empty-state boxes with CLI invocation hints.
- Cosponsor list sorts by `(is_original DESC, sponsorship_date ASC)`; withdrawn cosponsors visually struck through with a date.
- Action history reverse-chronological; minor procedural actions visually dimmed.
- Summaries render as collapsible `<details>` blocks; latest open.
- A search for a bill's short title (e.g. *"One Big Beautiful Bill Act"*) returns the bill in the Bills section when the bill has been enriched.

**Regression risk:**

- All Phase 1 + Phase 2a tests must continue to pass. The 2a `tests/test_pipeline_bills.py` tests should still work unchanged — they exercise tier-1-only paths, which 2b deliberately preserves.

## Acceptance criteria

- [ ] `concord scrape bills enrich --bill-ids 119-hr-1 --storage-dir /tmp/concord` writes one envelope to each of the five `data/bill_<section>.jsonl` files.
- [ ] `concord scrape bills enrich --bill-ids 119-hr-1 --sections cosponsors,actions` writes envelopes only to `data/bill_cosponsors.jsonl` and `data/bill_actions.jsonl`.
- [ ] `concord scrape bills enrich --db /tmp/test.db --limit 5` auto-selects 5 un-enriched bills and enriches all five sections each.
- [ ] `concord scrape bills enrich` with neither `--bill-ids` nor `--db` errors with a clear message.
- [ ] `concord load bills` (after a tier-2 enrich) populates the corresponding child tables and flips the corresponding `bills.*_fetched_at` columns to non-NULL.
- [ ] `concord load bills` (with only `data/bills.jsonl`, no tier-2 files) still works — Phase 2a behavior is unchanged.
- [ ] `concord index bills` populates `bills_fts.short_title` and `bills_fts.subjects` for enriched bills; tier-1-only bills are still indexed (with NULL/empty for those columns).
- [ ] `concord scrape members --congresses 119` still works (Phase 1 regression).
- [ ] `concord scrape bills --congresses 119 --bill-types hr --limit 10` still works (Phase 2a regression).
- [ ] `pytest` passes.
- [ ] `uv run ruff check` clean.
- [ ] `GET /bills/119/hr/{enriched}` shows cosponsor list, action history, subjects, titles, summaries — each with a "Fetched X ago" footer.
- [ ] `GET /bills/119/hr/{tier1_only}` shows five "not yet fetched" empty-state boxes naming the CLI invocation.
- [ ] `GET /bills/119/hr/22` (or any bill with withdrawn cosponsors) visually strikes through the withdrawn cosponsor rows.
- [ ] `GET /members/{cosponsor_bioguide_id}` shows a live Cosponsored bills section; the same route on a Member whose bills are tier-1-only shows the empty-state line.
- [ ] `GET /search?q=<short_title_phrase>` returns the bill in the Bills section once enrichment has populated `bills_fts.short_title`.

## Open questions

None — all design decisions resolved during the Phase 2 grilling. See [phase-2a-bills-basic.md](./phase-2a-bills-basic.md) for the shared design decisions.

## Out-of-band work

- **Strategic enrichment passes.** Out of plan scope: defining and running the "which bills get enriched first" policy. A reasonable first batch is `WHERE laws IS NOT NULL` (every bill that became law) — ~500 bills × 5 sections ≈ 2,500 calls, ~30 min. Next batch: bills introduced in the current Congress.
- **Lazy on-visit enrichment.** Visiting a tier-1-only Bill page could trigger a background `scrape_enrichment` job. Out of scope: requires a background-task framework that doesn't exist yet. Worth revisiting when Phase 3 (Votes) or a daily-refresh scheduler arrives.
- **Withdrawn cosponsor timeline UI.** `sponsorship_withdrawn_date` is captured; building a "who joined and who left when" timeline is a follow-up.
- **Per-Member roll-up stats.** Cosponsored counts, party-line break frequency, etc. become computable once Phase 2b lands. Phase 7 polish.
- **Phase 5 chunk-text inputs.** `bill_summaries.summary_text` is HTML CRS-written narrative — a natural chunking input alongside bill text itself. Phase 5 should pull from both.
- **Daily refresh.** The list endpoint's `updateDate` per stub tells us *which* bills changed since last fetch. Refreshing only changed bills (rather than re-enriching everything) is a follow-up shared scheduler task — same out-of-band item Phase 1 left open.
