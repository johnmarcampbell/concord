# Rate limiting for the bill-enrichment endpoint

> Add per-IP rate limiting to the `POST /bills/{c}/{t}/{n}/enrichment` route designed in [bill-enrichment-button.md](./bill-enrichment-button.md), introduce a centralized pydantic-settings config module so the growing set of operator env vars has a single home, make per-IP limits actually work behind the Traefik reverse proxy of the standard Docker deployment, and replace the default JSON 429 handler with one that returns HTMX-friendly HTML when the client opts in.

## Source

Conversation context, 2026-05-27 — paired with [bill-enrichment-button.md](./bill-enrichment-button.md) ([concord#78](https://github.com/johnmarcampbell/concord/issues/78)) which deferred rate limiting and named the chokepoint. The grilling session for this plan walked seven branches (deployment topology, per-IP rate string, global cap, status-poll handling, 429 response shape, ADR-worthiness, multi-process future-proofing); the resolved decisions live in [Approach](#approach).

## Context

The enrichment plan adds `POST /bills/{c}/{t}/{n}/enrichment` which runs `scrape_enrichment` → `load_bills.load_one` → `index_bills.reindex_one` for one bill in a `BackgroundTasks` job. Each click costs **5 sub-endpoint calls to api.congress.gov, often more after pagination** — cosponsors and actions paginate at 20/page, subjects paginates too — so a heavily-cosponsored bill (e.g. ~200 cosponsors, ~50 actions) can be 20–30+ API calls per click. The `CONGRESS_API_KEY` budget is **5,000 requests/hour per registered key**, a hard ceiling rather than a money-buys-more relationship. ~500 enrichment clicks/hr (or fewer if bills are heavy) exhaust the entire budget — and the budget is shared with any other use of the same key, including the `concord scrape` CLI an operator might run overnight.

The web app is anonymous (no users, no sessions). Threat model is **anonymous-internet abuse**: curious visitor (1–5 clicks total), adversarial visitor walking the `/bills` index (50 bills/page × ~15 calls = drains the quota in a few page-walks), scripted scraper from one IP, and distributed multi-IP campaigns.

`slowapi` is already wired up — `Limiter(key_func=get_remote_address)` at [src/concord/web/app.py:125](src/concord/web/app.py:125), and `@limiter.limit("30/minute")` decorates `/search` at line 177. This plan does not introduce a new rate-limit library; it extends the existing one. But the existing setup has two latent problems this plan also fixes:

1. **`get_remote_address` reads the socket peer**, which behind any reverse proxy is the proxy's IP. Every visitor shares one bucket and per-IP limiting silently collapses to global. The standard Concord deployment per [docs/adr/0012-web-bootstraps-empty-schema-on-startup.md](../adr/0012-web-bootstraps-empty-schema-on-startup.md) is a Docker container on a Hostinger VPS with a Traefik container on the same VPS doing the proxying — so the deployment **does** sit behind a proxy and per-IP is broken today.
2. **`slowapi`'s default `_rate_limit_exceeded_handler` returns JSON.** The enrichment button POST has `hx-swap="outerHTML"` targeting `#enrichment-status`; a JSON 429 lands as a string in that div. Visually broken.

A third item this plan addresses is **env-var sprawl**. The enrichment plan adds `CONCORD_ENABLE_WEB_ENRICHMENT` on top of `CONGRESS_API_KEY`; this plan adds `CONCORD_TRUST_PROXY_HEADERS`, `CONCORD_ENRICHMENT_RATE_LIMIT`, and `CONCORD_SEARCH_RATE_LIMIT`. Six env vars read from scattered places is one too many. A small `Settings` class via `pydantic-settings` makes the operator-facing surface self-documenting and typed.

Domain terms used here are defined in [CONTEXT.md](../../CONTEXT.md): Bill, Bill ID.

## Goals

1. Add per-IP rate limiting to `POST /bills/{c}/{t}/{n}/enrichment` at the composite rate `1/minute;3/hour;20/day` (`slowapi`'s semicolon-separated multi-limit syntax). Default in `Settings`; overridable via env var `CONCORD_ENRICHMENT_RATE_LIMIT`.
2. Introduce `src/concord/config.py` containing a single pydantic-settings `Settings` class with six fields covering every operator-facing knob. Read once at `create_app()` boot, stashed on `app.state.settings`.
3. Make per-IP limiting work behind a reverse proxy: a new `CONCORD_TRUST_PROXY_HEADERS` env var (default off). When set, `create_app()` registers `uvicorn.middleware.proxy_headers.ProxyHeadersMiddleware` with `trusted_hosts="*"`. `get_remote_address` then sees the real client IP rather than the proxy.
4. Replace `slowapi`'s default `_rate_limit_exceeded_handler` with a custom handler that branches on `HX-Request: true` — HTMX requests get an HTML fragment (`web/templates/_rate_limited.html`), other requests get the existing JSON shape. Both set `Retry-After` and log a WARN line.
5. Migrate the existing `SEARCH_RATE_LIMIT` module constant ([src/concord/web/app.py:49](src/concord/web/app.py:49)) to `settings.search_rate_limit`, env-overridable via `CONCORD_SEARCH_RATE_LIMIT`. No behaviour change at the default value.
6. Land [ADR 0017 — Rate-limit posture for the public web demo](../adr/0017-rate-limit-posture.md) (new). Records the seven locked decisions, the rejected alternatives (global static cap; status-poll limit), and the multi-process revisit trigger in a "what stays open" paragraph.
7. Amend [bill-enrichment-button.md](./bill-enrichment-button.md) Step 5 to consume `Settings` instead of doing inline `os.environ.get(...)` reads. (Done at plan-write time; documented under [Out-of-band work](#out-of-band-work).)

## Non-goals

1. **Global static cap across all IPs.** Considered and rejected during grilling. Defense stack is per-IP + the [`concord.api.Client`](../../src/concord/api.py) upstream-429 backoff that already exists ([src/concord/api.py:526-540](src/concord/api.py:526)) + the `CONCORD_ENABLE_WEB_ENRICHMENT` kill switch + a WARN log line on every 429. If a real distributed attack ever materializes, observability + a circuit-breaker is the better answer than a static cap; defer.
2. **Rate limiting `GET /bills/.../enrichment-status`.** The status-poll endpoint fires 20× per minute per in-flight job; it's a cheap SELECT with no external API calls; the POST limit indirectly caps how many polls are legitimately reachable. Limiting it would either fire on legitimate use (if tight) or be redundant (if generous). Don't.
3. **Refactoring `concord.api.Client` to read `Settings().congress_api_key`.** `Client` currently reads `os.environ.get("CONGRESS_API_KEY")` lazily in its `__init__` and that's fine. Migration is a small, independent follow-up; not part of this plan.
4. **Distributed (Redis-backed) rate limiting.** `slowapi`'s default in-memory storage is correct for single-process deployment. Multi-process correctness is the same revisit trigger as the in-process in-flight set in [ADR 0016](../adr/0016-web-initiated-enrichment.md) (not yet written; drafted by the enrichment plan). When `concord serve` ever grows a `--workers N > 1` flag, the storage backend swaps to `Limiter(storage_uri="redis://…")`. Documented in ADR 0017's "what stays open" section.
5. **Auth / per-user rate limits.** The app is anonymous. This is purely per-IP.
6. **A `Retry-After` value computed from `slowapi`'s internal bucket state.** `slowapi` doesn't expose seconds-until-reset on `RateLimitExceeded` via a public API. We use the *window size* of the violated limit instead — pessimistic (says "wait 60s" when actually only 23s remain) but correct in spirit and resilient to `slowapi` internals.

## Relevant prior decisions

- [bill-enrichment-button.md](./bill-enrichment-button.md). This plan extends that plan's `POST /bills/{c}/{t}/{n}/enrichment` route with a `@limiter.limit(...)` decorator, and amends Step 5 of that plan to consume `Settings`. Merge order: this plan's Settings module must exist before the enrichment plan's Step 5 lands. See [Out-of-band work](#out-of-band-work).
- [ADR 0001 — Python end-to-end for the web layer](../adr/0001-python-end-to-end-for-the-web-layer.md). The reason `slowapi` (in-process Python) rather than a sidecar limiter is the right shape.
- [ADR 0012 — Web layer bootstraps an empty schema on startup](../adr/0012-web-bootstraps-empty-schema-on-startup.md). References the upcoming Dockerfile that this plan's `CONCORD_TRUST_PROXY_HEADERS` env var documents alongside.
- [ADR 0016 — Web layer may invoke Stage 0 enrichment on demand](../adr/0016-web-initiated-enrichment.md) (drafted by the enrichment plan; not yet on disk). ADR 0017 cross-references its "what stays open" multi-worker note.
- **ADR 0017 — Rate-limit posture for the public web demo** (new, drafted as a step of this plan). See [Step 9](#step-by-step-plan).

## Relevant files and code

- [`src/concord/web/app.py:35`](../../src/concord/web/app.py) — `slowapi` import line. The custom handler will replace the imported `_rate_limit_exceeded_handler`.
- [`src/concord/web/app.py:48-49`](../../src/concord/web/app.py) — `SEARCH_RATE_LIMIT = "30/minute"` module constant. Migrated to `settings.search_rate_limit`.
- [`src/concord/web/app.py:98-146`](../../src/concord/web/app.py) — `create_app(db_path, *, embedder=None)`. Settings construction, proxy-header middleware registration, and custom 429 handler registration all land here.
- [`src/concord/web/app.py:125`](../../src/concord/web/app.py) — `limiter = Limiter(key_func=get_remote_address)`. Unchanged in this plan — `get_remote_address` keeps reading `request.client.host`, but `ProxyHeadersMiddleware` (when enabled) rewrites that to the real client IP from `X-Forwarded-For` before any handler runs.
- [`src/concord/web/app.py:136`](../../src/concord/web/app.py) — `app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)`. Replaced with the local custom handler.
- [`src/concord/web/app.py:176-177`](../../src/concord/web/app.py) — `@limiter.limit(SEARCH_RATE_LIMIT)` on `/search`. Updated to `@limiter.limit(settings.search_rate_limit)`.
- [`pyproject.toml`](../../pyproject.toml) — add `pydantic-settings` dependency. `pydantic` is already present (used by `concord.models`).
- [`tests/test_web_routes.py:230-247`](../../tests/test_web_routes.py) — existing 429 test class. Pattern: `TestClient(app, raise_server_exceptions=False)` + hammer the route + assert `r.status_code == 429`. New 429 tests for the enrichment route follow the same shape.
- [`CLAUDE.md`](../../CLAUDE.md) — "API keys" section ([CLAUDE.md:39-44](../../CLAUDE.md:39)). New env vars get one line each.
- (Created by enrichment plan, modified here) [`src/concord/web/app.py`](../../src/concord/web/app.py) — the `@app.post(".../enrichment")` route from enrichment-plan Step 6. This plan adds `@limiter.limit(settings.enrichment_rate_limit)` between `@app.post(...)` and the handler.

## Approach

### Centralized config via pydantic-settings

The growing operator-facing surface (`CONGRESS_API_KEY`, `OPENAI_API_KEY`, `CONCORD_ENABLE_WEB_ENRICHMENT`, `CONCORD_TRUST_PROXY_HEADERS`, `CONCORD_ENRICHMENT_RATE_LIMIT`, `CONCORD_SEARCH_RATE_LIMIT`) is consolidated into `src/concord/config.py`:

```python
"""Operator-facing configuration. Each field's name (uppercased) is the
env var that overrides it.

Construct a fresh ``Settings()`` at each consumption boundary (typically
once at app boot inside ``create_app``). Tests monkeypatch ``os.environ``
and then construct a new ``Settings`` to see the override — no
module-level cache to clear.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,            # we don't auto-load .env; see CLAUDE.md
        case_sensitive=False,     # CONGRESS_API_KEY → congress_api_key
        extra="ignore",
    )

    # API keys — no default; presence-checked at the use site.
    congress_api_key: str | None = None        # env: CONGRESS_API_KEY
    openai_api_key: str | None = None          # env: OPENAI_API_KEY

    # Web behaviour.
    enable_web_enrichment: bool = False        # env: CONCORD_ENABLE_WEB_ENRICHMENT
    trust_proxy_headers: bool = False          # env: CONCORD_TRUST_PROXY_HEADERS

    # Rate limits — slowapi rate strings.
    search_rate_limit: str = "30/minute"                       # env: CONCORD_SEARCH_RATE_LIMIT
    enrichment_rate_limit: str = "1/minute;3/hour;20/day"      # env: CONCORD_ENRICHMENT_RATE_LIMIT
```

Functions-vs-class was considered. Class wins because: (a) `Settings` is *the* answer to "what knobs does this app have?" — discoverable in one place; (b) bool/string parsing and validation are free; (c) a malformed value (`CONCORD_ENABLE_WEB_ENRICHMENT=banana`) fails loud at boot rather than silently defaulting to False; (d) `pydantic-settings` is small (~25 KB) and only depends on `pydantic`, which is already in the project.

The one cost: env-var → field mapping is implicit (uppercased field name). `grep -rn "CONCORD_TRUST_PROXY_HEADERS"` won't find the field declaration. The one-line comment per field (above) makes the contract grep-able.

### `Settings()` lifecycle

Construct once at the top of `create_app()`, stash on `app.state.settings`. Routes read it via `request.app.state.settings`. The decorator-applied rate strings (`@limiter.limit(settings.enrichment_rate_limit)`) are captured at *route registration time* (inside `_register_routes`, called from `create_app`) — so they always reflect the boot-time value. A re-tune of `CONCORD_ENRICHMENT_RATE_LIMIT` needs a process restart, which matches operator expectations for env-var-driven config.

### Proxy-header trust

`uvicorn.middleware.proxy_headers.ProxyHeadersMiddleware` ships with uvicorn — no new dep. When `settings.trust_proxy_headers` is True, register it with `trusted_hosts="*"`:

```python
if settings.trust_proxy_headers:
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")
```

The middleware rewrites `request.scope["client"]` from `X-Forwarded-For` *before* any handler (including `slowapi`'s `get_remote_address`) runs. So `get_remote_address` keeps working unchanged — it just sees the real client IP instead of the proxy.

`trusted_hosts="*"` is correct for the standard deployment shape (Docker container, Traefik on the same VPS — the container only receives traffic from Traefik, so trusting `X-Forwarded-For` from any upstream is fine). The env var **is** the operator promise: "I have an actual trusted proxy in front of me." A separate `CONCORD_TRUSTED_PROXIES` env var with CIDRs was considered and rejected as over-engineering for a single-tenant demo.

Defaulting the env var off (rather than on) means `concord serve` locally without a proxy keeps the spoof-proof socket-peer behavior. The Dockerfile follow-up (per [ADR 0012](../adr/0012-web-bootstraps-empty-schema-on-startup.md)) will document `CONCORD_TRUST_PROXY_HEADERS=1` as a recommended setting for the standard Traefik deployment.

### Rate-string shape — why `1/minute;3/hour;20/day`

The asymmetry with `/search`'s `30/minute` is intentional. `/search` costs one OpenAI embed call per request — your money, no hard external ceiling. Enrichment costs 5–30+ api.congress.gov calls — a hard 5,000/hr ceiling shared across all uses of the key. Enrichment must be tighter by ~an order of magnitude.

Composite shape rationale:
- **`1/minute`** is cheap insurance against accidental double-clicks (browser back/forward, form resubmission) and frantic retry storms. Legitimate users don't enrich more than one bill per minute.
- **`3/hour`** maps to "engaged researcher": clicking enrichment on three specific bills in an hour while writing about a topic. More than that and the CLI (`concord scrape bills enrich --bill-ids …`) is the right tool.
- **`20/day`** catches slow walkers (one click every ~5 minutes from one IP — beats the per-hour limit but still drains over a day). 20 × ~15 calls/click ≈ 300 calls/day per IP; even with a dozen committed adversarial IPs that's ~3,600/day, comfortably under the 120,000/day theoretical budget (5,000/hr × 24).

`slowapi` accepts the semicolon-separated form natively: `@limiter.limit("1/minute;3/hour;20/day")` is equivalent to stacking three decorators.

### No global cap; rely on upstream backoff

A global limit (e.g. `key_func=lambda *_: "global"` at `100/hour`) would catch distributed abuse but at the cost of returning 429 to legitimate new visitors during popularity moments. The failure mode without a global cap is well-tolerated: [`concord.api.Client`](../../src/concord/api.py) already retries upstream 429s indefinitely with `Retry-After`-respecting backoff ([src/concord/api.py:526-540](src/concord/api.py:526)), so a drained quota slows down enrichment rather than corrupting it. The user sees "Enriching…" for longer; nothing breaks.

The operator-visible signal that something is amiss: every web-side 429 emits a WARN log line. `grep "rate limit exceeded"` from container logs shows the rate of legitimate vs adversarial pressure.

### No limit on the status-poll endpoint

`GET /bills/.../enrichment-status` is cheap (one indexed SELECT, no external calls) and indirectly capped: you can only legitimately poll for bills you've POSTed, and the POST limit caps that. An attacker hammering the status endpoint achieves nothing (no expensive work triggered, no privileged info leaked). Limiting it would either be redundant or fire on legitimate use.

### HTMX-aware 429 response

Replace `slowapi`'s `_rate_limit_exceeded_handler` with a local one:

```python
def _rate_limit_exceeded(request: Request, exc: RateLimitExceeded) -> Response:
    retry_after_s = _window_seconds(exc)        # see below
    _log.warning(
        "rate limit exceeded: ip=%s path=%s limit=%s",
        get_remote_address(request),
        request.url.path,
        exc.detail,
    )
    headers = {"Retry-After": str(retry_after_s)}
    if request.headers.get("HX-Request") == "true":
        html = templates.get_template("_rate_limited.html").render(
            {"retry_after_s": retry_after_s}
        )
        return HTMLResponse(html, status_code=429, headers=headers)
    return JSONResponse(
        {"error": "rate_limit_exceeded", "retry_after": retry_after_s},
        status_code=429,
        headers=headers,
    )
```

`_window_seconds(exc)` parses `exc.detail` (a string like `"1 per 1 minute"`) and returns the window size (60 / 3600 / 86400). `slowapi`'s `RateLimitExceeded` doesn't expose seconds-until-reset on a public API; window-size is pessimistic but correct-in-spirit and resilient to `slowapi` internals.

The HTML fragment is **generic — no `id` attribute, no "Try again" button**:

```html
<div class="border border-amber-300 bg-amber-50 text-amber-800 rounded p-3 text-sm">
  ⚠ Too many requests. Try again in {{ retry_after_s }}s.
</div>
```

This lets it swap into any HTMX target (the `#enrichment-status` div for the enrichment POST, the search results container for `/search` if it ever 429s an HTMX-paginating client). The user retries via a page reload — clunkier than auto-recovery via `hx-trigger="load delay:60s"`, but the wiring for auto-recovery would couple this fragment to the enrichment polling state machine in a fragile way. For an exceptional UX state (rate-limited), a page reload is fine.

### ADR 0017 — what it records

The seven locked decisions plus the rejected alternatives. The multi-process correctness note lives inside ADR 0017's "what stays open" section rather than getting its own ADR — `slowapi`'s in-memory storage is one paragraph of context, not a standalone decision.

## Step-by-step plan

1. **Add `pydantic-settings` dependency.** Edit [`pyproject.toml`](../../pyproject.toml) `[project.dependencies]` to add `"pydantic-settings>=2.0"`. Run `uv sync` to update the lockfile. **Verify:** `uv run python -c "from pydantic_settings import BaseSettings"` succeeds.

2. **Create `src/concord/config.py`.** Add the `Settings` class shown in [Approach > Centralized config via pydantic-settings](#centralized-config-via-pydantic-settings) — six fields, one-line comment per field naming the env var, `model_config` set as shown. Export `__all__ = ["Settings"]`. **Verify:** `uv run python -c "from concord.config import Settings; s = Settings(); print(s.model_dump())"` prints all six fields with their defaults. `uv run mypy src` is clean.

3. **Write unit tests for `Settings`.** Add `tests/test_config.py` with cases using `pytest-monkeypatch`:
   - Default values match the source declarations when no env vars are set.
   - `CONCORD_ENABLE_WEB_ENRICHMENT=1` → True; `=true` → True; `=yes` → True; `=on` → True; `=0` → False; `=banana` → raises `ValidationError` at `Settings()` construction.
   - `CONCORD_ENRICHMENT_RATE_LIMIT=2/hour` → field reads the override.
   - `CONGRESS_API_KEY=test-key` → `settings.congress_api_key == "test-key"`.
   **Verify:** `uv run pytest tests/test_config.py -x` passes.

4. **Wire `Settings` into `create_app()`.** Edit [`src/concord/web/app.py:114-130`](../../src/concord/web/app.py:114) (the body of `create_app`):
   ```python
   from concord.config import Settings

   def create_app(db_path, *, embedder=None) -> FastAPI:
       db_path = Path(db_path)
       ensure_schema(db_path)
       settings = Settings()
       # ... existing embedder init ...
       limiter = Limiter(key_func=get_remote_address)
       app = FastAPI(...)
       app.state.db_path = db_path
       app.state.embedder = embedder
       app.state.limiter = limiter
       app.state.settings = settings
       # ...
   ```
   Remove the existing `SEARCH_RATE_LIMIT = "30/minute"` module constant at line 48-49 — it now lives in `Settings`. **Verify:** `uv run pytest tests/test_web_routes.py -x` passes (the existing test fixtures don't depend on `SEARCH_RATE_LIMIT` as a module symbol — confirm before deleting; if anything imports it, leave it as `SEARCH_RATE_LIMIT = Settings().search_rate_limit` for compat).

5. **Add proxy-header middleware conditionally.** In `create_app()` after the `app = FastAPI(...)` line and before `app.state.* = ...` assignments, add:
   ```python
   if settings.trust_proxy_headers:
       from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware  # noqa: PLC0415 - middleware gated on env var
       app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")
   ```
   The `# noqa: PLC0415` matches the project pattern for runtime-gated imports (see CLAUDE.md "Things that bite"). **Verify:** add `test_proxy_headers_middleware_registered_when_trust_enabled` to [`tests/test_web_routes.py`](../../tests/test_web_routes.py) — set `CONCORD_TRUST_PROXY_HEADERS=1` via monkeypatch, build the app, assert `ProxyHeadersMiddleware` is in `app.user_middleware`. Without the env var set, assert it isn't.

6. **Replace the default 429 handler.** In [`src/concord/web/app.py`](../../src/concord/web/app.py):
   - Remove the import of `_rate_limit_exceeded_handler` from `slowapi` (line 35).
   - Add a module-level function `def _rate_limit_exceeded(request: Request, exc: RateLimitExceeded) -> Response` per [Approach > HTMX-aware 429 response](#htmx-aware-429-response). The handler renders `_rate_limited.html` (created in Step 7) via `request.app.state.templates`.
   - Add `def _window_seconds(exc: RateLimitExceeded) -> int` that parses `exc.detail` (a string like `"1 per 1 minute"` or `"3 per 1 hour"`) and returns 60 / 3600 / 86400 / etc. The implementation can use a small `{"second": 1, "minute": 60, "hour": 3600, "day": 86400}` dict and `re.match(r"\d+ per \d+ (\w+)", exc.detail)`. Default to 60 if the parse fails.
   - Update line 136 to register the new handler: `app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded)`.
   **Verify:** `tests/test_web_routes.py::TestRateLimiting::test_search_endpoint_rate_limited` still passes (the existing test only checks status code, so format-of-body changes are fine).

7. **Add the `_rate_limited.html` template.** Create [`src/concord/web/templates/_rate_limited.html`](../../src/concord/web/templates/_rate_limited.html) with exactly the markup shown in [Approach](#htmx-aware-429-response). No `id` attribute, no button, generic amber alert with Tailwind classes matching the existing failure-banner styling in the bills templates. **Verify:** add `test_429_html_branch_returns_amber_alert` to [`tests/test_web_routes.py`](../../tests/test_web_routes.py) — hammer `/search` past the limit with `HX-Request: true` header, assert response is HTML (not JSON), assert it contains `"Too many requests"`, assert `Retry-After` header is set to `60`.

8. **Migrate `/search`'s rate string to settings.** Edit [`src/concord/web/app.py:177`](../../src/concord/web/app.py:177): change `@limiter.limit(SEARCH_RATE_LIMIT)` to `@limiter.limit(app.state.settings.search_rate_limit)`. This requires moving the decorator application inside `_register_routes` so it can access `app.state.settings`, which is already the structure since `_register_routes(app, limiter)` is called after `app.state.settings` is set. **Verify:** `tests/test_web_routes.py::TestRateLimiting::test_search_endpoint_rate_limited` still passes at the default rate; an additional test setting `CONCORD_SEARCH_RATE_LIMIT=2/minute` exercises the override path (hammer to 3 requests/min, assert third is 429).

9. **Add `@limiter.limit(...)` to the enrichment POST route.** This step *modifies code created by the enrichment plan* — it depends on [bill-enrichment-button.md](./bill-enrichment-button.md) Step 6 having landed. Inside `_register_routes` (per the enrichment plan), find the `@app.post("/bills/{congress}/{bill_type}/{bill_number}/enrichment", response_class=HTMLResponse)` decorator and add a `@limiter.limit(settings.enrichment_rate_limit)` decorator *below* it (slowapi-decorator goes inside FastAPI-decorator). The in-flight check inside the handler body stays unchanged — it's already designed to sit after any limiter decorator per the enrichment plan's chokepoint contract. **Verify:** add `test_enrichment_post_rate_limited` to [`tests/test_web_bills.py`](../../tests/test_web_bills.py) — set both `CONGRESS_API_KEY` and `CONCORD_ENABLE_WEB_ENRICHMENT=1`, POST the enrichment route twice in quick succession, assert the second response is 429 with `Retry-After` set. Verify the in-flight set was *not* mutated (the bill is not in `app.state.enrichment_in_flight` after the 429).

10. **Draft ADR 0017.** Create [`docs/adr/0017-rate-limit-posture.md`](../adr/0017-rate-limit-posture.md) using the existing ADR style (compare [ADR 0012](../adr/0012-web-bootstraps-empty-schema-on-startup.md)). Status: Accepted, dated at landing. Sections: Context → Decision → Consequences (trade-offs accepted / things this buys / what stays open) → Rejected: global static cap → Rejected: limit the status-poll endpoint. The "what stays open" section must include both (a) multi-process correctness (revisit when `--workers N > 1`; migration path is `Limiter(storage_uri="redis://…")`) and (b) the observability + circuit-breaker follow-up if distributed abuse becomes a real problem. **Verify:** `uv run ruff format --check` and `uv run ruff check` clean.

11. **Update CLAUDE.md.** Under the "API keys" section ([CLAUDE.md:39-44](../../CLAUDE.md:39)), add three lines documenting the new env vars:
    - `CONCORD_TRUST_PROXY_HEADERS=1` — set when running behind a reverse proxy (the standard Docker-on-VPS-with-Traefik deployment). Default off.
    - `CONCORD_ENRICHMENT_RATE_LIMIT` — overrides the default `"1/minute;3/hour;20/day"` rate applied to the bill-enrichment button. Format: `slowapi` rate string (semicolon-separated for composite limits).
    - `CONCORD_SEARCH_RATE_LIMIT` — overrides the default `"30/minute"` rate applied to `/search`.
    Also add a forward reference: "All operator-facing env vars are declared in [`src/concord/config.py`](src/concord/config.py); see the `Settings` class for the full set." **Verify:** read the rendered CLAUDE.md change in the PR diff.

12. **Amend [bill-enrichment-button.md](./bill-enrichment-button.md) Step 5 to consume `Settings`.** Replace the inline `os.environ.get(...)` reads:
    ```python
    # Before (enrichment plan as written):
    has_congress_api_key = bool(os.environ.get("CONGRESS_API_KEY"))
    enrich_flag = os.environ.get("CONCORD_ENABLE_WEB_ENRICHMENT", "").strip().lower()
    enrichment_enabled = has_congress_api_key and enrich_flag in {"1", "true", "yes"}
    ```
    with:
    ```python
    # After (this plan amends):
    settings = Settings()
    enrichment_enabled = bool(settings.congress_api_key) and settings.enable_web_enrichment
    ```
    Stash `settings` (and `enrichment_enabled` for convenience) on `app.state` exactly as the enrichment plan did. Also add a line to the enrichment plan's "Relevant prior decisions" listing this plan as the source of `concord.config.Settings`. This edit is **done at plan-write time** (i.e., as part of writing this plan, not as a runtime step) so the two plan files on disk are coherent for whichever executor reads them. See [Out-of-band work](#out-of-band-work) for the merge-order note.

13. **Manual end-to-end smoke.** With the enrichment plan landed and `proceedings.db` containing at least one tier-1-only bill:
    ```sh
    export CONGRESS_API_KEY=...                           # a real key
    export CONCORD_ENABLE_WEB_ENRICHMENT=1
    export CONCORD_ENRICHMENT_RATE_LIMIT="2/minute"       # tight, for easy testing
    uv run concord serve
    ```
    Visit `/bills/119/hr/1`. Click "Request enrichment" — confirm in-flight fragment. Click "Request enrichment" on another bill — confirm in-flight fragment. Click on a third bill within the same minute — confirm the response is an amber 429 fragment in `#enrichment-status` with "Try again in 60s." Confirm the third bill is *not* in `app.state.enrichment_in_flight` (inspect via a debug print or a `/healthz` extension; pragmatic — just check the container logs for the WARN line). Reload the page after 60s, click again — confirm the request goes through.

    Re-run with `CONCORD_TRUST_PROXY_HEADERS=1` *not* set behind Traefik — confirm all requests appear to come from one IP (Traefik's), confirming the per-IP collapse footgun is real. Then re-run with the env var set — confirm distinct client IPs are seen via the WARN log.

## Demo seed data

> No `backend/demo/seed.sql` exists in this repo — this is a Python project, not the JS-shape repo the template was written for. This plan adds zero persistent data (rate-limit state lives in-process in `slowapi`'s in-memory backend). No fixture changes needed beyond the test additions in Steps 3, 5, 7, 8, and 9.

## Testing strategy

**Unit tests (new):**
- `tests/test_config.py` — `Settings` field defaults, env-var overrides, bool parsing (including the malformed-value ValidationError case), case-insensitive env-var names.

**Integration tests (new, in `tests/test_web_routes.py`):**
- `test_proxy_headers_middleware_registered_when_trust_enabled` — env var set → middleware present; unset → middleware absent.
- `test_429_html_branch_returns_amber_alert` — hammer `/search` past limit with `HX-Request: true`, assert HTML response, content contains "Too many requests", `Retry-After: 60`.
- `test_429_json_branch_for_non_htmx` — hammer `/search` past limit without `HX-Request`, assert JSON response with `error: "rate_limit_exceeded"` and `Retry-After` header.
- `test_search_rate_limit_override` — `CONCORD_SEARCH_RATE_LIMIT=2/minute` → third request in a minute is 429.
- `test_rate_limit_warn_log` — assert WARN log line is emitted on a 429 (`caplog` fixture).

**Integration tests (new, in `tests/test_web_bills.py`):**
- `test_enrichment_post_rate_limited` — two POSTs in a minute, third returns 429; in-flight set unchanged.
- `test_enrichment_rate_limit_override` — `CONCORD_ENRICHMENT_RATE_LIMIT=10/hour` → looser, more requests allowed.

**Regression risk:**
- `tests/test_web_routes.py::TestRateLimiting::test_search_endpoint_rate_limited` must still pass — the rate-string source changed but the behavior at the default value didn't. Confirm before and after.
- `uv run pytest` full suite must pass.
- `uv run mypy src` must pass with strict mode — `pydantic-settings` has good mypy support; if any field-typing issue arises, prefer narrowing the field type over `# type: ignore`.
- `uv run ruff check` and `uv run ruff format --check` must pass.

**Manual checks:**
- The full Step 13 walkthrough.

## Acceptance criteria

- [ ] `src/concord/config.py` exists with a `Settings` class containing all six fields with documented env-var comments.
- [ ] `pydantic-settings` is a declared dependency in `pyproject.toml` and resolved in `uv.lock`.
- [ ] `create_app()` constructs `Settings()` once and stashes it on `app.state.settings`.
- [ ] `ProxyHeadersMiddleware` is registered iff `CONCORD_TRUST_PROXY_HEADERS` is truthy; verified by route test.
- [ ] `slowapi`'s default 429 handler is replaced with the HTMX-aware local handler.
- [ ] `web/templates/_rate_limited.html` exists; renders an amber alert with retry-after seconds; contains no `id` attribute and no button.
- [ ] `Retry-After` header is set on every 429 response.
- [ ] A WARN log line is emitted on every 429.
- [ ] `@limiter.limit(settings.enrichment_rate_limit)` decorates the enrichment POST route.
- [ ] `@limiter.limit(settings.search_rate_limit)` decorates the `/search` route (replacing the module-constant version).
- [ ] `docs/adr/0017-rate-limit-posture.md` exists with Status: Accepted; references ADR 0016's multi-worker revisit trigger.
- [ ] CLAUDE.md "API keys" section documents the three new env vars and points at `concord.config.Settings`.
- [ ] [bill-enrichment-button.md](./bill-enrichment-button.md) Step 5 has been amended (at plan-write time) to consume `Settings`.
- [ ] `uv run ruff check`, `uv run ruff format --check`, `uv run mypy src`, `uv run pytest` all clean.

## Open questions

None — all design decisions resolved during grilling on 2026-05-27.

## Out-of-band work

- **Merge-order coordination with [bill-enrichment-button.md](./bill-enrichment-button.md).** The enrichment plan's Step 5 (as amended by this plan's Step 12) imports `from concord.config import Settings`. Therefore this plan's Steps 1–4 must land *before* the enrichment plan's implementation begins, OR the two plans are executed as one bundled arc by the same agent. The simplest path: bundle. Bundle PR scope: `pydantic-settings` dep + `concord.config` + the enrichment feature + this plan's rate-limit decorator and 429 handler. That's roughly 25 numbered steps across the two plans; large but coherent. A reviewer can read the two plan files first, then the diff.
- **`concord.api.Client` could consume `Settings().congress_api_key`** instead of reading `os.environ.get("CONGRESS_API_KEY")` directly ([src/concord/api.py:121](../../src/concord/api.py:121)). Small follow-up, not part of this plan. Trade-off: introduces a dep from `concord.api` (currently lean) on `concord.config` (and transitively `pydantic-settings`).
- **The CLI modules (`src/concord/cli/`) also read `CONGRESS_API_KEY` and `OPENAI_API_KEY` directly.** Same follow-up opportunity as above. Migrating them is a one-line edit per command function and aligns the codebase on a single config-read pattern. Not required for this plan to land.
- **Distributed rate limiting / multi-worker correctness.** ADR 0017's "what stays open" section names this. Revisit trigger: introduction of `concord serve --workers N > 1`. Migration: `Limiter(storage_uri="redis://…")` with a corresponding Redis container in the deployment stack. Out of scope here.
- **Observability + circuit-breaker for distributed-abuse response.** If logs show sustained quota burn from many IPs collectively, the right response is a small counter that auto-disables enrichment for N minutes (effectively flipping the kill switch programmatically), not a static global cap. Defer until the threat materializes; ADR 0017 names this.
- **The Dockerfile follow-up referenced in [ADR 0012](../adr/0012-web-bootstraps-empty-schema-on-startup.md)** should document `CONCORD_TRUST_PROXY_HEADERS=1` as a recommended env-var setting alongside the standard Traefik deployment. That's a Dockerfile PR concern, not this plan's.
