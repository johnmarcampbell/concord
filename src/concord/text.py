"""Plain-text extraction for Congressional Record articles.

Articles served under ``congress.gov/.../modified/CREC-*.htm`` are a single
``<pre>`` block wrapping the article body with the occasional inline ``<a>``
tag pointing at gpo.gov. Extraction is: GET the URL, parse the HTML with
``html.parser`` from the stdlib, return the text inside ``<pre>`` with tags
dropped but their inner text preserved.

No bs4, no lxml — the format is stable enough that one stdlib parser does the
job and removes a dependency the rest of the project doesn't need.

Retry policy
------------

The ``www.congress.gov`` HTML tier is a separate service from
``api.congress.gov``: it sits behind Cloudflare and has its own (undocumented)
rate limit. A bulk pull of many articles trips two distinct Cloudflare signals,
and the limit is enforced **per client across every URL**, not per request —
so the backoff state lives on a single :class:`AdaptiveThrottle` shared by all
fetches in a run, not inside one URL's retry loop (see ADR 0002's "JSONL is
canonical, re-runs are cheap" — being throttled is a wait, never a data loss):

* HTTP 429 (Cloudflare rate-limit rule): typically carries a ``Retry-After``;
  we honor it. Retry **indefinitely**.
* HTTP 403 (Cloudflare bot/WAF block): a block page with **no** ``Retry-After``,
  so there is no server hint to honor. Treated as the same throttle signal as
  429 and retried **indefinitely**, but the wait comes from our own adaptive
  backoff. This is the case a flat retry handles badly, which is why the
  throttle escalates and persists across URLs rather than resetting per fetch.
* HTTP 5xx and transport errors (DNS, timeout, connection reset): genuine
  faults — retry up to :data:`MAX_5XX_RETRIES` times with exponential backoff
  before surfacing a :class:`TextFetchError`. These are tracked per fetch and
  are kept separate from throttling, which never counts against that budget.

Both throttle signals escalate one :class:`AdaptiveThrottle` level per hit and
relax one level per :data:`_DECAY_AFTER_SUCCESSES` clean fetches, so a steady
drip of "one 403 per URL" ratchets the backoff up instead of oscillating at the
floor. Retry decisions are logged via :mod:`logging` (logger ``concord.text``).
"""

import logging
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

#: Cap on a single backoff delay, in seconds. Applied to both the exponential
#: schedule and Retry-After values so a server-suggested 1-hour wait can't
#: silently stall the pipeline.
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


#: Consecutive clean fetches required to relax the throttle one level. Decay is
#: deliberately slower than the one-level-per-throttle climb so a steady drip of
#: "one 403 per URL" still ratchets the backoff up rather than oscillating at the
#: 1s floor (a single success would otherwise cancel a single 403).
_DECAY_AFTER_SUCCESSES = 3

#: Cap on the throttle level. At this level the exponential backoff has already
#: saturated :data:`MAX_BACKOFF`; capping bounds how long recovery takes after a
#: sustained block (``_MAX_THROTTLE_LEVEL * _DECAY_AFTER_SUCCESSES`` clean
#: fetches to fully relax).
_MAX_THROTTLE_LEVEL = 7


class AdaptiveThrottle:
    """Client-level adaptive backoff shared across every article fetch in a run.

    congress.gov enforces its limit per client, not per request, so a single
    instance is threaded through all :func:`fetch_text` calls: the level climbs
    one step per 403/429 and relaxes one step per :data:`_DECAY_AFTER_SUCCESSES`
    clean fetches. :meth:`pace` proactively slows the *next* request once the
    level is raised (so a recovered fetch doesn't immediately re-trip the
    limit); :meth:`penalize` performs the post-throttle retry wait, honoring a
    server ``Retry-After`` when present and otherwise backing off exponentially.

    Throttle waits never count against the 5xx fault budget — being blocked is a
    wait condition, not a fault. ``sleep`` is injectable so tests don't pay real
    wall time.
    """

    def __init__(self, *, sleep: Callable[[float], None] = time.sleep) -> None:
        self._sleep = sleep
        self._level = 0
        self._ok_streak = 0

    @property
    def level(self) -> int:
        """Current throttle level (0 when healthy). Exposed for tests/logging."""
        return self._level

    def pace(self) -> None:
        """Wait the current throttle delay before a request (no-op when healthy)."""
        if self._level > 0:
            self._sleep(self._delay())

    def penalize(self, retry_after: float | None) -> float:
        """Record a throttle response, sleep the retry wait, return the delay used.

        Honors ``retry_after`` when the server supplied one (Cloudflare 429s do);
        otherwise falls back to the exponential schedule for the newly raised
        level (Cloudflare 403s don't).
        """
        self._level = min(self._level + 1, _MAX_THROTTLE_LEVEL)
        self._ok_streak = 0
        delay = retry_after if retry_after is not None else self._delay()
        self._sleep(delay)
        return delay

    def recover(self) -> None:
        """Note a clean fetch; relax one level after enough consecutive successes."""
        if self._level == 0:
            return
        self._ok_streak += 1
        if self._ok_streak >= _DECAY_AFTER_SUCCESSES:
            self._level -= 1
            self._ok_streak = 0

    def _delay(self) -> float:
        """Exponential backoff for the current level, capped at :data:`MAX_BACKOFF`.

        Level 1 → 1s, 2 → 2s, 3 → 4s, … saturating at the cap.
        """
        return min(_BACKOFF_BASE ** (self._level - 1), MAX_BACKOFF)


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

    ``throttle`` is the shared :class:`AdaptiveThrottle` carrying rate-limit
    state across every fetch in a run; pass one instance for a whole pull so
    throttling congress.gov slows *subsequent* URLs too. When omitted, a fresh
    per-call throttle is used (fine for one-off fetches and tests). ``sleep`` is
    injectable so tests don't pay real wall time; it also seeds the default
    throttle's clock.

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
        raise TextFetchError(f"no <pre> content found at {url}")
    return text


def _get_with_retry(
    url: str,
    client: httpx.Client,
    throttle: AdaptiveThrottle,
    sleep: Callable[[float], None],
) -> httpx.Response:
    # Proactively wait out any backoff carried over from earlier URLs before the
    # first attempt — congress.gov rate-limits per client, so hammering the next
    # URL immediately after a throttle just re-trips the limit.
    throttle.pace()
    transient_attempts = 0
    while True:
        try:
            response = client.get(url, follow_redirects=True)
        except httpx.HTTPError as exc:
            if transient_attempts >= MAX_5XX_RETRIES:
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
            delay = throttle.penalize(_retry_after_seconds(response))
            _log.warning(
                "%d from %s (rate-limited?); backing off %.1fs before retry (level %d)",
                status,
                url,
                delay,
                throttle.level,
            )
            continue

        if HTTP_SERVER_ERROR_MIN <= status < HTTP_SERVER_ERROR_MAX:
            if transient_attempts >= MAX_5XX_RETRIES:
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
            raise TextFetchError(
                f"{status} {response.reason_phrase} fetching {url}",
                status_code=status,
            )

        throttle.recover()
        return response


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff capped at :data:`MAX_BACKOFF`. ``attempt`` is 0-based."""
    return min(_BACKOFF_BASE**attempt, MAX_BACKOFF)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse the ``Retry-After`` header, if any, as seconds.

    Supports the integer-seconds form. Returns ``None`` for the HTTP-date
    form or when the header is missing/unparseable; callers fall back to
    exponential backoff in that case. The result is clamped to
    :data:`MAX_BACKOFF` so a misbehaving server can't park us forever.
    """
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    return min(max(seconds, 0.0), MAX_BACKOFF)
