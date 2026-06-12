"""Plain-text extraction for Congressional Record articles.

Articles served under ``congress.gov/.../modified/CREC-*.htm`` are a single
``<pre>`` block wrapping the article body with the occasional inline ``<a>``
tag pointing at gpo.gov. Extraction is: GET the URL, parse the HTML with
``html.parser`` from the stdlib, return the text inside ``<pre>`` with tags
dropped but their inner text preserved.

No bs4, no lxml — the format is stable enough that one stdlib parser does the
job and removes a dependency the rest of the project doesn't need.

Pacing and retry policy
-----------------------

The ``www.congress.gov`` HTML tier is a separate service from
``api.congress.gov``: it sits behind Cloudflare and has its own (undocumented)
rate limit, enforced **per client across every URL**, not per request — so the
pacing state lives on a single :class:`AdaptiveThrottle` shared by all fetches
in a run, not inside one URL's retry loop (see ADR 0002's "JSONL is canonical,
re-runs are cheap" — being throttled is a wait, never a data loss).

The throttle is *proactive*, not merely reactive. Cloudflare rate-limit rules
work on windows: once tripped, the client stays blocked for the remainder of
the rule's window (minutes, not seconds), and requests made inside the window
are wasted. Running full speed until the first 429 therefore guarantees a
long stall. Instead the throttle paces **every** request (AIMD — additive
decrease of the inter-request gap on success, multiplicative increase on
throttle) with random jitter so the request stream never looks
machine-regular, and treats a throttle response as a *window* signal:

* Every request first waits the current pace (starts at
  :data:`_INITIAL_PACE`, decays toward :data:`_MIN_PACE` over clean fetches,
  doubles per throttle hit up to :data:`_MAX_PACE`), jittered ±50%.
* HTTP 429 (Cloudflare rate-limit rule) and HTTP 403 (Cloudflare bot/WAF
  block, which carries no ``Retry-After``) are the same throttle signal.
  Both are retried **indefinitely** after a cooldown of
  ``max(Retry-After, 30s doubling per consecutive strike)`` capped at
  :data:`MAX_COOLDOWN` — retrying every minute inside a multi-minute block
  window (the old behavior) only burns requests, so consecutive strikes wait
  out progressively more of the window. Cooldowns are jittered upward so
  retries don't synchronize with the window boundary.
* HTTP 5xx and transport errors (DNS, timeout, connection reset): genuine
  faults — retry up to :data:`MAX_5XX_RETRIES` times with exponential backoff
  (capped at :data:`MAX_BACKOFF`) before surfacing a :class:`TextFetchError`.
  These are tracked per fetch and kept separate from throttling, which never
  counts against that budget.

Strikes reset on the first clean fetch, but the *pace* does not: it decays
additively, so a steady drip of "one 403 per URL" still ratchets the request
rate down instead of oscillating at the floor. Retry decisions are logged via
:mod:`logging` (logger ``concord.text``).
"""

import logging
import random
import time
from collections.abc import Callable
from html.parser import HTMLParser

import httpx

from concord.api import (
    HTTP_FORBIDDEN,
    HTTP_SERVER_ERROR_MAX,
    HTTP_SERVER_ERROR_MIN,
    HTTP_TOO_MANY_REQUESTS,
)
from concord.models.runs import Attempt
from concord.observability import Recorder, active_recorder

#: Cap on a single 5xx/transport backoff delay, in seconds. Throttle cooldowns
#: have their own, much higher cap (:data:`MAX_COOLDOWN`) — a Cloudflare block
#: window outlasts 60s, but a flapping origin server shouldn't.
MAX_BACKOFF = 60.0

#: Maximum retries for transient 5xx / transport failures before surfacing
#: a :class:`TextFetchError`. 429s are retried indefinitely separately.
MAX_5XX_RETRIES = 5

_BACKOFF_BASE = 2.0

_log = logging.getLogger("concord.text")


class TextFetchError(Exception):
    """Raised when an article URL can't be fetched or contains no ``<pre>`` block.

    ``status_code`` is the HTTP status for response-level failures, ``None``
    for transport failures and structural problems with the HTML itself.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class _PreExtractor(HTMLParser):
    """Accumulate text inside ``<pre>`` blocks; drop tags, keep their text.

    Tracks nesting depth so that defensively-malformed HTML (extra closing
    tags, etc.) doesn't break extraction. Anchor tags inside ``<pre>`` are
    dropped — only their inner text survives, which is exactly what you want
    for human-readable plain text.
    """

    def __init__(self) -> None:
        super().__init__()
        self._depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:  # noqa: ARG002 — HTMLParser override
        if tag == "pre":
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "pre" and self._depth > 0:
            self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._depth > 0:
            self._chunks.append(data)

    @property
    def text(self) -> str:
        return "".join(self._chunks).strip()


#: Inter-request gap, in seconds, when a run starts. Deliberately non-zero:
#: full speed until the first 429 means entering the Cloudflare block window
#: at maximum velocity. Low and slow finishes sooner than fast and blocked.
_INITIAL_PACE = 1.0

#: Floor the pace decays toward over sustained clean fetches. Never zero —
#: every request stays paced.
_MIN_PACE = 0.5

#: Ceiling on the inter-request pace after repeated throttling.
_MAX_PACE = 30.0

#: Additive pace decrease per clean fetch (the AI half of AIMD). Small on
#: purpose: recovering the 1s lost to one pace-doubling takes ~20 clean
#: fetches, so a steady drip of "one 403 per URL" ratchets the rate down
#: rather than oscillating at the floor.
_PACE_DECAY = 0.05

#: Multiplicative pace increase per throttle hit (the MD half of AIMD).
_PACE_GROWTH = 2.0

#: Pace jitter band: each pace wait is ``pace * uniform(LO, HI)``. A
#: machine-regular request cadence is itself a bot signal; jitter also keeps
#: retries from synchronizing with the rate-limit window.
_PACE_JITTER_LO = 0.5
_PACE_JITTER_HI = 1.5

#: First-strike throttle cooldown, in seconds, doubling per consecutive
#: strike. A 429/403 means the rate-limit window is already tripped; waits
#: shorter than the window remainder are pure waste.
_COOLDOWN_BASE = 30.0

_COOLDOWN_GROWTH = 2.0

#: Strikes beyond this stop growing the cooldown schedule — it has already
#: saturated :data:`MAX_COOLDOWN` (30 x 2^5 = 960 > 900), and an unbounded
#: exponent would eventually overflow on a permanent block ("retried
#: indefinitely" means wait forever, not crash at strike 1025).
_MAX_COOLDOWN_DOUBLINGS = 5

#: Cap on a single throttle cooldown and on honored ``Retry-After`` values,
#: in seconds (15 min — a realistic Cloudflare window, unlike the old 60s cap
#: that kept re-entering the block window). Bounds how long a misbehaving
#: server can park the pipeline per retry.
MAX_COOLDOWN = 900.0

#: Upward-only jitter fraction on cooldowns: waiting slightly longer than the
#: hint/schedule is cheap; retrying exactly at the window boundary risks
#: another strike.
_COOLDOWN_JITTER = 0.25


class AdaptiveThrottle:
    """Client-level AIMD pacer shared across every article fetch in a run.

    congress.gov enforces its limit per client, not per request, so a single
    instance is threaded through all :func:`fetch_text` calls. :meth:`pace`
    waits the current jittered inter-request gap before *every* request — the
    throttle is proactive, not a reaction to the first 429. :meth:`penalize`
    handles a throttle response: it doubles the pace and sleeps out a cooldown
    sized to the Cloudflare block window (``max(Retry-After, escalating
    schedule)``), doubling per consecutive strike. :meth:`recover` notes a
    clean fetch: strikes reset, and the pace decays additively toward the
    floor.

    Throttle waits never count against the 5xx fault budget — being blocked is
    a wait condition, not a fault. ``sleep`` and ``rng`` are injectable so
    tests don't pay real wall time and aren't at the mercy of jitter
    (``rng()`` must return a float in ``[0, 1)``; the default is
    :func:`random.random`).
    """

    def __init__(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._sleep = sleep
        self._rng = rng
        self._pace = _INITIAL_PACE
        self._strikes = 0

    @property
    def pace_seconds(self) -> float:
        """Current un-jittered inter-request gap. Exposed for tests/logging."""
        return self._pace

    @property
    def strikes(self) -> int:
        """Consecutive throttle responses since the last clean fetch."""
        return self._strikes

    def pace(self) -> None:
        """Wait the jittered inter-request gap. Applies to every request."""
        jitter = _PACE_JITTER_LO + (_PACE_JITTER_HI - _PACE_JITTER_LO) * self._rng()
        self._sleep(self._pace * jitter)

    def penalize(self, retry_after: float | None) -> float:
        """Record a throttle response, sleep out the cooldown, return the delay used.

        The cooldown is the larger of the server's ``Retry-After`` (Cloudflare
        429s carry one; 403s don't) and the escalating per-strike schedule —
        a hint shorter than the schedule is distrusted, because consecutive
        strikes mean honoring it just re-entered the block window. Jittered
        upward, capped at :data:`MAX_COOLDOWN`. Also doubles the pace, so the
        request rate after recovery stays below whatever tripped the limit.
        """
        self._strikes += 1
        self._pace = min(self._pace * _PACE_GROWTH, _MAX_PACE)
        doublings = min(self._strikes - 1, _MAX_COOLDOWN_DOUBLINGS)
        scheduled = _COOLDOWN_BASE * _COOLDOWN_GROWTH**doublings
        base = max(retry_after if retry_after is not None else 0.0, scheduled)
        delay = min(base * (1.0 + _COOLDOWN_JITTER * self._rng()), MAX_COOLDOWN)
        self._sleep(delay)
        return delay

    def recover(self) -> None:
        """Note a clean fetch: reset strikes, decay the pace toward the floor."""
        self._strikes = 0
        self._pace = max(self._pace - _PACE_DECAY, _MIN_PACE)


def fetch_text(
    url: str,
    client: httpx.Client,
    *,
    throttle: AdaptiveThrottle | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Fetch an article URL and return its plain text.

    The caller owns the :class:`httpx.Client` (so connection pooling, custom
    transports, and timeout policy are external concerns). Redirects are
    followed automatically. Transient failures (429, 403, 5xx, transport
    errors) are retried per the module-level policy.

    ``throttle`` is the shared :class:`AdaptiveThrottle` carrying pacing state
    across every fetch in a run; pass one instance for a whole pull so the
    request rate adapts across URLs. When omitted, a fresh per-call throttle is
    used (fine for one-off fetches and tests, at the cost of the initial ~1s
    pace — every request is paced, even healthy ones). ``sleep`` is injectable
    so tests don't pay real wall time; it also seeds the default throttle's
    clock.

    Raises :class:`TextFetchError` on:

    - non-success HTTP status after retries are exhausted (``status_code`` populated)
    - transport-level failures after retries are exhausted (``status_code`` is ``None``)
    - HTML that contains no ``<pre>`` block (``status_code`` is ``None``)
    """
    response = _get_with_retry(url, client, throttle or AdaptiveThrottle(sleep=sleep), sleep)

    extractor = _PreExtractor()
    extractor.feed(response.text)
    text = extractor.text
    if not text:
        # The HTTP fetch succeeded (counted above) but the body has no <pre>
        # block — a structural failure distinct from any network error. Record
        # it so "page fetched but unparseable" is visible in the Scrape Run,
        # not silently dropped (ADR 0021). status_code is None for this class.
        _record_structural_failure(active_recorder(), url)
        raise TextFetchError(f"no <pre> content found at {url}")
    return text


def _get_with_retry(
    url: str,
    client: httpx.Client,
    throttle: AdaptiveThrottle,
    sleep: Callable[[float], None],
) -> httpx.Response:
    # Observability (ADR 0021): mirror api.py's chokepoint. ``attempts``
    # accumulates every non-success try — transport failure, 403/429 throttle
    # (recorded as errors even though we retry them indefinitely), 5xx — so a
    # resolved-on-retry fetch can report what it weathered. ``rec`` is None
    # outside an active scrape, making the recording a no-op. Retry *behavior*
    # is unchanged.
    rec = active_recorder()
    attempts: list[Attempt] = []

    # Every request is paced, healthy or not — the gap carries over from earlier
    # URLs because congress.gov rate-limits per client, and going full speed
    # until the first 429 just enters the block window at maximum velocity.
    throttle.pace()
    transient_attempts = 0
    while True:
        try:
            response = client.get(url, follow_redirects=True)
        except httpx.HTTPError as exc:
            _note_attempt(attempts, transport_class=type(exc).__name__, message=str(exc))
            if transient_attempts >= MAX_5XX_RETRIES:
                _record_failure(rec, url, attempts)
                raise TextFetchError(
                    f"transport error fetching {url} "
                    f"(gave up after {MAX_5XX_RETRIES} attempts): {exc}"
                ) from exc
            delay = _backoff_seconds(transient_attempts)
            _log.warning("transport error on %s (%s); retrying in %.1fs", url, exc, delay)
            sleep(delay)
            transient_attempts += 1
            continue

        status = response.status_code

        if status in (HTTP_FORBIDDEN, HTTP_TOO_MANY_REQUESTS):
            # Both are Cloudflare throttle signals on this tier (403 = bot/WAF
            # block, no Retry-After; 429 = rate-limit rule, usually with one).
            # Escalate the shared throttle and retry indefinitely. Throttle waits
            # do not increment transient_attempts — being blocked is a wait
            # condition, not a fault, and must not burn the 5xx budget.
            _note_attempt(attempts, status=status, message=response.reason_phrase)
            retry_after = _retry_after_seconds(response)
            delay = throttle.penalize(retry_after)
            _log.warning(
                "%d from %s (rate-limited?); cooling down %.1fs before retry "
                "(Retry-After: %s, strike %d, pace now %.2fs/req)",
                status,
                url,
                delay,
                _format_retry_after(retry_after),
                throttle.strikes,
                throttle.pace_seconds,
            )
            continue

        if HTTP_SERVER_ERROR_MIN <= status < HTTP_SERVER_ERROR_MAX:
            _note_attempt(attempts, status=status, message=response.reason_phrase)
            if transient_attempts >= MAX_5XX_RETRIES:
                _record_failure(rec, url, attempts)
                raise TextFetchError(
                    f"{status} {response.reason_phrase} fetching {url} "
                    f"(gave up after {MAX_5XX_RETRIES} attempts)",
                    status_code=status,
                )
            delay = _backoff_seconds(transient_attempts)
            _log.warning(
                "%s from %s; retrying in %.1fs (attempt %d/%d)",
                status,
                url,
                delay,
                transient_attempts + 1,
                MAX_5XX_RETRIES,
            )
            sleep(delay)
            transient_attempts += 1
            continue

        if not response.is_success:
            _note_attempt(attempts, status=status, message=response.reason_phrase)
            _record_failure(rec, url, attempts)
            raise TextFetchError(
                f"{status} {response.reason_phrase} fetching {url}",
                status_code=status,
            )

        throttle.recover()
        _record_success(rec, url, attempts)
        return response


# -- observability helpers --------------------------------------------------
#
# Module-level (rather than inline blocks) so each recording stays one
# statement in the retry loop, which is already at the project's complexity
# ceiling. Source bucket is always ``"text"`` — the route table maps the full
# URL to ``text:article``.

#: Synthetic ``transport_class`` marker for the "fetched but no <pre> block"
#: structural failure, which has no HTTP status of its own (the fetch was a 200).
_NO_PRE_MARKER = "NoPreContent"


def _note_attempt(
    attempts: list[Attempt],
    *,
    status: int | None = None,
    transport_class: str | None = None,
    message: str,
) -> None:
    """Append one non-success :class:`Attempt`; ``n`` derives from list position."""
    attempts.append(
        Attempt(
            n=len(attempts) + 1,
            status=status,
            transport_class=transport_class,
            message=message,
        )
    )


def _record_success(rec: Recorder | None, url: str, attempts: list[Attempt]) -> None:
    """Record a successful text fetch, plus a resolved Run Event if it retried."""
    if rec is None:
        return
    rec.note_success("text", url)
    if attempts:
        rec.note_request_outcome("text", url, attempts, resolved=True)


def _record_failure(rec: Recorder | None, url: str, attempts: list[Attempt]) -> None:
    """Record a terminal text fetch failure as a failed Run Event (no-op without a recorder)."""
    if rec is not None:
        rec.note_request_outcome("text", url, attempts, resolved=False)


def _record_structural_failure(rec: Recorder | None, url: str) -> None:
    """Record the HTTP-200-but-no-<pre> structural failure as a failed Run Event.

    Distinct from the network failures above: the fetch succeeded (and was
    counted as a success) but the body was unparseable. A single synthetic
    attempt carries the :data:`_NO_PRE_MARKER` so the cause is legible.
    """
    if rec is None:
        return
    attempts = [
        Attempt(n=1, status=None, transport_class=_NO_PRE_MARKER, message="no <pre> content")
    ]
    rec.note_request_outcome("text", url, attempts, resolved=False)


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff capped at :data:`MAX_BACKOFF`. ``attempt`` is 0-based."""
    return min(_BACKOFF_BASE**attempt, MAX_BACKOFF)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse the ``Retry-After`` header, if any, as seconds.

    Supports the integer-seconds form. Returns ``None`` for the HTTP-date
    form or when the header is missing/unparseable; callers fall back to the
    cooldown schedule in that case. The result is clamped to
    :data:`MAX_COOLDOWN` so a misbehaving server can't park us forever.
    """
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    return min(max(seconds, 0.0), MAX_COOLDOWN)


def _format_retry_after(retry_after: float | None) -> str:
    """Render the parsed (clamped) ``Retry-After`` for the throttle log line.

    ``"none"`` when the header was absent or unparseable — so the log shows
    whether a cooldown came from the server's hint or our own strike schedule.
    """
    return "none" if retry_after is None else f"{retry_after:.0f}s"
