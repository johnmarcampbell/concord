# Resilient-fetch — migrate `senate_xml.py` + sentinel policy (PR 3 of 3)

> Move `concord.senate_xml` onto the `concord.fetch` module from PR 1 using the base (no-op) rate-limit policy plus a **not-found sentinel** that reclassifies senate.gov's "HTML-disguised-as-200" trap as a network failure — folding the three drifted retry settings into the shared spine.

## Source

- Conversation context (no durable issue): the `improve-codebase-architecture` session that designed the resilient-fetch deepening. This is PR 3 of 3.
- **Blocks on PR 1** — [resilient-fetch-foundation.md](resilient-fetch-foundation.md), which introduces `concord.fetch` (`Fetcher`, `RateLimitPolicy`, `Decision`, `Disposition`, `FetchError`). Independent of PR 2 ([resilient-fetch-text.md](resilient-fetch-text.md)) — can land before or after it.

## Context

`concord.senate_xml` fetches Senate roll-call data from `senate.gov`'s LIS XML feeds (Phase 3b votes; see ADR 0010). It is the simplest of the three clients: senate.gov has no observed rate limit, so the client has **no 403/429 handling** at all — it just retries transport errors and 5xx a few times. Its one quirk is the **HTML trap**: senate.gov returns `200 OK` with an HTML error page (not a 404) for a roll-call file that doesn't exist yet. The client detects this by Content-Type and raises `SenateXmlError`, recording it as a failed Run Event ([senate_xml.py:146-201, 255-272](../../src/concord/senate_xml.py)).

Across the three clients, senate's retry settings have **drifted** from the others with no justifying need: a bounded `for range(MAX_RETRIES=3)` loop (vs `while True` with cap 5), and an **uncapped** exponential backoff (vs capped at `MAX_BACKOFF=60`). PR 1 made the spine's settings canonical; this PR retires senate's copies and accepts the small, deliberate convergence.

The grilling settled two senate-specific decisions:

- **The HTML trap is a not-found sentinel** (a 404 the server mislabels as 200), so it stays a **network failure** — *not* a "structural failure" / content problem. This is **sentinel normalization**: rather than the fetch module counting the 200 as a success and the caller flagging the body, the senate policy reclassifies the 2xx as a terminal network failure inside the fetch seam. The recorded ledger outcome is **identical to today** (a `failed` Run Event, not counted, carrying the `html-not-xml` marker); only the internal framing changes.
- **429 stays terminal** for senate. The base policy has no throttle branch, so a (never-actually-observed) 429 falls through to the spine's terminal path — exactly today's "any non-200 raises" behaviour.

Terms: see [CONTEXT.md](../../CONTEXT.md) (**Vote**, **Scrape Run**, **Run Event**, **Endpoint bucket**) and PR 1's "Two distinct failure concepts" (**network failure** vs **structural failure** vs **Load Validation Failure**).

## Goals

1. `concord.senate_xml._get_xml` fetches through `concord.fetch.Fetcher` instead of its own bounded retry loop; `SenateXmlError` remains the public exception.
2. A `SenateSentinelPolicy(RateLimitPolicy)` reclassifies a 2xx-with-HTML-Content-Type as `Decision(Disposition.REJECT, message="html-not-xml")`, preserving the trap's failed-event-not-counted ledger outcome with its marker.
3. senate's drifted settings are retired: `MAX_RETRIES=3` and the uncapped backoff give way to the spine's `MAX_5XX_RETRIES=5` and `MAX_BACKOFF=60` (the two **owned behaviour changes** below).
4. senate's copied helpers (`_note_attempt`, `_record_success`, `_record_failure`, `_record_html_trap`) and constants (`MAX_RETRIES`, `_BACKOFF_BASE`) are deleted.
5. `get_roster` / `get_vote_menu` / `get_vote_detail` and the parsers (`parse_vote_menu`, `parse_senate_roster`) are unchanged; the Senate vote loader needs no edits.

## Non-goals

1. Touching `api.py` or `text.py` (PRs 1 and 2).
2. Adding 403/429 throttle handling to senate (it has no observed rate limit; the base no-op policy is correct).
3. Changing `SenateVoteDetail.from_senate_xml` or any Stage-1 Senate vote parsing (ADR 0018 content validation; out of scope).
4. Changing `DETAIL_REQUEST_SLEEP_SECONDS` inter-request padding ([senate_xml.py:64](../../src/concord/senate_xml.py)) — that is scraper-level pacing, not part of the retry spine.
5. Introducing a "structural failure" Run Event kind — the trap is a *network* failure (sentinel), recorded exactly as today.

## Relevant prior decisions

- **PR 1 — [resilient-fetch-foundation.md](resilient-fetch-foundation.md)** *(blocking)*: defines `concord.fetch` and the `Decision(disposition, message)` return that lets this PR preserve the trap marker.
- ADR 0010 — Votes phased by chamber ([docs/adr/0010-votes-phased-by-chamber.md](../adr/0010-votes-phased-by-chamber.md)): senate votes come from senate.gov LIS XML; context for the source.
- ADR 0018 — Pydantic at the load boundary ([docs/adr/0018-pydantic-at-the-load-boundary.md](../adr/0018-pydantic-at-the-load-boundary.md)): XML *content* validation is `SenateVoteDetail.from_senate_xml` at Stage 1, untouched here.
- ADR 0021 — Scrape Run observability ([docs/adr/0021-scrape-run-observability.md](../adr/0021-scrape-run-observability.md)): the trap's failed-event-not-counted recording is preserved.
- ADR 0027 — Single network-only fetch seam *(created in PR 1)*: this PR adds the sentinel adapter and is where the "sentinel normalization" decision is realized.

## Relevant files and code

- `src/concord/senate_xml.py`:
  - `_get_xml(url) -> bytes` ([senate_xml.py:146-201](../../src/concord/senate_xml.py)) — the bounded retry loop to replace with `Fetcher.get`.
  - `_check_xml_content_type(response, url)` ([senate_xml.py:204-208](../../src/concord/senate_xml.py)) — the HTML-vs-XML test; its logic moves into the sentinel policy's `classify`.
  - `_record_html_trap` + `_HTML_TRAP_MARKER` ([senate_xml.py:217-272](../../src/concord/senate_xml.py)) — **deleted**; replaced by the `REJECT` decision carrying the marker.
  - `_note_attempt` / `_record_success` / `_record_failure` ([senate_xml.py:222-252](../../src/concord/senate_xml.py)) — **deleted** (spine handles).
  - Constants `MAX_RETRIES=3`, `_BACKOFF_BASE=2.0`, `HTTP_OK`, `HTTP_SERVER_ERROR_MIN/MAX` ([senate_xml.py:57-69](../../src/concord/senate_xml.py)) — **deleted** (spine owns the retry settings; the sentinel uses `response.is_success` + Content-Type, so the status constants are no longer needed locally).
  - Public methods `get_roster`/`get_vote_menu`/`get_vote_detail` ([senate_xml.py:118-142](../../src/concord/senate_xml.py)) and parsers — **unchanged**.
- `src/concord/fetch.py` — add `SenateSentinelPolicy` here, or in `senate_xml.py` (see Open questions).
- `tests/test_senate_xml.py` — functional suite (URL templating, parsing, HTML-trap → `SenateXmlError`). Stays; minimal edits.
- `tests/test_senate_xml_recording.py` — Run Event assertions ([tests/test_senate_xml_recording.py](../../tests/test_senate_xml_recording.py)); generic spine cases move to `tests/test_fetch.py` (PR 1), HTML-trap case stays.

## Approach

### The sentinel policy

senate needs no rate-limit handling, so it starts from the base no-op `RateLimitPolicy` and adds only the not-found sentinel:

```python
class SenateSentinelPolicy(RateLimitPolicy):
    def classify(self, response: httpx.Response) -> Decision:
        if response.is_success and "text/html" in response.headers.get("Content-Type", "").lower():
            # senate.gov's "404-as-200" trap for a missing roll-call file.
            return Decision(Disposition.REJECT, message=_HTML_TRAP_MARKER)   # "html-not-xml"
        return Decision(Disposition.PASS)
    # before_request / on_success inherited as no-ops; no throttle branch ⇒ 429 stays terminal.
```

The spine's `REJECT` path notes `Attempt(status=200, message="html-not-xml")` and records a `failed` Run Event without counting a success — byte-for-byte the same ledger result as today's `_record_html_trap`, which is why this is a *normalization*, not a behaviour change in the ledger.

`_get_xml` becomes a thin wrapper that translates the module's exception back to the public one:

```python
def _get_xml(self, url: str) -> bytes:
    try:
        return self._fetch.get(url)               # bytes (raw XML); raises FetchError
    except FetchError as exc:
        raise SenateXmlError(str(exc)) from exc
```

`self._fetch` is built once in `SenateClient.__init__`: `Fetcher(self._client, source="senate", policy=SenateSentinelPolicy(), sleep=self._sleep)`.

### Owned behaviour changes (call these out in the PR description)

Both are deliberate convergence of accidental drift; both are negligible:

1. **Transient retry cap 3 → 5.** On a persistently failing transport/5xx endpoint, senate now retries up to 5 times (spine default) instead of 3 before raising `SenateXmlError`. Slightly more resilient; no functional change to success paths.
2. **Backoff now capped at `MAX_BACKOFF=60`.** senate's old `_BACKOFF_BASE**attempt` was uncapped; with the (formerly 3-attempt) loop the delays were tiny, so in practice the cap never bites (the 5-attempt schedule is 1, 2, 4, 8, 16s, all < 60). Total retry wall-time on a persistent failure rises from ~3s to ~15s. Negligible and arguably correct.

Everything else is preserved: the HTML-trap ledger outcome (incl. marker), 429-stays-terminal, any-other-non-2xx-terminal, and `SenateXmlError` as the public exception.

## Step-by-step plan

1. **Add `SenateSentinelPolicy`.** Implement per the "Approach" sketch (in `concord.fetch` or `senate_xml.py` — see Open questions). It overrides only `classify`; `before_request`/`on_success` stay the inherited no-ops. Reuse `_check_xml_content_type`'s logic (you can keep that helper and call it, or inline the Content-Type test). Keep `_HTML_TRAP_MARKER` for the `Decision.message`.

2. **Wire the `Fetcher` into `SenateClient`.** In `SenateClient.__init__` ([senate_xml.py:85-98](../../src/concord/senate_xml.py)), after building `self._client`, add `self._fetch = Fetcher(self._client, source="senate", policy=SenateSentinelPolicy(), sleep=self._sleep)`.

3. **Rewrite `_get_xml`.** Replace the bounded retry loop ([senate_xml.py:146-201](../../src/concord/senate_xml.py)) with the thin wrapper from "Approach": call `self._fetch.get(url)`, translate `FetchError` → `SenateXmlError`. Confirm the three callers (`get_roster`, `get_vote_menu`, `get_vote_detail`) still type-check (they pass a URL string and get bytes back, unchanged).

4. **Delete senate's copied helpers and constants.** Remove `_note_attempt`, `_record_success`, `_record_failure`, `_record_html_trap` ([senate_xml.py:222-272](../../src/concord/senate_xml.py)) and the constants `MAX_RETRIES`, `_BACKOFF_BASE`, `HTTP_OK`, `HTTP_SERVER_ERROR_MIN/MAX` ([senate_xml.py:57-69](../../src/concord/senate_xml.py)). Keep `_check_xml_content_type` only if Step 1 reuses it; otherwise delete it too. Update the `concord.models.runs.Attempt` import if it becomes unused (the policy doesn't build `Attempt`s — the spine does — so this import likely goes).

5. **Update the recording tests.** Move generic-spine cases in `tests/test_senate_xml_recording.py` to `tests/test_fetch.py` (PR 1) if not already there. Keep the HTML-trap case: a 200 with `Content-Type: text/html` ⇒ a `failed` Run Event whose attempt carries status 200 and message `html-not-xml`, **no** success count, and `SenateXmlError` raised. Add/keep a case proving 5xx now retries up to 5 and an injected `sleep` sees the capped schedule.

6. **Adjust `tests/test_senate_xml.py` if needed.** Its HTML-trap assertion should still pass (still raises `SenateXmlError`). If any test asserted the old 3-attempt count or uncapped delays, update it to the converged values and note the change in the PR description.

7. **Run the full gate.** `uv run ruff format`, `uv run ruff check`, `uv run mypy src`, `uv run pytest`. Confirm import-cycle and smoke tests pass.

## Demo seed data

No seed changes — pure refactor of network plumbing, no new persisted state.

## Testing strategy

- **Adjusted — `tests/test_senate_xml_recording.py`**: the HTML-trap → `failed` event (status 200, marker `html-not-xml`, not counted) is the key senate-specific assertion and must be preserved exactly. Generic-spine cases deleted (now in `tests/test_fetch.py`). Add a converged-retry case (5xx retried up to 5, capped backoff).
- **Unchanged/minor — `tests/test_senate_xml.py`**: URL templating, menu/roster parsing, HTML-trap surfaces `SenateXmlError`. Update only if a test pinned the old `MAX_RETRIES=3` or uncapped delays.
- **Reused — `tests/test_fetch.py`** (PR 1): the shared spine; add a `SenateSentinelPolicy` case if the `REJECT`/marker path isn't otherwise covered.
- **Regression risk:** `tests/test_api*.py` and `tests/test_text*.py` must still pass (proof PR 3 didn't disturb the other clients). `tests/test_pipeline_votes.py` / `tests/test_index_votes.py` exercise the Senate vote path end-to-end against fixtures — they must stay green (the bytes returned by `_get_xml` are unchanged).
- **Manual check (optional):** with network access, `uv run concord scrape votes --congresses 119 --chambers senate --limit 5` and confirm Senate detail XML is fetched, missing-roll HTML traps are reported as Run Events (not counted as successes), and the Scrape Run summary's `senate:*` buckets look right.

## Acceptance criteria

- [ ] `concord.senate_xml._get_xml` fetches via `concord.fetch.Fetcher`; the bounded retry loop is deleted; `SenateXmlError` is still the public exception.
- [ ] `SenateSentinelPolicy` reclassifies 2xx-with-HTML as `Decision(Disposition.REJECT, message="html-not-xml")`; the resulting Run Event matches the old `_record_html_trap` (failed, status 200, marker, not counted).
- [ ] senate's `MAX_RETRIES`/`_BACKOFF_BASE`/`HTTP_*` constants and the `_note_attempt`/`_record_success`/`_record_failure`/`_record_html_trap` helpers are deleted.
- [ ] The two owned behaviour changes (cap 3→5; backoff capped at 60) are noted in the PR description.
- [ ] `get_roster`/`get_vote_menu`/`get_vote_detail` and the parsers are unchanged; the Senate vote loader is unedited.
- [ ] `uv run ruff check`, `uv run ruff format --check`, `uv run mypy src` clean; `uv run pytest` green including import-cycle, smoke, and Senate pipeline tests.

## Open questions

- **Q: Does `SenateSentinelPolicy` live in `concord.fetch` or `concord.senate_xml`?** It depends only on `RateLimitPolicy` + the Content-Type test. Default: **keep it in `senate_xml.py`** (the sentinel is a senate.gov-specific quirk; locality), mirroring PR 2's choice for `AdaptiveThrottlePolicy`. Implementer's call, but keep the two consistent.
- **Q: Is `REJECT` ever appropriate for a non-2xx?** Here it only fires on a 2xx (the trap). A genuine 404 from senate.gov stays on the `PASS`-then-terminal path. No need to generalize.

## Out-of-band work

- None beyond the PR-1 dependency. After this PR, all three clients share `concord.fetch`; the three drifted retry implementations are gone and the Run Event protocol has exactly one home.
