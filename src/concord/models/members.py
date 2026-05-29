"""Member and Term models (Phase 1).

The list endpoint ``/v3/member/congress/{n}`` returns the **same** payload
regardless of which Congress was queried, so the projection from payload
to :class:`Term` always requires the queried-Congress context. Both
classes follow ADR 0018's wire-shape-doubles-as-domain pattern — Member
has effectively one primary wire shape so the bare name is unambiguous.
The persistence envelope is ``Snapshot[Member]`` (see ADR 0018, ADR 0006).
"""

from typing import Any, Self

from pydantic import BaseModel, ConfigDict

from concord.models._common import Chamber, normalize_chamber

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


def normalize_state(value: str) -> str:
    """Map the API's full state name to a two-letter code; pass through codes."""
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
    party: str
    state: str  # two-letter state code (e.g. "VT")
    district: int | None = None  # NULL for senators
    # ISO YYYY-MM-DD, always clipped to the Congress's window — for a
    # currently-serving member, ``end_date`` is the next Congress's start
    # date (``YYYY-01-03``), not None.
    start_date: str
    end_date: str

    @classmethod
    def from_congress_api(cls, payload: dict[str, Any], *, congress: int) -> Self:
        """Project a raw ``/member`` payload + queried Congress into one :class:`Term`.

        The list endpoint omits ``congress`` from each ``terms.item`` and
        returns the **same** payload for every Congress a Member served in;
        the queried ``congress`` argument supplies that missing context.

        Raises ``ValueError`` if no ``terms.item`` covers ``congress`` (the
        payload contradicts its own listing). Raises ``ValidationError``
        if any required field is missing or malformed.
        """
        bioguide_id = payload["bioguideId"]
        item = _term_item_for_congress(_extract_term_items(payload), congress)
        if item is None:
            raise ValueError(
                f"payload for {bioguide_id} has no terms.item covering Congress {congress}"
            )

        # In every fixture observed, ``terms.item`` carries ``chamber`` and
        # ``startYear``/``endYear`` but not state/district/party — those live
        # at the top level. Read each from its only-known-source.
        chamber = normalize_chamber(item["chamber"])
        district = payload.get("district") if chamber == "house" else None

        # Clip the chamber-career window (API's startYear/endYear) to this
        # Congress's window. Each (member, congress) row should describe the
        # service inside that Congress, not the broader career stretch.
        cong_start_year = 2 * congress + 1787  # Congress 1 began 1789-01-03
        cong_end_year = 2 * congress + 1789  # = next Congress's start year

        api_start = item.get("startYear")
        start_date = (
            f"{api_start:04d}-01-03"
            if api_start is not None and api_start > cong_start_year
            else f"{cong_start_year:04d}-01-03"
        )

        api_end = item.get("endYear")
        end_date = (
            f"{api_end:04d}-01-03"
            if api_end is not None and api_end < cong_end_year
            else f"{cong_end_year:04d}-01-03"
        )

        return cls(
            bioguide_id=bioguide_id,
            congress=congress,
            chamber=chamber,
            party=payload["partyName"],
            # Canonicalize state inline (per ADR 0018 Rule 3 — no
            # @field_validator semantic shims on the wire-shape model).
            state=normalize_state(payload["state"]),
            district=district,
            start_date=start_date,
            end_date=end_date,
        )


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

    @classmethod
    def from_congress_api(cls, payload: dict[str, Any]) -> Self:
        """Project a raw ``/member`` payload into the identity-only Member row.

        Identity fields (name, birth year, photo) are the same regardless of
        which Congress the API was queried for, so this is the half of a
        member payload that's safe to project without knowing the queried
        Congress. Raises ``ValidationError`` if a required field is missing.
        """
        depiction = payload.get("depiction") or {}
        return cls(
            bioguide_id=payload["bioguideId"],
            first_name=payload["firstName"],
            middle_name=payload.get("middleName"),
            last_name=payload["lastName"],
            suffix=payload.get("suffixName"),
            birth_year=payload.get("birthYear"),
            death_year=payload.get("deathYear"),
            display_name=payload["directOrderName"],
            photo_url=depiction.get("imageUrl"),
            biography=payload.get("biography"),
        )


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
        start = item.get("startYear")
        if start is None or start > cong_last_year:
            continue
        end = item.get("endYear")
        if end is not None and end < cong_first_year:
            continue
        match = item
    return match


__all__ = ["Member", "Term", "normalize_state"]
