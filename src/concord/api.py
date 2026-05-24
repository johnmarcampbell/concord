"""Typed client for `api.congress.gov <https://api.congress.gov/>`_.

All HTTP and JSON-parsing concerns live here. Callers receive validated
Pydantic models (:class:`Issue`, :class:`Article`) and never touch the
raw camelCase payload shape.

Retry policy
------------

Transient failures are retried automatically:

* HTTP 429 ("Too Many Requests"): retry **indefinitely**, respecting any
  ``Retry-After`` header and otherwise backing off exponentially capped at
  :data:`MAX_BACKOFF`. Rate-limited is not broken — we wait.
* HTTP 5xx, connection errors, read/write/connect timeouts: retry up to
  :data:`MAX_5XX_RETRIES` times with exponential backoff. After that, the
  failure surfaces as an :class:`ApiError`.

Every retry decision is logged to ``stderr`` via :mod:`logging` (logger
``concord.api``) at WARNING level so multi-hour pulls have a visible
heartbeat.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from types import TracebackType
from typing import Any

import httpx

from . import __version__
from .models import Article, Issue

API_BASE = "https://api.congress.gov/v3"
USER_AGENT = f"concord/{__version__} (+https://github.com/johnmarcampbell/concord)"
ENV_API_KEY = "CONGRESS_API_KEY"

#: Cap on a single backoff delay, in seconds. Applied to both the exponential
#: schedule and Retry-After values so a server-suggested 1-hour wait can't
#: silently stall the pipeline.
MAX_BACKOFF = 60.0

#: Maximum retries for transient 5xx / transport failures before surfacing
#: an :class:`ApiError`. 429s are retried indefinitely separately.
MAX_5XX_RETRIES = 5

#: Exponential schedule for transient backoff: 1s, 2s, 4s, 8s, 16s (capped).
_BACKOFF_BASE = 2.0

_log = logging.getLogger("concord.api")


class ApiError(Exception):
    """Raised when api.congress.gov returns a non-success status or a transport error.

    ``status_code`` is the HTTP status when the failure was an HTTP response,
    or ``None`` for transport-level failures (DNS, timeout, connection reset).
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class Client:
    """Typed wrapper over ``api.congress.gov``.

    The client owns an ``httpx.Client`` underneath; pass a custom ``transport``
    (e.g. :class:`httpx.MockTransport`) to intercept requests in tests.

    Use as a context manager so the underlying connection pool is closed::

        with Client(api_key="...") as client:
            issues, next_offset = client.list_issues()
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        resolved = api_key if api_key is not None else os.environ.get(ENV_API_KEY)
        if not resolved:
            raise ApiError(f"API key required: pass api_key=... or set {ENV_API_KEY}")
        self._api_key = resolved
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=API_BASE,
            transport=transport,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
        )

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- endpoints -----------------------------------------------------------

    def list_issues(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Issue], int | None]:
        """List daily Congressional Record issues, newest first.

        Returns ``(issues, next_offset)``. ``next_offset`` is ``None`` once the
        last page has been served (the API omits ``pagination.next``).

        The API does not support a date filter on this endpoint; callers
        paginate until they walk past their target date.
        """
        payload = self._get(
            "/daily-congressional-record",
            params={"limit": limit, "offset": offset},
        )
        rows = payload.get("dailyCongressionalRecord", [])
        issues = [_parse_issue(row) for row in rows]
        has_next = "next" in payload.get("pagination", {})
        next_offset = offset + limit if has_next else None
        return issues, next_offset

    def list_articles(self, volume: int, issue_number: int) -> list[Article]:
        """List all articles in one issue, flattening the section nesting.

        The API groups articles by section (``Senate Section``, ``House
        Section``, ``Extensions of Remarks Section``, ``Daily Digest``). This
        method flattens them into a single list, populating each
        :class:`Article`'s ``section`` from the parent ``name``.
        """
        payload = self._get(
            f"/daily-congressional-record/{volume}/{issue_number}/articles",
        )
        out: list[Article] = []
        for section in payload.get("articles", []):
            section_name = section["name"]
            for art in section.get("sectionArticles", []):
                out.append(_parse_article(section_name, art))
        return out

    # -- internals -----------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        merged: dict[str, Any] = {"format": "json", "api_key": self._api_key}
        if params:
            merged.update(params)

        transient_attempts = 0
        while True:
            try:
                response = self._client.get(path, params=merged)
            except httpx.HTTPError as exc:
                # Transport-level failure (DNS, timeout, connection reset).
                # Treat as a retryable transient.
                if transient_attempts >= MAX_5XX_RETRIES:
                    raise ApiError(
                        f"transport error calling {path} "
                        f"(gave up after {MAX_5XX_RETRIES} attempts): {exc}"
                    ) from exc
                delay = _backoff_seconds(transient_attempts)
                _log.warning("transport error on %s (%s); retrying in %.1fs", path, exc, delay)
                self._sleep(delay)
                transient_attempts += 1
                continue

            status = response.status_code

            if status == 429:
                delay = _retry_after_seconds(response) or _backoff_seconds(transient_attempts)
                _log.warning("429 from %s; backing off %.1fs before retry", path, delay)
                self._sleep(delay)
                # 429 retries do not increment transient_attempts — rate-limited
                # is a wait condition, not a fault. We could be 429'd for hours.
                continue

            if 500 <= status < 600:
                if transient_attempts >= MAX_5XX_RETRIES:
                    raise ApiError(
                        f"{status} {response.reason_phrase} from {path} "
                        f"(gave up after {MAX_5XX_RETRIES} attempts)",
                        status_code=status,
                    )
                delay = _backoff_seconds(transient_attempts)
                _log.warning(
                    "%s from %s; retrying in %.1fs (attempt %d/%d)",
                    status,
                    path,
                    delay,
                    transient_attempts + 1,
                    MAX_5XX_RETRIES,
                )
                self._sleep(delay)
                transient_attempts += 1
                continue

            if not response.is_success:
                # Non-retryable client error (4xx other than 429): surface immediately.
                raise ApiError(
                    f"{status} {response.reason_phrase} from {path}",
                    status_code=status,
                )

            data: Any = response.json()
            if not isinstance(data, dict):
                raise ApiError(f"expected JSON object from {path}, got {type(data).__name__}")
            return data


# -- retry helpers ----------------------------------------------------------


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


# -- payload -> model -------------------------------------------------------


def _parse_issue(row: dict[str, Any]) -> Issue:
    return Issue(
        issue_date=row["issueDate"],
        congress=row["congress"],
        session=row["sessionNumber"],
        volume=row["volumeNumber"],
        issue_number=row["issueNumber"],
        update_date=row["updateDate"],
    )


def _parse_article(section_name: str, art: dict[str, Any]) -> Article:
    urls = {t["type"]: t["url"] for t in art.get("text", [])}
    try:
        text_url = urls["Formatted Text"]
        pdf_url = urls["PDF"]
    except KeyError as exc:
        raise ApiError(
            f"article {art.get('title', '?')!r} missing text format {exc.args[0]!r}"
        ) from exc
    return Article(
        section=section_name,
        title=art["title"],
        start_page=art["startPage"],
        end_page=art["endPage"],
        text_url=text_url,
        pdf_url=pdf_url,
    )  # type: ignore[call-arg]
