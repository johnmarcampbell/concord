# Resilient-fetch module — foundation + `api.py` migration (PR 1 of 3)

> Extract the retry / backoff / Run-Event-recording loop duplicated across Concord's three HTTP clients into one deep, network-only `concord.fetch` module, then migrate `concord.api` onto it as the first adapter.

## Source

- Conversation context (no durable issue): an `improve-codebase-architecture` session that identified the three HTTP clients (`api.py`, `text.py`, `senate_xml.py`) as each re-implementing one resilient-fetch behaviour, then grilled the design to ground. This plan is PR 1 of a 3-PR split. The design decisions below were settled in that session; there is no external doc.
- Sibling plans (same effort): [resilient-fetch-text.md](resilient-fetch-text.md) (PR 2), [resilient-fetch-senate.md](resilient-fetch-senate.md) (PR 3). **Both block on this plan.**

## Context

Concord has three HTTP clients, one per upstream source:

- `concord.api` — `api.congress.gov` JSON (Bills, Members, House Votes, Proceedings metadata).
- `concord.text` — `www.congress.gov` HTML behind Cloudflare (Proceeding article text).
- `concord.senate_xml` — `senate.gov` LIS XML (Senate Votes).

Each independently implements the **same network-resilience spine**: a retry loop over an `httpx` transport, exponential backoff, indefinite waiting on rate-limit signals, and the **Scrape Run** observability protocol (ADR 0021) — incrementing an endpoint-bucketed success count on a clean fetch and emitting a **Run Event** for any request that saw ≥1 non-success attempt. The three copies have already drifted (different retry caps, one uncapped backoff, `text.py` reaching into `concord.api` for HTTP status constants), and the Run Event contract is re-asserted in each.

The grilling established that the genuine variation between the clients collapses to **one axis — rate-limit / throttle policy** — and that *content* validation is not a fetch concern at all (it already lives at Stage 1 via the `from_<source>` Pydantic factories, per ADR 0018). So the deep module is **network-only**: it returns raw bytes on a successful fetch and never inspects the body. See "Approach" for the full rationale.

Domain terms used below are defined in [CONTEXT.md](../../CONTEXT.md): **Scrape Run**, **Run Event**, **Endpoint bucket**, **Load Validation Failure**, **Stage 0 / Stage 1**, **wire-shape model**, the `from_<source>` factory. Architecture terms (**deep module**, **seam**, **adapter**) follow the `improve-codebase-architecture` skill's LANGUAGE.md.

### Two distinct failure concepts (sharpened during grilling)

These were repeatedly conflated in the current code; the new design keeps them separate. Use these names:

- **Network failure** — the server did not return the bytes we asked for: transport error, exhausted-5xx, terminal 4xx, or a **not-found sentinel** (see below). Owned by the fetch module. Affects the Scrape Run success count and emits a Run Event.
- **Structural failure** — a genuinely successful fetch whose body is unusable in a way only visible *downstream* (e.g. `text.py`'s "fetched a real page, but it has no `<pre>` block"). The project's own term, currently only used in `text.py`. Stays with the caller, recorded beside a counted network success. **Not introduced or changed by this PR** — listed here only so the boundary is clear.
- **Load Validation Failure** — a Stage-1 concept (ADR 0023): an upstream payload that fails its `from_<source>` Pydantic contract. This is where *content* validation lives. The fetch module never does this.

## Goals

1. A new `src/concord/fetch.py` module owns the network-resilience spine — retry loop, exponential backoff, success/failure Run Event recording — behind a small interface, with `httpx` as its one injected dependency (the test seam).
2. The rate-limit variation is expressed as a **policy** seam: a base no-op policy plus one concrete adapter for `api.congress.gov` (the **retry-after** policy). PRs 2 and 3 add the other two adapters.
3. `concord.api` is migrated onto `concord.fetch`: its `_get` becomes a thin wrapper that calls the module for bytes, then does only JSON-parse + pagination-envelope concerns. `ApiError` remains the public exception.
4. The duplicated constants and the `_note_attempt` / `_record_success` / `_record_failure` helper trio that `api.py` defines move into `concord.fetch` (PRs 2 and 3 delete their copies).
5. A new ADR records the decision: one network-only fetch seam; content validation stays at Stage 1.
6. Behaviour for `api.congress.gov` is preserved except for two small, explicitly-owned changes (see "Approach → Owned behaviour changes").

## Non-goals

1. **Migrating `text.py` or `senate_xml.py`.** Those are PRs 2 and 3. This PR must leave them compiling and passing unchanged — including `text.py`'s current `from concord.api import HTTP_*` (see "Approach → Constant compatibility").
2. **Touching content validation.** No changes to any `from_congress_api` factory, the Stage-1 loaders, or Load Validation Failures.
3. **Introducing a "structural failure" Run Event kind.** We explicitly decided against this — content/structural failures are already handled (Stage 1 for `api`; `text.py`'s existing `_record_structural_failure` for Proceedings). The Run Event model is unchanged.
4. **Changing the `AdaptiveThrottle`** (PR 2's concern) or senate's behaviour (PR 3's concern).
5. **Re-homing `text.py`'s `<pre>` extraction** or restructuring Proceedings storage.

## Relevant prior decisions

- ADR 0021 — Scrape Run observability ([docs/adr/0021-scrape-run-observability.md](../adr/0021-scrape-run-observability.md)). Defines the Recorder protocol the fetch module must speak: `note_success(source, path)` and `note_request_outcome(source, path, attempts, *, resolved)`. The module preserves this exactly.
- ADR 0018 — Pydantic at the load boundary ([docs/adr/0018-pydantic-at-the-load-boundary.md](../adr/0018-pydantic-at-the-load-boundary.md)). Content validation lives at Stage 1 via `from_<source>` factories. The fetch module being network-only *leans into* this, not against it.
- ADR 0002 — JSONL as canonical raw store ([docs/adr/0002-jsonl-as-canonical-raw-store.md](../adr/0002-jsonl-as-canonical-raw-store.md)). "Being throttled is a wait, never a data loss" — re-runs are cheap, which is why indefinite waiting on rate limits is safe.
- ADR 0007 — Parallel pipelines per entity ([docs/adr/0007-parallel-pipelines-per-entity.md](../adr/0007-parallel-pipelines-per-entity.md)). The fetch module is a **thin shared helper, not a base class** — same status as `concord.observability`. It does *not* unify the clients into a class hierarchy; each client keeps its own URL-building and model-parsing.
- ADR 0022 — Internal import convention ([docs/adr/0022-internal-import-convention.md](../adr/0022-internal-import-convention.md)). Follow it for the new module's imports.
- **ADR 0027 — Single network-only fetch seam** *(new, created with this plan — see Step 7)*.

## Relevant files and code

- `src/concord/api.py` — the reference implementation. `Client._get` ([api.py:508-597](../../src/concord/api.py)) is the loop to extract; constants `MAX_BACKOFF` / `MAX_5XX_RETRIES` / `_BACKOFF_BASE` / `HTTP_*` ([api.py:66-82](../../src/concord/api.py)); helper trio `_note_attempt` / `_record_success` / `_record_failure` / `_backoff_seconds` / `_retry_after_seconds` ([api.py:607-667](../../src/concord/api.py)). `Client.__init__` owns the `httpx.Client` ([api.py:111-130](../../src/concord/api.py)).
- `src/concord/observability.py` — `Recorder.note_success` / `note_request_outcome` ([observability.py:180-210](../../src/concord/observability.py)); `active_recorder()`. The fetch module imports these.
- `src/concord/models/runs.py` — `Attempt` ([models/runs.py:21-35](../../src/concord/models/runs.py)). The fetch module builds these.
- `src/concord/text.py` — imports `HTTP_FORBIDDEN`, `HTTP_SERVER_ERROR_MIN/MAX`, `HTTP_TOO_MANY_REQUESTS` from `concord.api` ([text.py:62-67](../../src/concord/text.py)). Must keep working this PR (see "Constant compatibility").
- `tests/test_api.py` — functional suite (URL building, pagination, model parsing). Stays; should keep passing untouched.
- `tests/test_api_recording.py` — Run Event assertions via a manually-installed `Recorder` contextvar and `httpx.MockTransport` handlers ([tests/test_api_recording.py](../../tests/test_api_recording.py)). Most of these migrate to a new `tests/test_fetch.py` at the module interface.
- `tests/test_import_cycles.py` — guards the dependency graph; must still pass.

## Approach

### The deep module: network-only

`concord.fetch` exposes one small object, `Fetcher`, that wraps a caller-supplied `httpx.Client` and runs the resilience loop:

```python
class Fetcher:
    def __init__(
        self,
        client: httpx.Client,
        *,
        source: str,                      # Recorder bucket prefix: "api" / "text" / "senate"
        policy: RateLimitPolicy | None = None,   # None => no-op base policy
        sleep: Callable[[float], None] = time.sleep,
        max_transient_retries: int = MAX_5XX_RETRIES,
    ) -> None: ...

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> bytes:
        """Fetch `path`, returning the raw 2xx response body. Raises FetchError
        on a network failure (exhausted transient retries, terminal non-success,
        or a policy-rejected 2xx sentinel). Never inspects the body otherwise."""
```

`get` returns **raw bytes** on success and records the network success/Run Event. It does **no** JSON parsing, content-type validation, or shape checking — that is the caller's job (and, for the wire-shape entities, Stage 1's). On failure it raises `fetch.FetchError(message, *, status_code)`; each client translates that into its own public exception (`ApiError` here) so callers' `except` clauses are unchanged.

### The policy seam (one axis of variation)

The only genuine inter-client difference is rate-limit handling. It is an injected **policy** with three hooks, owned by the `Fetcher` for the client's lifetime (the throttle state in PR 2 is per-run, single-threaded — the scrapers do no threading, consistent with ADR 0007):

```python
class RateLimitPolicy:           # base class = all no-ops; reproduces "no rate-limit handling"
    def before_request(self) -> None: ...                 # default: pass  (PR2 text: pace())
    def classify(self, response: httpx.Response) -> Decision: ...
        # default: Decision(PASS). May SLEEP for a throttle response (the
        # policy owns its cooldown schedule + state) and return Decision(THROTTLE).
    def on_success(self) -> None: ...                     # default: pass  (PR2 text: recover())
```

`Disposition` is a small enum: `PASS` (let the spine judge by status code: 2xx ⇒ success, 5xx ⇒ transient retry, other ⇒ terminal), `THROTTLE` (the policy already slept; retry **without** burning the transient budget), `REJECT` (treat this response — typically a 2xx — as a terminal network failure; this is PR 3's **not-found sentinel**). `classify` returns a `Decision(disposition, message=None)` dataclass rather than a bare enum so a policy can supply the attempt message the spine records for a `THROTTLE`/`REJECT` response (PR 3 uses this to keep the HTML-trap marker legible in the ledger); `message=None` falls back to `response.reason_phrase`.

The spine loop, in one place:

```
transient = 0
while True:
    policy.before_request()
    try:
        resp = client.get(path, params=params)
    except httpx.HTTPError as exc:
        note transport attempt
        if transient >= max_transient_retries: record_failure; raise FetchError
        sleep(backoff(transient)); transient += 1; continue
    decision = policy.classify(resp)      # classify may sleep + escalate on THROTTLE
    match decision.disposition:
        case THROTTLE: note attempt(status, decision.message); continue   # no transient increment
        case REJECT:   note attempt(status, decision.message); record_failure; raise FetchError(not found)
        case PASS:
            status = resp.status_code
            if 5xx:        note attempt; if transient>=cap: record_failure; raise; else sleep(backoff(transient)); transient += 1; continue
            if not 2xx:    note attempt; record_failure; raise FetchError(terminal)
            record_success; return resp.content
```

The **retry-after** adapter for `api` (this PR): `before_request`/`on_success` are the base no-ops; `classify` returns `THROTTLE` for HTTP 429 (sleeping `Retry-After` seconds if present, else exponential backoff capped at `MAX_BACKOFF`) and `PASS` otherwise — so 5xx still flow through the shared transient path and a 403 (bad api.data.gov key) stays terminal, exactly as today.

### Content stays out (why `api._get` keeps a JSON/dict check)

`api._get` must still return a `dict` because it navigates the **pagination envelope** (`pagination.count` / `.next`). That JSON-parse-and-is-dict guard is a transport/protocol concern, *not* domain content validation — so it stays in `api._get`, layered **above** `Fetcher.get`. Domain content validation remains untouched at Stage 1 (`BillDetail.from_congress_api`, etc.).

### Owned behaviour changes (api)

This PR is behaviour-preserving for `api.congress.gov` except for two deliberate, negligible changes — call them out in the PR description:

1. **Malformed-2xx envelope.** Today a 2xx whose JSON is valid-but-not-a-dict is recorded as a *failed* Run Event and not counted ([api.py:588-595](../../src/concord/api.py)); a 2xx that isn't JSON at all raises uncounted. After: any 2xx counts as a network success (it *was* a successful fetch), and a malformed envelope raises `ApiError` from `api._get` **without** a separate failure event. This keeps the success count meaning exactly "the server returned a 2xx." api.congress.gov returning a non-object 2xx is a contract violation that essentially never happens; surfacing it as an exception is sufficient. (Listed under Open questions in case the team prefers the old semantics.)
2. **429 backoff schedule.** Today, when a 429 carries no `Retry-After`, the fallback delay reads the *transient* counter — which is never incremented on 429 — so it is effectively a flat `~1 s` ([api.py:549](../../src/concord/api.py)). The retry-after policy owns its own 429 schedule (Retry-After, else exponential capped at `MAX_BACKOFF`), removing reliance on the transient counter. This matches what `api.py`'s module docstring already *claims* the behaviour is; only an internal quirk changes.

### Constant compatibility

`text.py` imports `HTTP_FORBIDDEN`, `HTTP_SERVER_ERROR_MIN`, `HTTP_SERVER_ERROR_MAX`, `HTTP_TOO_MANY_REQUESTS` from `concord.api` ([text.py:62-67](../../src/concord/text.py)). To avoid touching `text.py` this PR, define the canonical constants in `concord.fetch` and **re-export** them from `concord.api` (keep the names bound in `api.py`, e.g. `from concord.fetch import HTTP_FORBIDDEN, ...`). PR 2 repoints `text.py` to import from `concord.fetch` directly and PR 1's re-export can then be dropped. Confirm no import cycle: `fetch → observability → models.runs`; `api → fetch`; `text → api` (unchanged this PR). `tests/test_import_cycles.py` is the guard.

### Module dependency category

`httpx` is the one true-external dependency, already substitutable via `httpx.MockTransport` (the clients accept a `transport=`). So the deep module is tested by handing it a `Fetcher` over a `MockTransport` — no new seam, the test surface *is* the module interface.

## Step-by-step plan

1. **Create `src/concord/fetch.py` with constants + helpers.** Add module docstring (cross-reference ADR 0021, ADR 0007, ADR 0027). Define `MAX_BACKOFF = 60.0`, `MAX_5XX_RETRIES = 5`, `_BACKOFF_BASE = 2.0`, and the `HTTP_*` status constants (`HTTP_FORBIDDEN`, `HTTP_TOO_MANY_REQUESTS`, `HTTP_SERVER_ERROR_MIN`, `HTTP_SERVER_ERROR_MAX`). Move `_note_attempt`, `_backoff_seconds`, `_retry_after_seconds` here (copied verbatim from `api.py:607-667`, dropping the per-client `path` flavour text). Add `_record_success(rec, source, path, attempts)` / `_record_failure(rec, source, path, attempts)` taking `source` as a parameter (generalising api's hardcoded `"api"`). Verify `uv run python -c "import concord.fetch"` succeeds.

2. **Define `FetchError`, `Disposition`, `Decision`, and `RateLimitPolicy` base in `fetch.py`.** `FetchError(Exception)` with `status_code: int | None` (mirrors `ApiError`'s shape, [api.py:87-96](../../src/concord/api.py)). `Disposition` as an `enum.Enum` with `PASS` / `THROTTLE` / `REJECT`. `Decision` as a frozen dataclass `(disposition: Disposition, message: str | None = None)`. `RateLimitPolicy` base class with the three no-op hooks (`before_request`, `classify` returning `Decision(Disposition.PASS)`, `on_success`). Type-hint everything (mypy strict on `src/`).

3. **Implement `Fetcher` in `fetch.py`.** Constructor per the "Approach" sketch (wraps a caller-supplied `httpx.Client`; `source`, `policy`, `sleep`, `max_transient_retries` injectable). Implement `get(path, *, params=None) -> bytes` as the spine loop from "Approach". `rec = active_recorder()` at entry; record success/failure via the `source`-parameterised helpers. Run `uv run mypy src`.

4. **Implement the retry-after policy in `fetch.py`.** Add `RetryAfterPolicy(RateLimitPolicy)`: `classify` returns `Decision(Disposition.THROTTLE)` for status 429 — sleeping `_retry_after_seconds(response)` if present else `_backoff_seconds` on its own consecutive-429 counter (capped at `MAX_BACKOFF`) — and `Decision(Disposition.PASS)` otherwise. It needs the injected `sleep`; pass it in via the policy constructor. (Design note: keep the 429 schedule self-contained in the policy, per "Owned behaviour change 2".)

5. **Write `tests/test_fetch.py`.** Port the network-resilience assertions from `tests/test_api_recording.py` to the module interface, driving a `Fetcher` over an `httpx.MockTransport`. Cover: clean 2xx ⇒ success bucket bumped, no event; 503-then-200 ⇒ one `resolved` event; terminal 503 (after `MAX_5XX_RETRIES`) ⇒ one `failed` event + `FetchError`; transport-error-then-200 ⇒ resolved event carrying `transport_class`; non-retryable 404 ⇒ `failed` event + `FetchError(status_code=404)`; no recorder ⇒ no-op. Add retry-after-specific tests: 429-then-200 with the `RetryAfterPolicy` ⇒ resolved event with a 429 attempt, and that 429 does not consume the transient budget (e.g. 429×N then 200 still succeeds with N>MAX_5XX_RETRIES). Use an injected `sleep` capturing delays to assert `Retry-After` is honoured.

6. **Migrate `concord.api` onto `Fetcher`.** In `Client.__init__`, after building `self._client`, construct `self._fetch = Fetcher(self._client, source="api", policy=RetryAfterPolicy(sleep=self._sleep), sleep=self._sleep)`. Rewrite `Client._get` ([api.py:508-597](../../src/concord/api.py)) to: build `merged` params, call `body = self._fetch.get(path, params=merged)` inside `try/except FetchError as exc: raise ApiError(str(exc), status_code=exc.status_code) from exc`, then `data = json.loads(body)`, check `isinstance(data, dict)` (raise `ApiError` if not — no separate event, per "Owned behaviour change 1"), and return. Delete `api.py`'s now-unused loop body and the moved helpers/constants. **Re-export** the `HTTP_*` constants in `api.py` (`from concord.fetch import HTTP_FORBIDDEN, HTTP_SERVER_ERROR_MIN, HTTP_SERVER_ERROR_MAX, HTTP_TOO_MANY_REQUESTS`) so `text.py` keeps importing them. Keep `MAX_BACKOFF` / `MAX_5XX_RETRIES` / `_BACKOFF_BASE` re-exported too if any test references them.

7. **Write ADR 0027.** Create `docs/adr/0027-single-network-only-fetch-seam.md` following the format of a recent ADR (e.g. [0026](../adr/0026-sync-command-not-resident-daemon.md)). Record: the three clients shared one resilience spine; it is now one network-only `concord.fetch` module with a rate-limit **policy** seam; content validation stays at Stage 1 (ADR 0018); the module is a thin helper, not a base class (ADR 0007); the **network failure vs structural failure vs Load Validation Failure** distinction. Note PRs 2/3 add the other two policy adapters. List it in the ADR index if one exists.

8. **Trim `tests/test_api_recording.py`.** Delete the assertions now covered by `tests/test_fetch.py` (the generic spine + 429 behaviour). Keep only anything genuinely api-specific (e.g. that `Client.list_*` routes through the fetcher and surfaces `ApiError`); if nothing remains api-specific, delete the file and note it in the PR description. `tests/test_api.py` (functional) should pass untouched.

9. **Run the full gate.** `uv run ruff format`, `uv run ruff check`, `uv run mypy src`, `uv run pytest`. Pay attention to `tests/test_import_cycles.py` and the smoke test.

## Demo seed data

No seed changes. This is a pure refactor of network plumbing — no new tables, columns, entity types, or persisted state. `backend/demo/seed.sql` is untouched.

## Testing strategy

- **New unit tests — `tests/test_fetch.py`** (module interface, over `httpx.MockTransport`): the generic spine (clean / resolved-on-retry / terminal / transport-class / non-retryable-4xx / no-recorder) plus retry-after policy behaviour (429 resolved event; 429 doesn't burn the transient budget; `Retry-After` honoured). This is the new home for the resilience contract.
- **Trimmed — `tests/test_api_recording.py`**: only api-specific routing left (or deleted).
- **Unchanged — `tests/test_api.py`**: the functional suite (URL building, pagination across pages, model parsing) must pass with no edits. This is the regression guard that the migration preserved api's externally-visible behaviour.
- **Regression risk:** `tests/test_text.py`, `tests/test_text_recording.py`, `tests/test_senate_xml*.py` must continue to pass *unchanged* — proof that PR 1 didn't disturb the not-yet-migrated clients. `tests/test_import_cycles.py` guards the new dependency edge.
- **Manual check (optional):** with a real `CONGRESS_API_KEY`, `uv run concord scrape members --congresses 119 --limit 5` and confirm the Scrape Run summary still reports success counts and any Run Events sensibly.

## Acceptance criteria

- [ ] `src/concord/fetch.py` exists with `Fetcher`, `RateLimitPolicy`, `RetryAfterPolicy`, `Disposition`, `FetchError`, and the moved constants/helpers.
- [ ] `concord.api.Client._get` calls `Fetcher.get` and contains no retry loop of its own; `ApiError` is still the public exception.
- [ ] `text.py` and `senate_xml.py` are unmodified and their tests pass (the `HTTP_*` re-export keeps `text.py` importing from `concord.api`).
- [ ] `docs/adr/0027-single-network-only-fetch-seam.md` exists and records the decision.
- [ ] `tests/test_fetch.py` covers the spine + retry-after behaviour; redundant assertions removed from `tests/test_api_recording.py`.
- [ ] Two owned behaviour changes (malformed-2xx counting; 429 schedule) are noted in the PR description.
- [ ] `uv run ruff check` and `uv run ruff format --check` clean.
- [ ] `uv run mypy src` clean.
- [ ] `uv run pytest` green, including `tests/test_import_cycles.py`.

## Open questions

- **Q: Keep the old "valid-JSON-but-not-a-dict 2xx ⇒ failed Run Event, not counted" semantics, or adopt the clean "any 2xx counts; malformed envelope raises without an event"?** Decided default: the clean version (see "Owned behaviour change 1"). It is a near-impossible case for api.congress.gov and keeps the success-count meaning honest. If a reviewer objects, the fallback is for `api._get` to call a `fetch`-provided "record structural failure" helper — but that reintroduces the muddle we removed. Flag, don't silently switch.
- **Q: Exact public name of the module/type (`fetch.Fetcher`)?** Proposed `concord.fetch` + `Fetcher`. Per ADR 0014 the *CLI* is the stable contract and Python imports are best-effort, so this is the implementer's call — but PRs 2 and 3 reference these names, so if you rename, update those plans.

## Out-of-band work

- PRs 2 ([resilient-fetch-text.md](resilient-fetch-text.md)) and 3 ([resilient-fetch-senate.md](resilient-fetch-senate.md)) **block on this PR merging.** They add the `AdaptiveThrottlePolicy` and the senate default-policy + not-found-sentinel adapters respectively, and delete each client's copied constants/helpers. They are independent of each other.
- After all three land, the `HTTP_*` re-export added to `api.py` in Step 6 becomes dead (PR 2 repoints `text.py`); PR 2 should remove it.
