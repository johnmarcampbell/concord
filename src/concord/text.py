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
``api.congress.gov`` and has its own (undocumented) rate limit — bulk pulls
of many articles can trip 429s or 403s. Retries mirror :mod:`concord.api`:

* HTTP 403: congress.gov uses 403 as a rate-limit signal in addition to 429.
  Treated identically to 429: retry **indefinitely** with exponential backoff.
* HTTP 429: retry **indefinitely**, honoring ``Retry-After`` and otherwise
  backing off exponentially capped at :data:`MAX_BACKOFF`.
* HTTP 5xx and transport errors (DNS, timeout, connection reset): retry up
  to :data:`MAX_5XX_RETRIES` times before surfacing a :class:`TextFetchError`.

Retry decisions are logged via :mod:`logging` (logger ``concord.text``).
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


def fetch_text(
    url: str,
    client: httpx.Client,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Fetch an article URL and return its plain text.

    The caller owns the :class:`httpx.Client` (so connection pooling, custom
    transports, and timeout policy are external concerns). Redirects are
    followed automatically. Transient failures (429, 5xx, transport errors)
    are retried per the module-level policy; ``sleep`` is injectable so tests
    don't pay real wall time.

    Raises :class:`TextFetchError` on:

    - non-success HTTP status after retries are exhausted (``status_code`` populated)
    - transport-level failures after retries are exhausted (``status_code`` is ``None``)
    - HTML that contains no ``<pre>`` block (``status_code`` is ``None``)
    """
    response = _get_with_retry(url, client, sleep)

    extractor = _PreExtractor()
    extractor.feed(response.text)
    text = extractor.text
    if not text:
        raise TextFetchError(f"no <pre> content found at {url}")
    return text


def _get_with_retry(
    url: str,
    client: httpx.Client,
    sleep: Callable[[float], None],
) -> httpx.Response:
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

        if status == HTTP_FORBIDDEN:
            # congress.gov returns 403 instead of 429 when rate-limiting bulk
            # fetches. Treat it the same as 429: back off and retry indefinitely
            # rather than surfacing a permanent failure. Retries do not count
            # against transient_attempts — being throttled is a wait condition,
            # not a fault.
            delay = _retry_after_seconds(response) or _backoff_seconds(transient_attempts)
            _log.warning("403 from %s (rate-limited?); backing off %.1fs before retry", url, delay)
            sleep(delay)
            continue

        if status == HTTP_TOO_MANY_REQUESTS:
            delay = _retry_after_seconds(response) or _backoff_seconds(transient_attempts)
            _log.warning("429 from %s; backing off %.1fs before retry", url, delay)
            sleep(delay)
            # 429 retries do not increment transient_attempts — rate-limited
            # is a wait condition, not a fault.
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
