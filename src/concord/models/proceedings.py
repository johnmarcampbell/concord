"""Models for the Proceedings entity (Phase 0).

- :class:`Issue` — one daily Congressional Record issue (metadata only).
- :class:`Article` — one article within an issue, including its text URL.
- :class:`Proceeding` — the final output record: an issue + article + text.
"""

import re
from datetime import date, datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, HttpUrl, model_validator

from concord.models._common import SessionNumber

# Granule IDs look like ``CREC-2026-05-22-pt1-PgD551-6`` and appear as the
# filename stem of every article URL (both the HTML and PDF variants). The
# pattern is documented at https://www.govinfo.gov/help/crec.
_GRANULE_ID_RE = re.compile(r"(CREC-\d{4}-\d{2}-\d{2}-[A-Za-z0-9-]+?)(?:\.[a-z]+)?$")


def parse_granule_id(url: str) -> str:
    """Extract the granule ID from a Congressional Record article URL.

    Accepts the ``.htm``, ``.pdf``, or extensionless forms. Raises ``ValueError``
    if the URL doesn't contain a recognizable granule ID.
    """
    match = _GRANULE_ID_RE.search(url)
    if not match:
        raise ValueError(f"no granule ID found in URL: {url!r}")
    return match.group(1)


class Issue(BaseModel):
    """One daily Congressional Record issue.

    Wire shape of one row from ``/v3/daily-congressional-record/`` list
    responses. Constructed via :meth:`from_congress_api`.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    issue_date: date
    congress: int
    session: SessionNumber
    volume: int
    issue_number: int
    update_date: datetime

    @classmethod
    def from_congress_api(cls, payload: dict[str, Any]) -> Self:
        """Project one ``/daily-congressional-record`` row into an :class:`Issue`.

        Strips the Z-suffixed time portion from ``issueDate`` (the API
        delivers it as ``"2026-05-22T04:00:00Z"`` even though it's a
        date-only field). Per ADR 0018 Rule 3, that normalization lives
        in this factory body rather than in a ``@field_validator``.
        """
        raw_date = payload["issueDate"]
        if isinstance(raw_date, str) and "T" in raw_date:
            raw_date = raw_date.split("T", 1)[0]
        return cls(
            issue_date=raw_date,
            congress=payload["congress"],
            session=payload["sessionNumber"],
            volume=payload["volumeNumber"],
            issue_number=payload["issueNumber"],
            update_date=payload["updateDate"],
        )


class Article(BaseModel):
    """One article (proceeding) within a daily issue.

    Wire shape of one item from the ``/v3/daily-congressional-record/.../articles``
    response. Constructed via :meth:`from_congress_api`, which flattens
    the API's nested ``text: [{type, url}, ...]`` array into the
    ``text_url`` / ``pdf_url`` columns. ``granule_id`` is auto-derived
    from ``text_url`` in the factory and cross-checked against ``pdf_url``
    via the ``@model_validator`` below — the validator is an *invariant
    assertion* (cross-field consistency), not a semantic shim, so it
    stays on the wire-shape model per ADR 0018 Rule 3.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    section: str
    title: str
    start_page: str
    end_page: str
    text_url: HttpUrl
    pdf_url: HttpUrl
    granule_id: str

    @model_validator(mode="before")
    @classmethod
    def _default_granule_id(cls, data: Any) -> Any:
        """Default ``granule_id`` from ``text_url`` when callers omit it.

        Not a Rule 3 normalization shim — Pydantic field-level ``default``
        cannot reference other fields, and this is the idiomatic
        cross-field default. ``from_congress_api`` passes ``granule_id``
        explicitly; this validator only kicks in for direct ``Article(...)``
        construction (mostly test fixtures).
        """
        if not isinstance(data, dict):
            return data
        if "granule_id" not in data and "text_url" in data:
            data = {**data, "granule_id": parse_granule_id(str(data["text_url"]))}
        return data

    @classmethod
    def from_congress_api(cls, payload: dict[str, Any], *, section: str) -> Self:
        """Project one ``articles`` row into an :class:`Article`.

        ``section`` is supplied by the caller because the API's response
        groups articles under a per-section key; the section name itself
        isn't repeated inside each article record. Raises :exc:`ValueError`
        if the article's ``text`` array doesn't carry both ``"Formatted Text"``
        and ``"PDF"`` entries.
        """
        urls = {t["type"]: t["url"] for t in payload.get("text", [])}
        try:
            text_url = urls["Formatted Text"]
            pdf_url = urls["PDF"]
        except KeyError as exc:
            raise ValueError(
                f"article {payload.get('title', '?')!r} missing text format {exc.args[0]!r}"
            ) from exc
        granule_id = parse_granule_id(str(text_url))
        return cls(
            section=section,
            title=payload["title"],
            start_page=payload["startPage"],
            end_page=payload["endPage"],
            text_url=text_url,
            pdf_url=pdf_url,
            granule_id=granule_id,
        )

    @model_validator(mode="after")
    def _check_granule_id_matches_urls(self) -> Self:
        from_text = parse_granule_id(str(self.text_url))
        if from_text != self.granule_id:
            raise ValueError(
                f"granule_id {self.granule_id!r} does not match text_url ({from_text!r})"
            )
        from_pdf = parse_granule_id(str(self.pdf_url))
        if from_pdf != self.granule_id:
            raise ValueError(
                f"granule_id {self.granule_id!r} does not match pdf_url ({from_pdf!r})"
            )
        return self


class Proceeding(BaseModel):
    """The final output record: one article's full metadata + plain text.

    A ``Proceeding`` is what gets written to storage. It carries everything
    from the parent :class:`Issue` and the :class:`Article` plus the fetched
    text and the fetch timestamp.
    """

    model_config = ConfigDict(extra="ignore")

    # Issue fields
    issue_date: date
    congress: int
    session: SessionNumber
    volume: int
    issue_number: int
    update_date: datetime

    # Article fields
    section: str
    title: str
    start_page: str
    end_page: str
    text_url: HttpUrl
    pdf_url: HttpUrl
    granule_id: str

    # Fetched
    text: str
    fetched_at: datetime

    @classmethod
    def build(cls, *, issue: Issue, article: Article, text: str, fetched_at: datetime) -> Self:
        """Combine an issue, an article, and fetched text into a Proceeding."""
        return cls(
            **issue.model_dump(),
            **article.model_dump(),
            text=text,
            fetched_at=fetched_at,
        )


__all__ = ["Article", "Issue", "Proceeding", "parse_granule_id"]
