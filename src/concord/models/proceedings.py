"""Models for the Proceedings entity (Phase 0).

- :class:`Issue` — one daily Congressional Record issue (metadata only).
- :class:`Article` — one article within an issue, including its text URL.
- :class:`Proceeding` — the final output record: an issue + article + text.
"""

import re
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, HttpUrl, field_validator, model_validator

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

    Field naming matches the API's camelCase via aliases so an API payload
    can be passed straight to ``Issue.model_validate``.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    issue_date: date
    congress: int
    session: SessionNumber
    volume: int
    issue_number: int
    update_date: datetime

    @field_validator("issue_date", mode="before")
    @classmethod
    def _coerce_issue_date(cls, value: Any) -> Any:
        """Accept the API's ``"2026-05-22T04:00:00Z"`` datetime strings as dates."""
        if isinstance(value, str) and "T" in value:
            return value.split("T", 1)[0]
        return value

    @field_validator("issue_number", mode="before")
    @classmethod
    def _coerce_issue_number(cls, value: Any) -> Any:
        """API returns ``issueNumber`` as a string; coerce to int."""
        if isinstance(value, str):
            return int(value)
        return value


class Article(BaseModel):
    """One article (proceeding) within a daily issue.

    ``granule_id`` is derived from ``text_url`` if not supplied, and verified
    against ``pdf_url`` when both are present. This means the API's flat
    ``text`` array (a list of ``{type, url}`` objects) can be flattened by
    callers and the model still keeps everything consistent.
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
    def _derive_granule_id(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        # Derive from text_url if caller didn't pass an explicit granule_id.
        if "granule_id" not in data and "text_url" in data:
            data = {**data, "granule_id": parse_granule_id(str(data["text_url"]))}
        return data

    @model_validator(mode="after")
    def _check_granule_id_matches_urls(self) -> "Article":
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
    def build(
        cls, *, issue: Issue, article: Article, text: str, fetched_at: datetime
    ) -> "Proceeding":
        """Combine an issue, an article, and fetched text into a Proceeding."""
        return cls(
            **issue.model_dump(),
            **article.model_dump(),
            text=text,
            fetched_at=fetched_at,
        )


__all__ = ["Article", "Issue", "Proceeding", "parse_granule_id"]
