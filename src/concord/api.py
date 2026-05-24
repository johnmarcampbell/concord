"""Typed client for `api.congress.gov <https://api.congress.gov/>`_.

All HTTP and JSON-parsing concerns live here. Callers receive validated
Pydantic models (:class:`Issue`, :class:`Article`) and never touch the
raw camelCase payload shape.

Rate-limit and retry handling are intentionally absent — they land in #23
once the client is in use. Today, transient failures surface as
:class:`ApiError`.
"""

from __future__ import annotations

import os
from types import TracebackType
from typing import Any

import httpx

from . import __version__
from .models import Article, Issue

API_BASE = "https://api.congress.gov/v3"
USER_AGENT = f"concord/{__version__} (+https://github.com/johnmarcampbell/concord)"
ENV_API_KEY = "CONGRESS_API_KEY"


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
    ) -> None:
        resolved = api_key if api_key is not None else os.environ.get(ENV_API_KEY)
        if not resolved:
            raise ApiError(f"API key required: pass api_key=... or set {ENV_API_KEY}")
        self._api_key = resolved
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
        try:
            response = self._client.get(path, params=merged)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ApiError(
                f"{exc.response.status_code} {exc.response.reason_phrase} from {path}",
                status_code=exc.response.status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise ApiError(f"transport error calling {path}: {exc}") from exc
        data: Any = response.json()
        if not isinstance(data, dict):
            raise ApiError(f"expected JSON object from {path}, got {type(data).__name__}")
        return data


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
