"""Bill and tier-2 entity models (Phase 2a/b).

The :class:`Bill` row carries identity fields only; the political-graph
data (cosponsors, actions, subjects, titles, summaries) lives in the
five tier-2 models below, one per :doc:`ADR 0009 </adr/0009>` JSONL
sibling file.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator


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

    @field_validator("bill_type", mode="before")
    @classmethod
    def _coerce_bill_type(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.lower()
        return value

    @classmethod
    def from_congress_api(cls, payload: dict[str, Any]) -> "Bill":
        """Project a ``/v3/bill/{c}/{t}/{n}`` detail payload into a :class:`Bill`.

        The detail endpoint nests sponsors / policyArea / latestAction one
        level deep; this helper flattens them into the columns the loader
        writes. The first sponsor is taken (Bills have at most one per
        Congress's rules — the array is the API's convention, not a
        multi-sponsor signal). Raises ``ValidationError`` on a malformed
        or incomplete payload.
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

        return cls(
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


class Cosponsor(BaseModel):
    """One Member's M:N edge to a Bill, as recorded on the Bill's cosponsors list.

    ``sponsorship_withdrawn_date`` is non-NULL for Members who removed
    their name after signing on. ``is_original_cosponsor`` is True for
    cosponsors recorded on the day of introduction.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    bioguide_id: str
    sponsorship_date: str | None = None
    sponsorship_withdrawn_date: str | None = None
    is_original_cosponsor: bool = False

    @classmethod
    def from_congress_api(cls, payload: dict[str, Any]) -> "Cosponsor":
        """Project one ``/cosponsors`` row into a :class:`Cosponsor`.

        Raises ``ValidationError`` (or ``ValueError`` for the bioguide check
        below) if the row lacks the fields needed to link a position back to
        a Member — the API has been known to emit placeholder entries for
        unfilled vacancies, and the loader should surface those rather than
        silently drop them.
        """
        bioguide = payload.get("bioguideId")
        if not isinstance(bioguide, str) or not bioguide:
            raise ValueError(f"cosponsor row missing bioguideId: {payload!r}")
        raw_original = payload.get("isOriginalCosponsor")
        if isinstance(raw_original, bool):
            is_original = raw_original
        elif isinstance(raw_original, str):
            is_original = raw_original.lower() in {"true", "y", "yes", "1"}
        else:
            is_original = False
        return cls(
            bioguide_id=bioguide,
            sponsorship_date=payload.get("sponsorshipDate"),
            sponsorship_withdrawn_date=payload.get("sponsorshipWithdrawnDate"),
            is_original_cosponsor=is_original,
        )


class BillAction(BaseModel):
    """One event in a Bill's legislative history."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    action_date: str
    action_text: str
    action_code: str | None = None
    source_system: str | None = None

    @classmethod
    def from_congress_api(cls, payload: dict[str, Any]) -> "BillAction":
        """Project one ``/actions`` row into a :class:`BillAction`."""
        action_date = payload.get("actionDate")
        text = payload.get("text")
        if not action_date or not text:
            raise ValueError(f"action row missing actionDate/text: {payload!r}")
        source_raw = payload.get("sourceSystem")
        source_system = source_raw.get("name") if isinstance(source_raw, dict) else None
        return cls(
            action_date=str(action_date),
            action_text=str(text),
            action_code=payload.get("actionCode"),
            source_system=source_system,
        )


class BillSubject(BaseModel):
    """One CRS-assigned legislative subject for a Bill.

    A thin wrapper around a single string — modeled so the loader and
    storage layer can speak the same noun for the row.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    name: str

    @classmethod
    def from_congress_api(cls, payload: dict[str, Any]) -> "BillSubject":
        """Project one ``legislativeSubjects`` row into a :class:`BillSubject`."""
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"subject row missing name: {payload!r}")
        return cls(name=name)


class BillTitle(BaseModel):
    """One title variant for a Bill (display, official, short, popular)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    title_type: str
    title_text: str
    chamber: str | None = None

    @classmethod
    def from_congress_api(cls, payload: dict[str, Any]) -> "BillTitle":
        """Project one ``/titles`` row into a :class:`BillTitle`."""
        title_type = payload.get("titleType")
        title_text = payload.get("title")
        if not title_type or not title_text:
            raise ValueError(f"title row missing titleType/title: {payload!r}")
        return cls(
            title_type=str(title_type),
            title_text=str(title_text),
            chamber=payload.get("chamberName") or payload.get("chamberCode") or None,
        )


class BillSummary(BaseModel):
    """One CRS-written summary version for a Bill."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    version_code: str
    action_date: str | None = None
    action_desc: str | None = None
    summary_text: str

    @classmethod
    def from_congress_api(cls, payload: dict[str, Any]) -> "BillSummary":
        """Project one ``/summaries`` row into a :class:`BillSummary`."""
        version_code = payload.get("versionCode")
        text = payload.get("text")
        if not version_code or text is None:
            raise ValueError(f"summary row missing versionCode/text: {payload!r}")
        return cls(
            version_code=str(version_code),
            action_date=payload.get("actionDate"),
            action_desc=payload.get("actionDesc"),
            summary_text=str(text),
        )


__all__ = [
    "Bill",
    "BillAction",
    "BillSnapshot",
    "BillSubject",
    "BillSummary",
    "BillTitle",
    "Cosponsor",
    "bill_id_from_components",
]
