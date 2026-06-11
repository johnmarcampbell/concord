"""Bill section catalogue and Bill entity models (Phase 2a/b).

The :class:`BillDetail` row carries identity fields only; the political-graph
data (cosponsors, actions, subjects, titles, summaries) lives in the
five Bill-section models below, one per :doc:`ADR 0009 </adr/0009>` JSONL
sibling file. :data:`BILL_SECTIONS` is the catalogue of those sections —
the single source of truth for their names and every string derived from
them (ADR 0025). Model naming follows the endpoint that produced each model
per :doc:`ADR 0018 </adr/0018>`: ``BillDetail`` from ``/v3/bill/{c}/{t}/{n}``,
``BillCosponsor`` from ``/cosponsors``, etc. The unqualified "Bill" is
the aggregate domain concept and is not bound to any class.
"""

from dataclasses import dataclass
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict


@dataclass(frozen=True)
class BillSection:
    """One Bill section — a mutable sub-endpoint of the Bill aggregate (ADR 0009).

    The catalogue entry carries every name derived from the section:
    ``name`` (the plural token used by the CLI, the sub-endpoint URL
    segment, and every per-stage mapping key), ``entity`` (the singular
    name recorded for this section's child rows in ``validation_failures``,
    ADR 0023), ``jsonl_name`` (the ADR 0009 JSONL sibling file), and
    ``fetched_at_column`` (the ``bills`` column its loader stamps). All
    four are spelled out literally — never computed — so each string is
    greppable and a test can assert internal consistency. Data only by
    design: fetchers, writers, and projectors stay in their stage modules,
    keyed by ``name`` (ADR 0025).
    """

    name: str
    entity: str
    jsonl_name: str
    fetched_at_column: str


#: The Bill section catalogue — which sections make up the Bill aggregate.
#: Scraper, loader, storage, web, and CLI all consult this tuple; their
#: per-stage maps are keyed by ``.name`` and drift-checked against it in
#: tests. Adding a section means one entry here plus a model, a projector,
#: a fetcher, and a storage writer (ADR 0025).
BILL_SECTIONS: tuple[BillSection, ...] = (
    BillSection("cosponsors", "cosponsor", "bill_cosponsors.jsonl", "cosponsors_fetched_at"),
    BillSection("actions", "action", "bill_actions.jsonl", "actions_fetched_at"),
    BillSection("subjects", "subject", "bill_subjects.jsonl", "subjects_fetched_at"),
    BillSection("titles", "title", "bill_titles.jsonl", "titles_fetched_at"),
    BillSection("summaries", "summary", "bill_summaries.jsonl", "summaries_fetched_at"),
)

#: Section names in catalogue order — the "all sections" default everywhere.
BILL_SECTION_NAMES: tuple[str, ...] = tuple(s.name for s in BILL_SECTIONS)

#: By-name lookup for callers handed a bare section name (CLI args, map keys).
BILL_SECTIONS_BY_NAME: dict[str, BillSection] = {s.name: s for s in BILL_SECTIONS}


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
