"""Bill and tier-2 entity models (Phase 2a/b).

The :class:`BillDetail` row carries identity fields only; the political-graph
data (cosponsors, actions, subjects, titles, summaries) lives in the
five tier-2 models below, one per :doc:`ADR 0009 </adr/0009>` JSONL
sibling file. Naming follows the endpoint that produced each model per
:doc:`ADR 0018 </adr/0018>`: ``BillDetail`` from ``/v3/bill/{c}/{t}/{n}``,
``BillCosponsor`` from ``/cosponsors``, etc. The unqualified "Bill" is
the aggregate domain concept and is not bound to any class.
"""

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict


def bill_id_from_components(congress: int, bill_type: str, bill_number: int) -> str:
    """Flatten a Bill's natural key into the single string used as the SQL PK.

    Shape: ``"{congress}-{bill_type_lower}-{bill_number}"`` (e.g.
    ``"119-hr-1234"``). Chosen to match the named ``source_id`` format
    in [ADR 0008] so Phase 5 chunks linkage is a mechanical join.
    """
    return f"{congress}-{bill_type.lower()}-{bill_number}"


class BillDetail(BaseModel):
    """Wire shape of ``/v3/bill/{c}/{t}/{n}`` — a Bill's identity record.

    The detail endpoint nests sponsors / policyArea / latestAction one
    level deep; :meth:`from_congress_api` flattens them into the columns
    the loader writes. Doubles as the domain model since the wire shape
    already aligns with what the ``bills`` table needs (ADR 0018 Rule 3).
    Mutable political-graph data (cosponsors, actions, subjects, titles,
    summaries) lives in the sibling tier-2 models below.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    bill_id: str
    congress: int
    bill_type: Literal["hr", "hres", "hjres", "hconres", "s", "sres", "sjres", "sconres"]
    bill_number: int
    origin_chamber: Literal["House", "Senate"]
    title: str
    introduced_date: str | None = None  # ISO YYYY-MM-DD
    policy_area: str | None = None
    sponsor_bioguide_id: str | None = None
    latest_action_date: str | None = None
    latest_action_text: str | None = None
    update_date: str

    @classmethod
    def from_congress_api(cls, payload: dict[str, Any]) -> Self:
        """Project a ``/v3/bill/{c}/{t}/{n}`` detail payload into a :class:`BillDetail`.

        The detail endpoint nests sponsors / policyArea / latestAction one
        level deep; this helper flattens them into the columns the loader
        writes. The first sponsor is taken (Bills have at most one per
        Congress's rules — the array is the API's convention, not a
        multi-sponsor signal). Raises ``ValidationError`` on a malformed
        or incomplete payload; raises ``ValueError`` if both ``updateDate``
        sources are absent.
        """
        # The API delivers ``number`` as a string ("1") on the detail
        # endpoint; coerce before composing the SQL primary key.
        bill_number = int(payload["number"])

        sponsors = payload.get("sponsors") or []
        sponsor_bioguide = sponsors[0]["bioguideId"] if sponsors else None

        policy_area = (payload.get("policyArea") or {}).get("name")
        latest_action = payload.get("latestAction") or {}

        # The detail endpoint exposes two timestamps; the ``IncludingText``
        # variant ticks when the bill *text* changes, so prefer it when set.
        update_date = payload.get("updateDateIncludingText") or payload.get("updateDate")
        if not update_date:
            raise ValueError(f"bill payload missing updateDate: {payload!r}")

        # Canonicalize bill_type inline (per ADR 0018 Rule 3 — no
        # @field_validator semantic shims on the wire-shape model).
        bill_type = payload["type"].lower() if isinstance(payload["type"], str) else payload["type"]

        return cls(
            bill_id=bill_id_from_components(payload["congress"], bill_type, bill_number),
            congress=payload["congress"],
            bill_type=bill_type,  # type: ignore[arg-type]  # Pydantic validates against Literal at construction
            bill_number=bill_number,
            origin_chamber=payload["originChamber"],
            title=payload["title"],
            introduced_date=payload.get("introducedDate"),
            policy_area=policy_area,
            sponsor_bioguide_id=sponsor_bioguide,
            latest_action_date=latest_action.get("actionDate"),
            latest_action_text=latest_action.get("text"),
            update_date=update_date,
        )


class BillCosponsor(BaseModel):
    """Wire shape of one row from ``/v3/bill/{c}/{t}/{n}/cosponsors``.

    Models a Member's M:N edge to a Bill. ``sponsorship_withdrawn_date``
    is non-NULL for Members who removed their name after signing on.
    ``is_original_cosponsor`` is True for cosponsors recorded on the day
    of introduction. The bare domain term "Cosponsor" stays in CONTEXT.md;
    the class follows the endpoint-naming rule (ADR 0018 Rule 4).
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    bioguide_id: str
    sponsorship_date: str
    sponsorship_withdrawn_date: str | None = None  # only set when withdrawn
    is_original_cosponsor: bool

    @classmethod
    def from_congress_api(cls, payload: dict[str, Any]) -> Self:
        """Project one ``/cosponsors`` row into a :class:`BillCosponsor`.

        Raises ``ValueError`` if the row lacks a ``bioguideId`` — the API
        has been known to emit placeholder entries for unfilled vacancies,
        and the loader should surface those rather than silently drop them.
        """
        bioguide = payload.get("bioguideId")
        if not bioguide:
            raise ValueError(f"cosponsor row missing bioguideId: {payload!r}")
        return cls(
            bioguide_id=bioguide,
            sponsorship_date=payload["sponsorshipDate"],
            sponsorship_withdrawn_date=payload.get("sponsorshipWithdrawnDate"),
            is_original_cosponsor=payload["isOriginalCosponsor"],
        )


class BillAction(BaseModel):
    """Wire shape of one row from ``/v3/bill/{c}/{t}/{n}/actions`` —
    one event in a Bill's legislative history."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    action_date: str
    action_text: str
    action_code: str
    source_system: str

    @classmethod
    def from_congress_api(cls, payload: dict[str, Any]) -> Self:
        """Project one ``/actions`` row into a :class:`BillAction`."""
        action_date = payload.get("actionDate")
        text = payload.get("text")
        if not action_date or not text:
            raise ValueError(f"action row missing actionDate/text: {payload!r}")
        return cls(
            action_date=action_date,
            action_text=text,
            action_code=payload["actionCode"],
            source_system=payload["sourceSystem"]["name"],
        )


class BillSubject(BaseModel):
    """Wire shape of one row from ``/v3/bill/{c}/{t}/{n}/subjects`` —
    one CRS-assigned legislative subject for a Bill.

    A thin wrapper around a single string — modeled so the loader and
    storage layer can speak the same noun for the row.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    name: str

    @classmethod
    def from_congress_api(cls, payload: dict[str, Any]) -> Self:
        """Project one ``legislativeSubjects`` row into a :class:`BillSubject`."""
        name = payload.get("name")
        if not name:
            raise ValueError(f"subject row missing name: {payload!r}")
        return cls(name=name)


class BillTitle(BaseModel):
    """Wire shape of one row from ``/v3/bill/{c}/{t}/{n}/titles`` —
    one title variant for a Bill (display, official, short, popular)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    title_type: str
    title_text: str
    chamber: str | None = None

    @classmethod
    def from_congress_api(cls, payload: dict[str, Any]) -> Self:
        """Project one ``/titles`` row into a :class:`BillTitle`."""
        title_type = payload.get("titleType")
        title_text = payload.get("title")
        if not title_type or not title_text:
            raise ValueError(f"title row missing titleType/title: {payload!r}")
        return cls(
            title_type=title_type,
            title_text=title_text,
            chamber=payload.get("chamberName"),
        )


class BillSummary(BaseModel):
    """Wire shape of one row from ``/v3/bill/{c}/{t}/{n}/summaries`` —
    one CRS-written summary version for a Bill."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    version_code: str
    action_date: str
    action_desc: str
    summary_text: str

    @classmethod
    def from_congress_api(cls, payload: dict[str, Any]) -> Self:
        """Project one ``/summaries`` row into a :class:`BillSummary`."""
        version_code = payload.get("versionCode")
        text = payload.get("text")
        if not version_code or text is None:
            raise ValueError(f"summary row missing versionCode/text: {payload!r}")
        return cls(
            version_code=version_code,
            action_date=payload["actionDate"],
            action_desc=payload["actionDesc"],
            summary_text=text,
        )
