# Resilient-fetch — migrate `text.py` + adaptive-throttle adapter (PR 2 of 3)

> Move `concord.text` onto the `concord.fetch` module from PR 1, expressing its Cloudflare-aware `AdaptiveThrottle` as the second rate-limit policy adapter, while keeping the `<pre>` extraction (a structural-failure concern) downstream of a counted network success.

## Source

- Conversation context (no durable issue): the `improve-codebase-architecture` session that designed the resilient-fetch deepening. This is PR 2 of 3.
- **Blocks on PR 1** — [resilient-fetch-foundation.md](resilient-fetch-foundation.md). That PR introduces `concord.fetch` (`Fetcher`, `RateLimitPolicy`, `Decision`, `Disposition`, `FetchError`, the shared constants/helpers). Do not start until it has merged. Independent of PR 3.

## Context

`concord.text` fetches Congressional Record article HTML from `www.congress.gov` and returns the plain text inside the page's single `<pre>` block. Unlike `api.congress.gov`, this tier sits behind Cloudflare with an undocumented, **per-client** rate limit, so `text.py` carries a stateful `AdaptiveThrottle` (AIMD pacing + escalating cooldowns on 403/429) shared across every fetch in a run. See the module docstring ([text.py:12-52](../../src/concord/text.py)) and ADR 0002 ("being throttled is a wait, never a data loss").

PR 1 extracted the network-resilience spine (retry / backoff / Run Event recording) into `concord.fetch.Fetcher`, leaving rate-limit handling to an injected `RateLimitPolicy`. This PR makes `text.py` the second client on that spine: the `AdaptiveThrottle`'s three touchpoints map exactly onto the policy's three hooks. The migration is **behaviour-neutral** for `text.py`.

Domain/architecture terms: see [CONTEXT.md](../../CONTEXT.md) (**Scrape Run**, **Run Event**, **Proceeding**) and PR 1's "Two distinct failure concepts" section. The key one here: text's "fetched a real page but it has no `<pre>` block" is a **structural failure** — a genuinely successful network fetch whose body is unusable, recorded *beside* a counted success — and it must stay exactly where it is.

## Goals

1. `concord.text` performs its network fetch through `concord.fetch.Fetcher` instead of its own `_get_with_retry` loop.
2. The `AdaptiveThrottle` is wrapped in a new `AdaptiveThrottlePolicy(RateLimitPolicy)` whose `before_request` → `pace()`, `classify` → throttle-on-403/429 (sleeping via `penalize`), and `on_success` → `recover()`.
3. `text.py`'s copied network constants (`MAX_BACKOFF`, `MAX_5XX_RETRIES`, `_BACKOFF_BASE`) and copied helpers (`_get_with_retry`, `_note_attempt`, `_record_success`, `_record_failure`, `_backoff_seconds`) are deleted in favour of `concord.fetch`'s.
4. The `<pre>` extraction and its `_record_structural_failure` stay in `fetch_text`, downstream of a counted network success — behaviour unchanged.
5. `text.py` stops importing HTTP status constants from `concord.api` and imports them from `concord.fetch`; PR 1's compatibility re-export in `api.py` is removed.
6. `fetch_text`'s public signature and `TextFetchError` are unchanged; callers (the Proceedings scraper) need no edits.

## Non-goals

1. Changing the `AdaptiveThrottle`'s pacing/cooldown algorithm or its tunables (`_INITIAL_PACE`, `MAX_COOLDOWN`, etc.). They move verbatim; only their *invocation site* changes.
2. Touching `senate_xml.py` (PR 3) or `api.py` beyond removing the re-export added in PR 1.
3. Re-homing the `<pre>` extraction into a Pydantic factory or restructuring Proceeding storage (ADR 0002 keeps Proceedings as extracted text; out of scope).
4. Introducing a "structural failure" Run Event kind — `_record_structural_failure` already exists and is unchanged.

## Relevant prior decisions

- **PR 1 — [resilient-fetch-foundation.md](resilient-fetch-foundation.md)** *(blocking)*: defines everything in `concord.fetch` this PR builds on.
- ADR 0021 — Scrape Run observability ([docs/adr/0021-scrape-run-observability.md](../adr/0021-scrape-run-observability.md)): `text.py`'s structural failure is recorded via `note_request_outcome(..., resolved=False)`; that path is preserved.
- ADR 0002 — JSONL as canonical raw store ([docs/adr/0002-jsonl-as-canonical-raw-store.md](../adr/0002-jsonl-as-canonical-raw-store.md)): re-runs are cheap, so indefinite throttle waiting is safe; Proceedings persist as extracted text.
- ADR 0007 — Parallel pipelines per entity ([docs/adr/0007-parallel-pipelines-per-entity.md](../adr/0007-parallel-pipelines-per-entity.md)): the throttle state is per-run and single-threaded (the scrapers do no threading), so sharing one `AdaptiveThrottle` behind the policy is safe.
- ADR 0027 — Single network-only fetch seam *(created in PR 1)*: this PR adds the second policy adapter it anticipates.

## Relevant files and code

- `src/concord/text.py` — the module to migrate:
  - `fetch_text(url, client, *, throttle, sleep) -> str` ([text.py:251-291](../../src/concord/text.py)) — public entry; keeps the `<pre>` extraction.
  - `AdaptiveThrottle` with `pace()` / `penalize(retry_after)` / `recover()` ([text.py:180-248](../../src/concord/text.py)) — wrapped, not changed.
  - `_get_with_retry(url, client, throttle, sleep) -> httpx.Response` ([text.py:294-386](../../src/concord/text.py)) — **deleted**; its loop is now `Fetcher.get`.
  - `_PreExtractor` + `_record_structural_failure` + `_NO_PRE_MARKER` ([text.py:280-291, 396-446](../../src/concord/text.py)) — **kept**, downstream of the fetch.
  - Network constants `MAX_BACKOFF`/`MAX_5XX_RETRIES`/`_BACKOFF_BASE` ([text.py:74-80](../../src/concord/text.py)) — **deleted** (import from `concord.fetch`).
  - `_retry_after_seconds` (clamped to `MAX_COOLDOWN=900`, **not** `MAX_BACKOFF`) + `_format_retry_after` ([text.py:454-478](../../src/concord/text.py)) — **kept** in `text.py`; used by the throttle cooldown, distinct from `api`'s 60-clamped version.
  - `from concord.api import HTTP_FORBIDDEN, HTTP_SERVER_ERROR_MAX, HTTP_SERVER_ERROR_MIN, HTTP_TOO_MANY_REQUESTS` ([text.py:62-67](../../src/concord/text.py)) — **repoint** to `concord.fetch`.
- `src/concord/api.py` — remove the `HTTP_*` re-export added in PR 1 Step 6 (now that `text.py` imports from `concord.fetch`).
- `src/concord/fetch.py` — add `AdaptiveThrottlePolicy` here (it is a `text`-tier concern but depends only on `RateLimitPolicy`; alternatively keep it in `text.py`. See Open questions).
- `tests/test_text.py` — functional suite (extraction, error surfaces). Should pass with minimal/no edits.
- `tests/test_text_recording.py` — Run Event assertions ([tests/test_text_recording.py](../../tests/test_text_recording.py)); the generic spine cases move to `tests/test_fetch.py` (created in PR 1), leaving throttle-specific + structural-failure cases.

## Approach

### The adaptive-throttle adapter

`text.py`'s `AdaptiveThrottle` already has exactly the three touchpoints the policy seam exposes; the migration is a wrapper:

```python
class AdaptiveThrottlePolicy(RateLimitPolicy):
    def __init__(self, throttle: AdaptiveThrottle) -> None:
        self._throttle = throttle

    def before_request(self) -> None:
        self._throttle.pace()                       # paces EVERY request (happy path included)

    def classify(self, response: httpx.Response) -> Decision:
        if response.status_code in (HTTP_FORBIDDEN, HTTP_TOO_MANY_REQUESTS):
            self._throttle.penalize(_retry_after_seconds(response))   # sleeps the cooldown, escalates strikes
            return Decision(Disposition.THROTTLE)
        return Decision(Disposition.PASS)

    def on_success(self) -> None:
        self._throttle.recover()                    # reset strikes, decay pace
```

Note `classify` calls `penalize` (which sleeps) itself, consistent with PR 1's "the policy owns its cooldown wait" design. `_retry_after_seconds` is `text.py`'s own cooldown-clamped version (cap 900), not `concord.fetch`'s 60-clamped one — keep it in `text.py`.

`fetch_text` builds the policy and a per-call `Fetcher` (the *state* lives in the shared `AdaptiveThrottle`, so a throwaway `Fetcher` per call is fine):

```python
def fetch_text(url, client, *, throttle=None, sleep=time.sleep) -> str:
    throttle = throttle or AdaptiveThrottle(sleep=sleep)
    fetcher = Fetcher(client, source="text", policy=AdaptiveThrottlePolicy(throttle), sleep=sleep)
    try:
        body = fetcher.get(url)                      # bytes; raises FetchError
    except FetchError as exc:
        raise TextFetchError(str(exc), status_code=exc.status_code) from exc
    text = _extract_pre(body)                        # decode + <pre> parse
    if not text:
        _record_structural_failure(active_recorder(), url)   # beside the counted success
        raise TextFetchError(f"no <pre> content found at {url}")
    return text
```

### Bytes → text decode (the one fidelity point)

`Fetcher.get` returns raw `bytes`; the old `_get_with_retry` returned an `httpx.Response` and the extractor consumed `response.text`. Decode the bytes as **UTF-8** (`www.congress.gov` CREC pages are UTF-8) before feeding `_PreExtractor`. Verify this matches the prior output against the recorded fixtures in `tests/test_text.py` / `tests/test_text_recording.py` (they use `httpx.MockTransport` with fixed bodies, so the decode is deterministic). If any fixture is non-UTF-8, fall back to `httpx`'s charset detection by having `Fetcher.get` expose it — but the default expectation is plain UTF-8. Flagged under Open questions.

### Behaviour preservation

text's transient cap is already 5 (`MAX_5XX_RETRIES`), matching `concord.fetch`'s default — **no drift to converge** (unlike PR 3). The 403/429-as-throttle, indefinite cooldown waiting, AIMD pacing, and 5xx/transport budget are all preserved: pacing and cooldowns move into the policy unchanged; the 5xx/transport loop moves into the shared `Fetcher`. The structural-failure path is byte-identical. This PR should produce **no observable behaviour change** for `text.py`.

## Step-by-step plan

1. **Repoint `text.py`'s constant imports.** Change [text.py:62-67](../../src/concord/text.py) to `from concord.fetch import HTTP_FORBIDDEN, HTTP_SERVER_ERROR_MAX, HTTP_SERVER_ERROR_MIN, HTTP_TOO_MANY_REQUESTS`. Delete `text.py`'s own `MAX_BACKOFF`, `MAX_5XX_RETRIES`, `_BACKOFF_BASE` ([text.py:74-80](../../src/concord/text.py)) and import them from `concord.fetch` where still referenced. Keep `MAX_COOLDOWN` and all throttle tunables in `text.py`.

2. **Add `AdaptiveThrottlePolicy`.** Implement it per the "Approach" sketch (in `concord.fetch` or `text.py` — see Open questions). It wraps an `AdaptiveThrottle`; `classify` calls `penalize` and returns `Decision(Disposition.THROTTLE)` on 403/429, else `Decision(Disposition.PASS)`. Use `text.py`'s cooldown-clamped `_retry_after_seconds`.

3. **Rewrite `fetch_text` to use `Fetcher`.** Per the "Approach" sketch: build the throttle, wrap in the policy, construct a per-call `Fetcher(client, source="text", policy=..., sleep=sleep)`, call `fetcher.get(url)`, translate `FetchError` → `TextFetchError`, then run the existing `<pre>` extraction (decoding bytes as UTF-8) and the `_record_structural_failure` path. Keep the exact `TextFetchError` messages/`status_code` semantics from the current docstring ([text.py:273-277](../../src/concord/text.py)).

4. **Delete `_get_with_retry` and the copied helpers.** Remove `_get_with_retry` ([text.py:294-386](../../src/concord/text.py)), `_note_attempt`, `_record_success`, `_record_failure`, and `_backoff_seconds`. Keep `_record_structural_failure`, `_NO_PRE_MARKER`, `_retry_after_seconds`, `_format_retry_after`, `_PreExtractor`, `AdaptiveThrottle`.

5. **Remove PR 1's compatibility re-export.** In `api.py`, delete the `HTTP_*` re-export added in PR 1 Step 6 (nothing imports them from `concord.api` anymore — grep to confirm: `grep -rn "from concord.api import" src tests`).

6. **Migrate the recording tests.** Move any remaining generic-spine assertions in `tests/test_text_recording.py` to `tests/test_fetch.py` (PR 1) if not already covered. Keep/adjust the throttle-specific cases (403/429 → cooldown, pacing applied, strikes escalate) and the structural-failure case (200 with no `<pre>` ⇒ counted success **and** a `failed` event). Assert via injected `sleep`/`rng` so no real wall time and deterministic jitter.

7. **Run the full gate.** `uv run ruff format`, `uv run ruff check`, `uv run mypy src`, `uv run pytest`. Confirm `tests/test_text.py` (functional) passes with no behavioural edits and `tests/test_import_cycles.py` passes.

## Demo seed data

No seed changes — pure refactor of network plumbing, no new persisted state.

## Testing strategy

- **Unchanged — `tests/test_text.py`**: extraction correctness, `TextFetchError` on no-`<pre>` and on terminal status, redirect following. The primary regression guard that `fetch_text`'s contract is preserved. Should need no behavioural edits.
- **Adjusted — `tests/test_text_recording.py`**: throttle-specific Run Event behaviour (403/429 cooldown + indefinite retry, pacing on the happy path) and the structural-failure record-beside-success case. Generic spine cases deleted (now in `tests/test_fetch.py`).
- **Reused — `tests/test_fetch.py`** (from PR 1): the shared spine doesn't need re-testing here; add an `AdaptiveThrottlePolicy` case if its `classify`/`penalize` wiring isn't otherwise exercised.
- **Regression risk:** the bytes→UTF-8 decode must reproduce the prior `<pre>` text exactly (assert against existing fixtures). `tests/test_api*.py` and `tests/test_senate_xml*.py` must still pass (proof PR 2 didn't disturb the other clients).
- **Manual check (optional):** `uv run concord run proceedings --from 2026-05-22 --to 2026-05-22` against a real run; confirm article text is extracted and the Scrape Run summary reports `text:article` success counts.

## Acceptance criteria

- [ ] `concord.text.fetch_text` fetches via `concord.fetch.Fetcher`; `_get_with_retry` is deleted.
- [ ] `AdaptiveThrottlePolicy` maps `pace`/`penalize`/`recover` onto `before_request`/`classify`/`on_success`.
- [ ] `text.py` imports `HTTP_*` from `concord.fetch`; its copied `MAX_BACKOFF`/`MAX_5XX_RETRIES`/`_BACKOFF_BASE` are gone.
- [ ] The `<pre>` extraction + `_record_structural_failure` remain in `fetch_text`; structural-failure-beside-success behaviour is unchanged.
- [ ] PR 1's `HTTP_*` re-export in `api.py` is removed; no module imports those names from `concord.api`.
- [ ] `fetch_text`'s signature and `TextFetchError` are unchanged; the Proceedings scraper is unedited.
- [ ] `uv run ruff check`, `uv run ruff format --check`, `uv run mypy src` clean; `uv run pytest` green including import-cycle + smoke tests.

## Open questions

- **Q: Does `AdaptiveThrottlePolicy` live in `concord.fetch` or `concord.text`?** It only depends on `RateLimitPolicy` + an `AdaptiveThrottle`. Putting it in `text.py` keeps the Cloudflare specifics with the only client that needs them and avoids `fetch.py` importing `AdaptiveThrottle`; putting it in `fetch.py` co-locates all policies. Default: **keep it in `text.py`** (locality of the Cloudflare concern). Implementer's call.
- **Q: Is UTF-8 the correct decode for all `www.congress.gov` CREC bodies?** Expected yes; verify against `tests/test_text*.py` fixtures. If a fixture proves otherwise, expose `httpx`'s charset detection from `Fetcher.get` rather than hardcoding. Don't ship without confirming the decode reproduces prior output.

## Out-of-band work

- This PR removes the temporary `HTTP_*` re-export PR 1 added to `api.py`. Coordinate only in the sense that PR 1 must be merged first; no coordination with PR 3 (independent).
