"""Pydantic models for Concord.

Every value that flows between the API client, text fetcher, and storage layer
is one of these types. API JSON is parsed *into* these models at the
network boundary; storage writes serialize *from* these models. Nothing in the
pipeline handles untyped dicts.

Proceedings (the original entity):

- :class:`Issue` — one daily Congressional Record issue (metadata only).
- :class:`Article` — one article within an issue, including its text URL.
- :class:`Proceeding` — the final output record: an issue + article + text.

Members (Phase 1):

- :class:`Member` — a person who has served in Congress.
- :class:`Term` — one continuous service period in one chamber.
- :class:`MemberSnapshot` — ADR 0006 envelope wrapping a raw API payload.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, HttpUrl, field_validator, model_validator

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


SessionNumber = Literal[1, 2]


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
    def _check_granule_id_matches_urls(self) -> Article:
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
    ) -> Proceeding:
        """Combine an issue, an article, and fetched text into a Proceeding."""
        return cls(
            **issue.model_dump(),
            **article.model_dump(),
            text=text,
            fetched_at=fetched_at,
        )


# ---------------------------------------------------------------------------
# Member entity (Phase 1)
# ---------------------------------------------------------------------------


Chamber = Literal["house", "senate"]


def _normalize_chamber(value: Any) -> Any:
    """Map the API's verbose chamber names to the canonical ``house``/``senate``.

    The API uses ``House of Representatives`` and ``Senate``; tests and SQL
    use the lowercased one-word forms. Pass-through for already-normalized
    values lets callers use either form on input.
    """
    if not isinstance(value, str):
        return value
    lower = value.strip().lower()
    if lower in {"house", "house of representatives"}:
        return "house"
    if lower == "senate":
        return "senate"
    return value


_STATE_NAME_TO_CODE = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
    "district of columbia": "DC",
    "puerto rico": "PR",
    "american samoa": "AS",
    "guam": "GU",
    "northern mariana islands": "MP",
    "virgin islands": "VI",
}


_STATE_CODE_LEN = 2


def normalize_state(value: str | None) -> str | None:
    """Map the API's full state name to a two-letter code; pass through codes."""
    if value is None:
        return None
    stripped = value.strip()
    if len(stripped) == _STATE_CODE_LEN and stripped.isupper():
        return stripped
    return _STATE_NAME_TO_CODE.get(stripped.lower(), stripped)


class Term(BaseModel):
    """One continuous service period for a :class:`Member` in one chamber.

    Keyed by ``(bioguide_id, congress, chamber)``. Party, state, and district
    are recorded per-Term so a Member who changed party between Congresses or
    moved between House and Senate has multiple Terms reflecting reality.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    bioguide_id: str
    congress: int
    chamber: Chamber
    party: str | None = None
    state: str  # two-letter state code (e.g. "VT")
    district: int | None = None  # NULL for senators
    start_date: str | None = None  # ISO YYYY-MM-DD or YYYY (year-only fallback)
    end_date: str | None = None  # None = currently serving

    @field_validator("chamber", mode="before")
    @classmethod
    def _coerce_chamber(cls, value: Any) -> Any:
        return _normalize_chamber(value)

    @field_validator("state", mode="before")
    @classmethod
    def _coerce_state(cls, value: Any) -> Any:
        return normalize_state(value) if isinstance(value, str) else value


class Member(BaseModel):
    """A person who has served in Congress.

    Identity fields only — anything that varies across a career (party,
    chamber, state, district) lives on :class:`Term` instead.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    bioguide_id: str
    first_name: str
    middle_name: str | None = None
    last_name: str
    suffix: str | None = None
    birth_year: int | None = None
    death_year: int | None = None
    display_name: str  # API's directOrderName
    photo_url: str | None = None  # API's depiction.imageUrl
    biography: str | None = None


class MemberSnapshot(BaseModel):
    """ADR 0006 envelope wrapping one raw ``/member`` API response.

    Each Stage 0 fetch appends one of these to ``data/members.jsonl``. The
    Stage 1 loader groups by ``key["bioguide_id"]`` and keeps the latest
    ``fetched_at`` per key.
    """

    model_config = ConfigDict(extra="ignore")

    fetched_at: datetime
    key: dict[str, str | int]
    payload: dict[str, Any]


# ---------------------------------------------------------------------------
# Helpers for projecting raw API payloads into typed Member + Term records.
# ---------------------------------------------------------------------------


def _split_inverted_name(name: str) -> tuple[str, str]:
    """Parse the API's ``"Last, First[ Middle][, Suffix]"`` into ``(first, last)``.

    A best-effort fallback when the API's structured first/last fields are
    absent (the list endpoint sometimes only returns the inverted form).
    """
    parts = [p.strip() for p in name.split(",", 1)]
    if len(parts) == 1:
        return parts[0], ""
    last = parts[0]
    first = parts[1].split()[0] if parts[1] else ""
    return first, last


def parse_member_identity(payload: dict[str, Any]) -> Member:
    """Project a raw ``/member`` payload into the identity-only :class:`Member` row.

    Identity fields (name, birth year, photo) are the same regardless of
    which Congress the API was queried for, so this is the half of a member
    payload that's safe to project without knowing the queried Congress.
    """
    bioguide_id = str(payload["bioguideId"])

    first_name = payload.get("firstName")
    last_name = payload.get("lastName")
    if not first_name or not last_name:
        # Fall back to parsing the inverted "Last, First" name from the
        # list endpoint when structured fields aren't present.
        inverted = payload.get("invertedOrderName") or payload.get("name") or ""
        derived_first, derived_last = _split_inverted_name(inverted)
        first_name = first_name or derived_first
        last_name = last_name or derived_last

    display_name = payload.get("directOrderName") or (
        f"{first_name} {last_name}".strip() if first_name or last_name else payload.get("name", "")
    )

    depiction = payload.get("depiction") or {}
    photo_url = depiction.get("imageUrl") if isinstance(depiction, dict) else None

    return Member(
        bioguide_id=bioguide_id,
        first_name=first_name or "",
        middle_name=payload.get("middleName"),
        last_name=last_name or "",
        suffix=payload.get("suffixName"),
        birth_year=_coerce_int(payload.get("birthYear")),
        death_year=_coerce_int(payload.get("deathYear")),
        display_name=display_name,
        photo_url=photo_url,
        biography=payload.get("biography"),
    )


def parse_member_term(payload: dict[str, Any], *, congress: int) -> Term | None:  # noqa: C901 — one branch per optional payload field
    """Project a raw ``/member`` payload + queried Congress into one :class:`Term`.

    The ``/v3/member/congress/{n}`` list endpoint omits ``congress`` from
    each ``terms.item`` — and returns the **same** payload for every
    Congress a Member served in. So a single payload can't tell us which
    Congresses it represents; the queried ``congress`` argument provides
    the missing context.

    Returns the Term row for ``(bioguide_id, congress, chamber)`` where
    ``chamber`` is found by matching ``congress``'s years against the
    payload's ``terms.item`` ranges. Returns ``None`` if no item covers
    the Congress (the payload contradicts its own listing — log + skip
    rather than 500).
    """
    bioguide_id = str(payload["bioguideId"])
    items = _extract_term_items(payload)
    item = _term_item_for_congress(items, congress)
    if item is None:
        return None

    chamber_raw = item.get("chamber")
    if chamber_raw is None:
        return None
    chamber = _normalize_chamber(chamber_raw)
    if chamber not in {"house", "senate"}:
        return None

    top_state = payload.get("state")
    top_district = payload.get("district")
    top_party = payload.get("partyName")

    state = item.get("stateCode") or item.get("stateName") or top_state
    if not state:
        return None

    district = item.get("district")
    if district is None and chamber == "house":
        district = top_district
    if isinstance(district, str):
        try:
            district = int(district)
        except ValueError:
            district = None
    if chamber == "senate":
        district = None

    # Clip the chamber-career window (API's startYear/endYear) to this
    # Congress's window. Each (member, congress) row should describe the
    # service inside that Congress, not the broader career stretch.
    cong_start_year = 2 * congress + 1787  # Congress 1 began 1789-01-03
    cong_end_year = 2 * congress + 1789  # = next Congress's start year

    api_start = _coerce_int(item.get("startYear"))
    if api_start is not None and api_start > cong_start_year:
        start_date = f"{api_start:04d}-01-03"  # joined mid-Congress
    else:
        start_date = f"{cong_start_year:04d}-01-03"

    api_end = _coerce_int(item.get("endYear"))
    if api_end is not None and api_end < cong_end_year:
        end_date = f"{api_end:04d}-01-03"  # left mid-Congress
    else:
        end_date = f"{cong_end_year:04d}-01-03"

    return Term(
        bioguide_id=bioguide_id,
        congress=congress,
        chamber=chamber,
        party=item.get("partyName") or top_party,
        state=state,  # validator normalizes to 2-letter
        district=district,
        start_date=start_date,
        end_date=end_date,
    )


def parse_member(payload: dict[str, Any], *, congress: int) -> tuple[Member, Term | None]:
    """Convenience wrapper: identity + one Term for the queried Congress."""
    return parse_member_identity(payload), parse_member_term(payload, congress=congress)


def _extract_term_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull the ``terms.item`` list from a /member payload, tolerating both
    list-endpoint (``{"terms": {"item": [...]}}``) and detail-endpoint
    (``{"terms": [...]}``) shapes."""
    terms_raw = payload.get("terms")
    if isinstance(terms_raw, dict):
        return [i for i in (terms_raw.get("item") or []) if isinstance(i, dict)]
    if isinstance(terms_raw, list):
        return [i for i in terms_raw if isinstance(i, dict)]
    return []


def _term_item_for_congress(items: list[dict[str, Any]], congress: int) -> dict[str, Any] | None:
    """Return the term item whose year range contains ``congress``, or None.

    A term item covers Congress N if its ``startYear`` falls on or before
    N's last year, and its ``endYear`` (when set) falls on or after N's
    first year. If multiple items overlap, returns the last match — useful
    for the rare member who switches chambers mid-Congress (a member is
    only listed under one chamber per Congress in practice).
    """
    cong_first_year = 2 * congress + 1787
    cong_last_year = cong_first_year + 1
    match: dict[str, Any] | None = None
    for item in items:
        start = _coerce_int(item.get("startYear"))
        if start is None or start > cong_last_year:
            continue
        end = _coerce_int(item.get("endYear"))
        if end is not None and end < cong_first_year:
            continue
        match = item
    return match


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Bill entity (Phase 2a)
# ---------------------------------------------------------------------------


#: Lowercase canonical Bill type codes accepted across the codebase. The
#: API returns these in mixed case (``HR``, ``HJRES``); :func:`Bill`'s
#: validator lowercases on the way in so SQL constraints stay simple.
BILL_TYPES = ("hr", "hres", "hjres", "hconres", "s", "sres", "sjres", "sconres")
BillType = Literal["hr", "hres", "hjres", "hconres", "s", "sres", "sjres", "sconres"]
OriginChamber = Literal["House", "Senate"]


def bill_id_from_components(congress: int, bill_type: str, bill_number: int) -> str:
    """Flatten a Bill's natural key into the single string used as the SQL PK.

    Shape: ``"{congress}-{bill_type_lower}-{bill_number}"`` (e.g.
    ``"119-hr-1234"``). Chosen to match the named ``source_id`` format
    in [ADR 0008] so Phase 5 chunks linkage is a mechanical join.
    """
    return f"{congress}-{bill_type.lower()}-{bill_number}"


class Bill(BaseModel):
    """A piece of legislation introduced in either chamber.

    Identity record only — the mutable political-graph data (cosponsors,
    actions, subjects, titles, summaries) lives in child tables added in
    Phase 2b. Fields here are exactly what the loader writes to the
    ``bills`` SQLite table.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    bill_id: str
    congress: int
    bill_type: BillType
    bill_number: int
    origin_chamber: OriginChamber
    title: str
    introduced_date: str | None = None  # ISO YYYY-MM-DD
    policy_area: str | None = None
    sponsor_bioguide_id: str | None = None
    latest_action_date: str | None = None
    latest_action_text: str | None = None
    update_date: str

    @field_validator("bill_type", mode="before")
    @classmethod
    def _coerce_bill_type(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.lower()
        return value


class BillSnapshot(BaseModel):
    """ADR 0006 envelope wrapping one raw ``/bill`` detail API response.

    Each Stage 0 fetch appends one of these to ``data/bills.jsonl``. The
    Stage 1 loader groups by ``(key["congress"], key["bill_type"],
    key["bill_number"])`` and keeps the latest ``fetched_at`` per key.
    """

    model_config = ConfigDict(extra="ignore")

    fetched_at: datetime
    key: dict[str, str | int]
    payload: dict[str, Any]


def parse_bill(payload: dict[str, Any]) -> Bill:
    """Project a ``/v3/bill/{c}/{t}/{n}`` detail payload into a :class:`Bill`.

    The detail endpoint nests sponsors / policyArea / latestAction one
    level deep; this helper flattens them into the columns the loader
    writes. The first sponsor is taken (Bills have at most one per
    Congress's rules — the array is the API's convention, not a
    multi-sponsor signal).
    """
    congress = int(payload["congress"])
    bill_type_raw = payload["type"]
    bill_number = int(payload["number"])

    sponsors = payload.get("sponsors") or []
    sponsor_bioguide = None
    if sponsors:
        first = sponsors[0]
        if isinstance(first, dict):
            sponsor_bioguide = first.get("bioguideId")

    policy_area_raw = payload.get("policyArea")
    policy_area = policy_area_raw.get("name") if isinstance(policy_area_raw, dict) else None

    latest_action_raw = payload.get("latestAction") or {}
    latest_action_date = (
        latest_action_raw.get("actionDate") if isinstance(latest_action_raw, dict) else None
    )
    latest_action_text = (
        latest_action_raw.get("text") if isinstance(latest_action_raw, dict) else None
    )

    update_date_raw = payload.get("updateDateIncludingText") or payload.get("updateDate")
    if not update_date_raw:
        raise ValueError(f"bill payload missing updateDate: {payload!r}")

    bill_type = str(bill_type_raw).lower()

    return Bill(
        bill_id=bill_id_from_components(congress, bill_type, bill_number),
        congress=congress,
        bill_type=bill_type,  # type: ignore[arg-type]
        bill_number=bill_number,
        origin_chamber=payload["originChamber"],
        title=payload["title"],
        introduced_date=payload.get("introducedDate"),
        policy_area=policy_area,
        sponsor_bioguide_id=sponsor_bioguide,
        latest_action_date=latest_action_date,
        latest_action_text=latest_action_text,
        update_date=str(update_date_raw),
    )
