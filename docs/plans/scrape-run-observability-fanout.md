# Scrape-run observability — fan-out to text/senate clients + remaining scrapers (PR 2 of 2)

> Extend the Scrape Run ledger built in PR 1 to the two remaining HTTP clients (`text.py`, `senate_xml.py`) and the three remaining Stage-0 scrapers (Proceedings, Members, Votes), so every scrape across every source produces a complete, correctly-bucketed Scrape Run.

> ⚠️ **This plan is blocked by PR 1** ([scrape-run-observability-foundation.md](scrape-run-observability-foundation.md)) and was written against PR 1's *planned* API. The concrete shapes of `Recorder`, `scrape_run(...)`, the `Attempt`/`RunEvent` types, and the route-table helper may differ from what PR 1 actually ships. **Re-read PR 1's merged `observability.py` before starting and update the steps below to match the real signatures.** The decisions in [ADR 0021](../adr/0021-scrape-run-observability.md) are stable; the function-level details here are provisional.

## Source

- Continuation of the grilling session captured in **ADR 0021 — Scrape-run observability** ([docs/adr/0021-scrape-run-observability.md](../adr/0021-scrape-run-observability.md)) and the **Observability** section of [CONTEXT.md](../../CONTEXT.md). Predecessor plan: [scrape-run-observability-foundation.md](scrape-run-observability-foundation.md) (PR 1).

## Context

PR 1 delivered the ledger machinery (`concord/observability.py`: contextvars, `Recorder`, route table, `scrape_run(...)`, central logging) and the `runs`/`run_events` record tables, and proved them end-to-end on the **api.congress.gov** client and the **Bills** scraper. After PR 1, a Bills scrape records a full Scrape Run, but:

- **`text.py`** (congress.gov article-text fetches, with `AdaptiveThrottle` + the Cloudflare 403 pain) and **`senate_xml.py`** (senate.gov LIS XML, with the "404-disguised-as-200" trap) make network calls that are **not yet recorded**.
- The **Proceedings**, **Members**, and **Votes** Stage-0 paths are **not yet wrapped** in `scrape_run(...)`, so they produce no Scrape Run at all (Bills is the only wired scraper).

This PR closes both gaps. It is mostly mechanical — the hard design and the reusable machinery already exist — but `text.py` and `senate_xml.py` have their own retry loops and error types (`TextFetchError`, `SenateXmlError`) and full-URL endpoints (not api.congress.gov paths), so the route table and the attempt-accumulation need source-specific care.

Read [CONTEXT.md](../../CONTEXT.md) Observability terms and [ADR 0021](../adr/0021-scrape-run-observability.md) first.

## Goals

1. `text.py` records into the active recorder: successful fetches bucketed (`text:article`), and per-request error outcomes (403/429/5xx/transport) with retry resolution. 429/403 throttle attempts are recorded as errors per ADR 0021.
2. `senate_xml.py` records into the active recorder: successes bucketed (`senate:menu`, `senate:detail`, `senate:roster`), per-request error outcomes with resolution, and the HTML-not-XML "404-as-200" trap recorded as a `failed` request.
3. The Proceedings, Members, and Votes Stage-0 entry points are each wrapped in `scrape_run(...)` (matching the Bills wiring from PR 1), so every entity's scrape produces a Scrape Run.
4. The route table covers all `text:` and `senate:` URL shapes, with `text:unmatched` / `senate:unmatched` fallbacks.

## Non-goals

1. **Any change to PR 1's machinery semantics.** Reuse `Recorder`/`scrape_run`/the route table as shipped; if a real gap forces a change, prefer an additive extension and call it out.
2. **The `/runs` web dashboard** — still a later, separate effort.
3. **Retry-behavior changes** in any client — recording is additive only.
4. **Re-instrumenting `api.py` / re-wiring Bills** — done in PR 1.

## Relevant prior decisions

- **ADR 0021 — Scrape-run observability** ([docs/adr/0021-scrape-run-observability.md](../adr/0021-scrape-run-observability.md)) — governing decisions; all stable.
- **ADR 0007 — Parallel pipelines per entity** ([docs/adr/0007-parallel-pipelines-per-entity.md](../adr/0007-parallel-pipelines-per-entity.md)) — each scraper is wrapped independently; no shared base class. Repeating the `with scrape_run(...)` call across entities is the intended pattern, not duplication to DRY away.
- **PR 1 plan** ([scrape-run-observability-foundation.md](scrape-run-observability-foundation.md)) — the API this plan builds on.

## Relevant files and code

- `src/concord/observability.py` — **read as merged in PR 1.** Reuse `active_recorder`, `scrape_run`, the route table, and the `Attempt`/`RunEvent`/`note_*` API. Extend the route table with `text:` and `senate:` entries.
- `src/concord/text.py:223` — `_get_with_retry`, the text-fetch chokepoint; 403/429 at `:251`, 5xx at `:267`, transport give-up at `:239`, success return at `:294`. `fetch_text` at `:186` raises `TextFetchError` on a missing `<pre>` block at `:219` (a structural failure, `status_code=None`).
- `src/concord/senate_xml.py:147` — `SenateClient._get_xml`, the senate chokepoint; 5xx at `:163`, non-200 at `:172`, success at `:176`, terminal give-up at `:180`. `_ensure_xml` at `:184` raises on the HTML-as-200 trap. Callers: `roster` (`:119` → `ROSTER_URL`), `menu` (`:129` → `MENU_URL`), `detail` (`:143` → `DETAIL_URL`).
- `src/concord/scraper/proceedings.py:27` — `scrape(...)`, the single Proceedings Stage-0 entry (also calls `fetch_text`); wrap here or at its CLI seam.
- `src/concord/cli/proceedings.py:218` — `scrape_proceedings_command`; `:340` `run_proceedings_command`. Find the shared Stage-0 helper (analogous to Bills' `_run_scrape_bills`) and wrap it.
- `src/concord/cli/members.py:121` — `scrape members` command; `:203` `run members`.
- `src/concord/cli/votes.py:199` — `scrape votes` command; `:319` `run votes`. Votes uses both `api.py` (House) and `senate_xml.py` (Senate).
- `src/concord/scraper/{proceedings,members,votes}.py` — Stage-0 orchestration per entity.

## Approach

**Mirror PR 1's chokepoint pattern in two more clients.** Each of `text.py:_get_with_retry` and `senate_xml.py:_get_xml` gets the same additive treatment as `api.py:_get`: accumulate an `attempts` list across the retry loop (each non-success response/transport failure, including 403/429); on success, `note_success(source, url)` and, if attempts exist, `note_request_outcome(resolved=True)`; on terminal raise, `note_request_outcome(resolved=False)` first. Guard with `if rec is not None`. Two source-specific wrinkles:

- **text.py structural failure.** `fetch_text` can succeed at the HTTP layer but then raise `TextFetchError("no <pre> content")` (`status_code=None`). Record this as a `failed` request with a transport-class/structural marker so "page fetched but unparseable" is visible, not silently dropped. Do this in `fetch_text` (`:219`), not `_get_with_retry`.
- **senate_xml HTML trap.** `_ensure_xml` raising on an HTML 200 (`:184`) is a `failed` outcome even though the HTTP status was 200 — record it with a synthetic marker (e.g. `status=200, note="html-not-xml"`).

**Endpoints are full URLs here**, not api.congress.gov paths. Extend the route table with `text:` and `senate:` sections that match on the full URL: `congress.gov` article-text URLs → `text:article`; `senate.gov` URLs → `senate:menu` / `senate:detail` / `senate:roster` (match the `MENU_URL`/`DETAIL_URL`/`ROSTER_URL` shapes in `senate_xml.py`). Unmatched → `text:unmatched` / `senate:unmatched` with the same deduped-sample + WARNING behavior PR 1 established.

**Wrap the three remaining scrapers** exactly as PR 1 wrapped Bills: locate each entity's shared Stage-0 helper (used by both `scrape <entity>` and the Stage-0 phase of `run <entity>`) and wrap its body in `with scrape_run(entity=..., command=...):`, threading the ledger `db_path`. Proceedings is the interesting one — a single Proceedings scrape spans both `api.py` (issue/article metadata) and `text.py` (article text), so its Scrape Run will carry buckets from two sources; verify both appear. Votes similarly spans `api.py` (House) and `senate_xml.py` (Senate).

## Step-by-step plan

> First: re-read the merged `src/concord/observability.py` and reconcile the signatures below with what PR 1 actually shipped.

1. **Extend the route table with `text:` entries.** Add regexes matching congress.gov formatted-text article URLs → `text:article`; unknown congress.gov-text URL → `text:unmatched`. Unit-test a representative article URL maps to `text:article`.

2. **Extend the route table with `senate:` entries.** Match the `MENU_URL` shape → `senate:menu`, `DETAIL_URL` → `senate:detail`, `ROSTER_URL` → `senate:roster`; unknown senate.gov URL → `senate:unmatched`. Unit-test one URL of each kind (build them from `senate_xml.py`'s templates).

3. **Instrument `text.py:_get_with_retry`.** Accumulate `attempts` (403/429 at `:251`, 5xx at `:267`, transport at `:239`); on success (`:294`) `note_success("text", url)` + conditional `note_request_outcome(resolved=True)`; on terminal raises `note_request_outcome(resolved=False)`. Guard with `if rec is not None`. Tests via `httpx.MockTransport`: clean fetch, 403-then-200 (resolved, attempts include 403), terminal 5xx (failed).

4. **Record the structural failure in `fetch_text`.** At `:219`, before raising the "no `<pre>`" `TextFetchError`, record a `failed` request (`text:article`, synthetic structural attempt). Test: a 200 response whose body has no `<pre>` yields a `failed` event.

5. **Instrument `senate_xml.py:_get_xml`.** Accumulate `attempts` (5xx at `:163`, non-200 at `:172`, transport in the loop); success (`:176`) `note_success` + conditional resolved; terminal give-up (`:180`) `note_request_outcome(resolved=False)`. Guard with `if rec is not None`. Tests: clean, 5xx-then-200 (resolved), terminal failure.

6. **Record the HTML-as-200 trap.** Where `_ensure_xml` (`:184`) raises on HTML, record a `failed` request with a `html-not-xml` marker before the raise propagates. Test: an HTML 200 response yields a `failed` event for the right `senate:*` bucket.

7. **Wrap the Proceedings Stage-0 path** in `scrape_run(entity="proceedings", command=...)` at its shared helper / CLI seam (`cli/proceedings.py`). Verify a `concord scrape proceedings --limit 1` Scrape Run carries **both** `api:*` and `text:*` buckets.

8. **Wrap the Members Stage-0 path** (`cli/members.py`) in `scrape_run(entity="members", ...)`. Verify a Scrape Run with `api:member/list` buckets.

9. **Wrap the Votes Stage-0 path** (`cli/votes.py`) in `scrape_run(entity="votes", ...)`. Verify a Scrape Run carries `api:house-vote/*` and/or `senate:*` buckets depending on chamber.

10. **Confirm no `*:unmatched` buckets appear** in a real (or recorded) run of each entity — an unmatched bucket means a route-table gap to fix. Inspect the captured `unmatched_sample` if any appear.

## Demo seed data

Not applicable (same rationale as PR 1 — no `backend/demo/seed.sql`; the ledger self-populates on first scrape).

## Testing strategy

- **Unit:** route-table tests for `text:`/`senate:` shapes (steps 1–2); `httpx.MockTransport` recording tests for `text.py` and `senate_xml.py` chokepoints (steps 3–6), covering clean / resolved-on-retry / terminal-failed / structural-failure / html-trap.
- **Integration:** one `scrape` per entity (transport-mocked or live, `--limit 1`) persists a Scrape Run with the expected source buckets and **no** `*:unmatched` bucket.
- **Regression risk:** existing `text.py`, `senate_xml.py`, and per-entity scraper/pipeline tests must pass unchanged (retry behavior untouched). `uv run mypy src` clean. PR 1's tests must still pass — this PR must not alter `observability.py`'s public behavior.

## Acceptance criteria

- [ ] `text.py` and `senate_xml.py` record successes (bucketed) and per-request error outcomes with retry resolution into the active recorder.
- [ ] The "no `<pre>`" and "HTML-as-200" structural failures are recorded as `failed` events.
- [ ] Proceedings, Members, and Votes scrapes each produce a Scrape Run; Proceedings carries both `api:*` and `text:*` buckets; Votes carries `api:*` and/or `senate:*`.
- [ ] No `*:unmatched` buckets appear for normal runs of any entity.
- [ ] `uv run ruff check`, `uv run ruff format --check`, `uv run mypy src`, `uv run pytest` all pass; PR 1's tests still pass.

## Open questions

- **Did PR 1's API land as planned?** This is the standing risk for this plan. Reconcile `Recorder`/`scrape_run`/`Attempt`/`RunEvent`/route-table signatures against the merged code before writing any of the above; update steps accordingly.
- **Per-source `note_success` granularity for text.** `text:article` may be the only useful text bucket (there's effectively one endpoint shape). If the maintainer wants finer text buckets later, the route table can grow — not needed for this PR.

## Out-of-band work

- Depends on PR 1 being merged. The `/runs` dashboard remains a deferred, unplanned follow-up that will consume the now-fully-populated `runs`/`run_events` tables across all entities and sources.
