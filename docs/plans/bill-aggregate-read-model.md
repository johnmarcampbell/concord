# Bill aggregate — a read-side reconstitution model

> Give the read path a single deep entry point — `BillAggregate.from_sql(db, bill_id)` — that owns *all* the SQL for reading one Bill, so the two web consumers stop hand-choreographing six-to-nine queries each and the fact-pack assembly becomes pure.

## Source

- Conversation context (no durable issue): an `improve-codebase-architecture` session that identified the Bill read path as a deepening candidate and grilled it to a settled design. No external doc.
- Companion change (separate PR, already open): [#152](https://github.com/johnmarcampbell/concord/pull/152) makes "reach for a Pydantic model at every boundary" a documented principle (CLAUDE.md / CONTRIBUTING.md). This plan is a direct application of it — a `from_<source>` read-view factory. This plan does **not** depend on #152 merging, but should be consistent with it.

## Context

A **Bill** is, in code, conceptual: "the aggregate that spans all six endpoints" — identity plus the five **Bill sections** (`cosponsors`, `actions`, `subjects`, `titles`, `summaries`), per [CONTEXT.md](../../CONTEXT.md) and ADR 0009. On the **write** side that aggregate has a rejoin: the Stage-1 loader joins the six JSONL files into the SQLite tables. On the **read** side there is no rejoin — every consumer re-discovers and re-fetches the sections.

Two web consumers do this independently today:

- `bill_profile` (GET) — [routes_bills.py:76-151](../../src/concord/web/routes_bills.py): `get_bill` + **six** section readers (`cosponsors_for_bill`, `actions_for_bill`, `subjects_for_bill`, `titles_for_bill`, `summaries_for_bill`, `vote_history_for_bill`), an inline `updated_at`/`any_missing` freshness roll-up ([routes_bills.py:106-108](../../src/concord/web/routes_bills.py)), then `assemble_facts`.
- `generate_brief` (POST) — [web/brief.py:165-211](../../src/concord/web/brief.py): `get_bill` + **five** section readers (**no `titles`** — a real, already-present drift), then `assemble_facts`.

`assemble_facts` ([web/brief.py:45-70](../../src/concord/web/brief.py)) is itself leaky: it takes loose pre-fetched params, makes the caller recompute `vote_count` at the call site, internally plucks `summaries[-1]`, and fires **one more query** (`cosponsor_party_breakdown`). The genuinely pure, well-tested piece — `build_facts` ([brief.py:113-162](../../src/concord/brief.py)) — is fine; the bugs live in the fetch-and-marshal choreography duplicated across two routes. That's a leaky seam with no **locality**, and the titles drift is the bug class it invites.

The fix introduces the missing read-side rejoin as a **read-view model** (the family that already includes `BillHit`/`VoteHit`/`MemberHit` in `web/search.py`): `BillAggregate`, with a `from_sql(db, bill_id)` classmethod that owns every Bill-read query. Both routes then do `agg = BillAggregate.from_sql(db, bill_id)` and pass the one object onward.

Domain terms: [CONTEXT.md](../../CONTEXT.md) — **Bill**, **Bill section**, **Fact pack** / **Bill Brief** (ADR 0020), **read-view model** vs **wire-shape model** (ADR 0018). Architecture vocabulary (deep module, seam, locality) follows the `improve-codebase-architecture` skill.

## Goals

1. A `BillAggregate` Pydantic read-view model in `web/search.py` (beside `BillHit`/`VoteHit`) with `from_sql(db, bill_id) -> BillAggregate | None` that issues **all** Bill-read SQL: full identity (`b.*` + `sponsor_display_name`), the five Bill sections, `vote_history` (as `list[VoteHit]`), the cosponsor→latest-Term-party join, and the derived `updated_at` / `any_missing` freshness fields.
2. `cosponsor_party_breakdown` ([search.py:814](../../src/concord/web/search.py)) dissolves — its SQL folds into `from_sql`'s cosponsor query.
3. `assemble_facts` ([web/brief.py:45](../../src/concord/web/brief.py)) becomes a **pure** projection `assemble_facts(agg) -> BriefFacts` (no `db`, no extra query); `build_facts` stays pure and unchanged in the core `concord.brief` module.
4. Both `bill_profile` and `generate_brief` fetch through `from_sql`; the titles drift is gone (titles fetched for everyone); each route keeps its own 404 *response*.
5. Behaviour-neutral for the rendered pages and generated briefs; the existing web/brief test suites pass.

## Non-goals

1. **Splitting `web/search.py`** or extracting a dedicated Bill read module — that's candidate 3 (search.py is 1146 lines). `BillAggregate` lives in `web/search.py` for now, movable later.
2. **The entity-404 and pagination duplication** across routes — also candidate 3; not folded in here. Each route keeps its current, *differing* 404 handling (profile renders `404.html`; brief raises `HTTPException`).
3. **Re-modeling every section row** as its own Pydantic type. The aggregate holds the existing `dict` / `list[dict]` / `list[str]` / `list[VoteHit]` shapes; only the aggregate itself and `vote_history` are typed.
4. **Query batching / N+1 elimination.** `from_sql` may issue the same number of queries as today (one per section); concentrating them in one factory is the goal, not reducing the count.
5. **Changing `build_facts`, `BriefFacts`, or the brief cache/staleness logic** (ADR 0020). Only the *assembly path into* `build_facts` changes.

## Relevant prior decisions

- ADR 0009 — Multi-endpoint entities split JSONL ([docs/adr/0009-multi-endpoint-entities-split-jsonl.md](../adr/0009-multi-endpoint-entities-split-jsonl.md)): defines the Bill as identity + five sections. `BillAggregate` is the read-side counterpart to the loader's write-side rejoin.
- ADR 0018 — Pydantic at the load boundary ([docs/adr/0018-pydantic-at-the-load-boundary.md](../adr/0018-pydantic-at-the-load-boundary.md)): wire-shape models (`BillDetail`, `BillCosponsor`, …) mirror the **API** and are *not* the read shape. `BillAggregate` is a **read-view** model (the `BillHit`/`VoteHit` family), distinct from those wire models — do **not** add `from_sql` to `BillDetail`.
- ADR 0020 — Bill Brief LLM summary ([docs/adr/0020-bill-brief-llm-summary.md](../adr/0020-bill-brief-llm-summary.md)): the Fact pack is deterministic and neutral; `build_facts` stays the pure computation. This plan only changes how its inputs are assembled.
- "Reach for a Pydantic model at every boundary" (CLAUDE.md / CONTRIBUTING.md, PR [#152](https://github.com/johnmarcampbell/concord/pull/152)): `from_sql` is the read-view instance of the `from_<source>` factory pattern.
- **No new ADR.** This applies existing patterns (read-view model + `from_<source>` factory) rather than introducing a new one.

## Relevant files and code

- `src/concord/web/search.py` — home of the read-view models and the section readers this composes:
  - `BillHit` ([search.py:436](../../src/concord/web/search.py)), `_bill_hit_from_row` ([:451](../../src/concord/web/search.py)), `VoteHit` ([:864](../../src/concord/web/search.py)) — the model family `BillAggregate` joins.
  - `get_bill` ([:610](../../src/concord/web/search.py)) — full `b.*` + `sponsor_display_name`; the identity query `from_sql` absorbs.
  - `cosponsors_for_bill` ([:729](../../src/concord/web/search.py)), `actions_for_bill` ([:749](../../src/concord/web/search.py)), `subjects_for_bill` ([:763](../../src/concord/web/search.py)), `titles_for_bill` ([:772](../../src/concord/web/search.py)), `summaries_for_bill` ([:781](../../src/concord/web/search.py)), `vote_history_for_bill` ([:1006](../../src/concord/web/search.py)) — the section readers.
  - `_party_bucket` ([:795](../../src/concord/web/search.py)) and `cosponsor_party_breakdown` ([:814](../../src/concord/web/search.py)) — the party-split query whose SQL folds into `from_sql`.
- `src/concord/web/brief.py` — `assemble_facts` ([:45-70](../../src/concord/web/brief.py)) to simplify; `generate_brief` route ([:165-211](../../src/concord/web/brief.py)) to update.
- `src/concord/web/routes_bills.py` — `bill_profile` route ([:76-151](../../src/concord/web/routes_bills.py)) to update; the inline freshness roll-up ([:106-108](../../src/concord/web/routes_bills.py)) moves onto the aggregate. Note it imports `BILL_SECTIONS` from `concord.models.bills` for that roll-up.
- `src/concord/brief.py` — `build_facts` ([:113-162](../../src/concord/brief.py)), `BriefFacts` ([:55-83](../../src/concord/brief.py)): **core, pure, unchanged**.
- Tests: `tests/test_web_bills.py` (profile route, 936 lines), `tests/test_web_brief.py` (brief route, 308), `tests/test_brief.py` (pure `build_facts`, 347). Regression guards + new `from_sql` unit tests.

## Approach

### The model

`BillAggregate` is a Pydantic `BaseModel` in `web/search.py`. It holds the existing read shapes (no per-row re-modeling), plus the derived freshness fields:

```python
class BillAggregate(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)  # holds list[dict] / VoteHit
    bill: dict[str, Any]                 # full b.* + sponsor_display_name (richer than BillHit)
    cosponsors: list[dict[str, Any]]     # each row carries `party` (joined latest Term) for the fact pack
    actions: list[dict[str, Any]]
    subjects: list[str]
    titles: list[dict[str, Any]]
    summaries: list[dict[str, Any]]
    vote_history: list[VoteHit]
    updated_at: str                      # max(bill.fetched_at, section *_fetched_at) — was inline in the route
    any_missing: bool                    # any section *_fetched_at is NULL

    @classmethod
    def from_sql(cls, db: sqlite3.Connection, bill_id: str) -> "BillAggregate | None":
        """Reconstitute one Bill aggregate from the derived store, or None if absent."""
```

`from_sql` runs the identity query first (the `get_bill` SQL, by `bill_id`); returns `None` if absent. Then it runs the five section reads and `vote_history`, computes `updated_at`/`any_missing` from `BILL_SECTIONS` + the bill row's `*_fetched_at` columns (the logic currently inline at [routes_bills.py:106-108](../../src/concord/web/routes_bills.py)), and joins each cosponsor to their latest-Term `party` (the `cosponsor_party_breakdown` subquery, but carried per-row instead of pre-bucketed). It composes the existing reader functions where practical, so the section SQL isn't duplicated.

### Why the fact pack stays a *pure web projection*, not `build_facts(aggregate)`

The in-session shorthand was "`build_facts` becomes pure over the aggregate." There's a layering constraint to honor: `build_facts` lives in the **core** `concord.brief` module, and `BillAggregate` is a **web-layer** read-view model — core must not import web. So the resolution that preserves the intent (the fact-pack path no longer touches the DB; one source object) **without** inverting the dependency:

- `build_facts` (core) stays pure and unchanged — it still takes plain values.
- `assemble_facts` (web) is simplified from `assemble_facts(db, bill, *, cosponsors, subjects, actions, vote_count, summaries)` to **`assemble_facts(agg: BillAggregate) -> BriefFacts`** — a pure function (no `db`): it buckets cosponsor party from `agg.cosponsors` (via `_party_bucket`, which stays in `web/search.py`), plucks `agg.summaries[-1]`, counts `len(agg.actions)` / `len(agg.vote_history)`, and calls `build_facts(...)`. The web aggregate depends on core `build_facts` (web → core, allowed); core stays unaware of the aggregate.

So `cosponsor_party_breakdown`'s *SQL* moves into `from_sql`; its *bucketing* stays in the web layer, now driven off the aggregate. `assemble_facts` no longer needs `db`.

### Route shape after

Both routes collapse to: fetch the aggregate (one call), 404 on `None` their own way, render. For example, `bill_profile`:

```python
agg = BillAggregate.from_sql(db, bill_id)
if agg is None:
    return templates.TemplateResponse(request, "404.html", {...}, status_code=404)
# template context pulls agg.bill, agg.cosponsors, agg.actions, ..., agg.updated_at, agg.any_missing
if brief_enabled:
    brief_facts = assemble_facts(agg)          # pure, no db
    brief_view = cached_view(db, brief_facts, model=...)
```

`generate_brief` mirrors it (raising `HTTPException` on `None`, using `assemble_facts(agg)` then `get_or_generate_brief`). The `bill_id` for `from_sql` comes from resolving `(congress, bill_type, bill_number)` — keep a small identity lookup (or let `from_sql` accept the natural key); see Open questions.

## Step-by-step plan

1. **Add `BillAggregate` + `from_sql` to `web/search.py`.** Define the model beside `VoteHit`. Implement `from_sql(db, bill_id)`: identity query (reuse `get_bill`'s SQL/shape — accept `bill_id`, return `None` if absent), then compose `cosponsors_for_bill` (extended to carry `party`), `actions_for_bill`, `subjects_for_bill`, `titles_for_bill`, `summaries_for_bill`, `vote_history_for_bill`; compute `updated_at`/`any_missing` from `BILL_SECTIONS` + the bill row. Type everything (`mypy --strict`).

2. **Fold the party join into the cosponsor read.** Extend the aggregate's cosponsor rows to include `party` (the latest-Term subquery from `cosponsor_party_breakdown`, [search.py:822-836](../../src/concord/web/search.py)). Either extend `cosponsors_for_bill` to select `party` or run the joined query inside `from_sql`. Keep `_party_bucket` in `web/search.py`.

3. **Delete `cosponsor_party_breakdown`.** Once `from_sql` carries per-cosponsor party, remove the function ([search.py:814](../../src/concord/web/search.py)). Grep to confirm no other caller (`grep -rn cosponsor_party_breakdown src tests`).

4. **Simplify `assemble_facts` to a pure aggregate projection.** Change `web/brief.py:assemble_facts` to `assemble_facts(agg: BillAggregate) -> BriefFacts` (drop the `db` param). Bucket party from `agg.cosponsors` via `_party_bucket`, pluck `agg.summaries[-1]`, pass `action_count=len(agg.actions)` / `vote_count=len(agg.vote_history)` into the unchanged core `build_facts`. Confirm no other caller passes the old signature.

5. **Update `bill_profile`.** In `routes_bills.py`, replace the `get_bill` + six section reads + inline `updated_at`/`any_missing` block with `agg = BillAggregate.from_sql(db, bill_id)`; 404 on `None` as today (`404.html`); build the template context from `agg.*`; call `assemble_facts(agg)` when `brief_enabled`. Remove the now-unused per-section imports if any.

6. **Update `generate_brief`.** In `web/brief.py`, replace the `get_bill` + five section reads with `agg = BillAggregate.from_sql(db, bill_id)`; 404 on `None` via `HTTPException` as today; `facts = assemble_facts(agg)`; rest unchanged.

7. **Write `from_sql` unit tests** in `tests/test_web_bills.py` (or a new `tests/test_bill_aggregate.py`): a fully-populated Bill returns all sections incl. titles + `vote_history` as `VoteHit` + per-cosponsor `party` + correct `updated_at`/`any_missing`; a missing Bill returns `None`; a Bill with some sections unscraped sets `any_missing=True`. Add a `assemble_facts(agg)` test asserting the `BriefFacts` matches today's output for a fixture Bill (parity).

8. **Run the gate.** `uv run ruff format`, `uv run ruff check`, `uv run mypy src`, `uv run pytest`. `tests/test_web_bills.py`, `tests/test_web_brief.py`, `tests/test_brief.py` must pass — green-with-minimal-edits is the proof the rendered pages and briefs are unchanged.

## Demo seed data

No seed changes — this is a read-path refactor with no new tables, columns, entity types, or persisted state. `backend/demo/seed.sql` is untouched.

## Testing strategy

- **New unit tests (`from_sql` + `assemble_facts`):** the aggregate populates correctly (all six sections incl. titles, `VoteHit` nesting, per-cosponsor party, freshness fields), returns `None` for a missing Bill, and `assemble_facts(agg)` reproduces today's `BriefFacts` for a fixture (parity).
- **Regression guards (must pass with minimal edits):** `tests/test_web_bills.py` (profile renders the same content), `tests/test_web_brief.py` (brief generation path), `tests/test_brief.py` (pure `build_facts` unchanged). These prove behaviour-neutrality for pages and briefs.
- **Regression risk:** the titles-drift fix means the brief path now *fetches* titles (it didn't before) — confirm `build_facts` ignores titles (it does; titles aren't a fact-pack input), so the generated brief is unchanged. The party-split parity test guards the `cosponsor_party_breakdown` → `from_sql` move.
- **Manual check (optional):** `uv run concord serve` and load a populated Bill profile + generate a brief; confirm sections, the freshness line, and the fact card render identically.

## Acceptance criteria

- [ ] `BillAggregate` + `from_sql(db, bill_id)` exist in `web/search.py`; `from_sql` owns identity + all five sections + `vote_history` + per-cosponsor party + `updated_at`/`any_missing`.
- [ ] `cosponsor_party_breakdown` is deleted; its SQL lives in `from_sql`; `_party_bucket` retained.
- [ ] `assemble_facts(agg)` is pure (no `db` param); `build_facts`/`BriefFacts` in `concord.brief` are unchanged.
- [ ] `bill_profile` and `generate_brief` both fetch via `from_sql`; titles fetched in both paths; each route keeps its own 404 response.
- [ ] New tests cover `from_sql` (populated / missing / partial) and `assemble_facts` parity.
- [ ] `uv run ruff check`, `uv run ruff format --check`, `uv run mypy src` clean; `uv run pytest` green, with `tests/test_web_bills.py` / `test_web_brief.py` / `test_brief.py` passing.

## Open questions

- **`from_sql` key: `bill_id` vs natural key?** The internal `bill_id` is `"<congress>-<type>-<number>"`; the routes receive `(congress, bill_type, bill_number)`. Default: `from_sql(db, bill_id)` and have each route resolve the `bill_id` first (cheap), OR add a `from_natural_key(db, *, congress, bill_type, bill_number)` convenience classmethod. Pick whichever keeps the routes cleanest; not load-bearing.
- **`BillAggregate` as `BaseModel` vs `@dataclass`?** It holds `list[dict]` and `VoteHit`; a `BaseModel` needs `arbitrary_types_allowed` for the dict bags (or keep them as `list[dict[str, Any]]`, which Pydantic accepts). A frozen `@dataclass` is lighter and avoids validating bags it's just carrying. Default: `BaseModel` for family consistency with `BillHit`/`VoteHit`; switch to `@dataclass` if the dict-bag validation is awkward. Implementer's call.

## Out-of-band work

- Candidate 3 (split `web/search.py`; dedupe the entity-404 + pagination patterns) is the natural next step; `BillAggregate` is designed to move into whatever Bill read module that split creates. Not blocked by, and does not block, this plan.
