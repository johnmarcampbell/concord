"""Vote-related models (Phase 3a).

- :class:`Vote` — the canonical (domain) row written to the ``votes`` SQLite
  table. House parses straight into this; Senate projects to it from
  :class:`SenateVoteDetail` (ADR 0018 Rule 3, wire-shape-projects-to-domain).
- :class:`VotePosition` — one Member's recorded position on one Vote.
  House wire shape coincides with domain shape; Senate projects to it
  from :class:`SenateVotePosition` after Bioguide resolution.
- :class:`HouseVoteMembers` — wire shape of the
  ``/v3/house-vote/{c}/{s}/{r}/members`` response wrapper.
- :class:`SenateVoteDetail` / :class:`SenateVotePosition` — wire shapes
  parsed from senate.gov LIS detail XML. Carry an unresolved
  ``member_full`` bridge string until the loader resolves to Bioguide ID.

Persistence envelopes are ``Snapshot[T]`` per ADR 0006 / ADR 0018:
``Snapshot[Vote]`` for House detail JSONL, ``Snapshot[HouseVoteMembers]``
for House positions JSONL, ``Snapshot[SenateVoteDetail]`` for Senate
detail JSONL.
"""

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, field_validator

from concord.models._common import Chamber, SessionNumber, normalize_chamber
from concord.models.bills import bill_id_from_components

VoteKind = Literal["standard", "election"]
VoteThreshold = Literal["simple_majority", "two_thirds", "three_fifths"]


def vote_id_from_components(
    chamber: str,
    congress: int,
    session: int,
    roll_number: int,
) -> str:
    """Flatten a Vote's natural key into the single string used as the SQL PK.

    Shape: ``"{chamber}-{congress}-{session}-{roll}"`` (e.g.
    ``"house-119-1-240"``). Lowercase chamber matches the
    `votes.chamber` column's CHECK constraint.
    """
    return f"{chamber.lower()}-{congress}-{session}-{roll_number}"


def amendment_id_from_components(
    congress: int,
    amendment_type: str,
    amendment_number: int,
) -> str:
    """Flatten an Amendment's natural key. Shape: ``"{congress}-{type}-{number}"``.

    Phase 4 (Amendments) will own this entity; Phase 3a stores it as
    bare TEXT on `votes.amendment_id` so the linkage exists when the
    Amendment profile page lands.
    """
    return f"{congress}-{amendment_type.lower()}-{amendment_number}"


def parse_vote_threshold(vote_type: str) -> VoteThreshold | None:
    """Map the API's free-text ``voteType`` to a normalized threshold code.

    Case-insensitive substring matches against the strings the spike
    found in House data: ``"2/3 Yea-And-Nay"`` → ``'two_thirds'``,
    ``"3/5"`` → ``'three_fifths'``, ``"Yea-and-Nay"`` / ``"Recorded
    Vote"`` / ``"Quorum"`` → ``'simple_majority'``. Anything else
    (including the empty string) returns ``None`` and the loader leaves
    the column NULL.
    """
    if not vote_type:
        return None
    lower = vote_type.lower()
    if "2/3" in lower:
        return "two_thirds"
    if "3/5" in lower:
        return "three_fifths"
    if "yea-and-nay" in lower or "recorded vote" in lower or "quorum" in lower:
        return "simple_majority"
    return None


class Vote(BaseModel):
    """One recorded roll-call decision in a chamber (domain model).

    Identified by ``(chamber, congress, session, roll_number)``; the
    flattened ``vote_id`` is the SQL primary key. ``bill_id`` and
    ``amendment_id`` are bare TEXT references (no FK) so ingest is
    robust to gaps in the Bills / Amendments tables.

    House wire shape and domain shape coincide — ``from_congress_api``
    parses the House JSON directly into this class. Senate's wire shape
    (:class:`SenateVoteDetail`) projects into ``Vote`` at load time via
    ``pipeline.load_votes._vote_from_senate_detail``.

    ``is_party_unity`` defaults to False; the indexer (Stage 2)
    populates it across all rows.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    vote_id: str
    chamber: Chamber
    congress: int
    session: SessionNumber
    roll_number: int
    vote_kind: VoteKind
    start_date: str  # ISO 8601 with offset, as the API supplies
    vote_question: str
    vote_type: str
    threshold: VoteThreshold | None = None
    result: str
    yea_count: int | None = None
    nay_count: int | None = None
    present_count: int | None = None
    not_voting_count: int | None = None
    bill_id: str | None = None
    amendment_id: str | None = None
    # Stage 1 (load_votes) doesn't know whether a vote is party-unity —
    # that requires the per-member positions. Stage 2 (index_votes)
    # computes it and runs ``UPDATE votes SET is_party_unity = ?``. The
    # ``False`` default IS the "not yet indexed" state in the contract.
    is_party_unity: bool = False
    update_date: str

    @field_validator("chamber", mode="before")
    @classmethod
    def _coerce_chamber(cls, value: Any) -> Any:
        return normalize_chamber(value)

    @classmethod
    def from_congress_api(cls, payload: dict[str, Any], *, chamber: str) -> Self:
        """Project a ``/v3/house-vote/{c}/{s}/{roll}`` detail payload into a :class:`Vote`.

        ``chamber`` is supplied by the caller because the API leaves it
        ``null`` on some detail payloads (the loader knows which endpoint
        it queried, so it can pass the right value).

        Handles two shape quirks the spike surfaced:

        1. ``votePartyTotal`` arrives as a list on some payloads and as
           ``{"item": [...]}`` on others — :func:`_extract_vote_party_totals`
           normalizes the two.
        2. Election votes (Speaker elections, etc.) bucket totals by
           candidate name rather than by party; in that shape we leave the
           count columns NULL and set ``vote_kind='election'``.

        Amendment votes carry both the amendment's identity *and* the
        underlying bill in ``legislationType`` + ``legislationNumber`` — the
        "amendment trap" — so both ``bill_id`` and ``amendment_id`` get
        populated for that case.

        Raises ``ValidationError`` on a malformed payload (missing required
        fields, wrong type, …); raises ``ValueError`` for the same reasons
        on the few sub-objects we crack open before constructing the model.
        """
        congress = payload["congress"]
        session = payload["sessionNumber"]
        roll_number = payload["rollCallNumber"]

        party_totals = _extract_vote_party_totals(payload)
        is_election = _is_election_vote(party_totals)

        # Real fixtures show ``legislationNumber`` arriving as either int
        # (3424) or str ("3424"); coerce so the SQL key formatter sees an int.
        bill_id = None
        if (leg_type := payload.get("legislationType")) and (
            leg_number := payload.get("legislationNumber")
        ) is not None:
            bill_id = bill_id_from_components(congress, leg_type, int(leg_number))

        amendment_id = None
        if (amd_type := payload.get("amendmentType")) and (
            amd_number := payload.get("amendmentNumber")
        ) is not None:
            amendment_id = amendment_id_from_components(congress, amd_type, int(amd_number))

        yea = nay = present = not_voting = None
        if not is_election:
            yea = sum(entry["yeaTotal"] for entry in party_totals)
            nay = sum(entry["nayTotal"] for entry in party_totals)
            present = sum(entry["presentTotal"] for entry in party_totals)
            not_voting = sum(entry["notVotingTotal"] for entry in party_totals)

        return cls(
            vote_id=vote_id_from_components(chamber, congress, session, roll_number),
            chamber=chamber,  # type: ignore[arg-type]  # validator normalizes "House" → "house"
            congress=congress,
            session=session,
            roll_number=roll_number,
            vote_kind="election" if is_election else "standard",
            start_date=payload["startDate"],
            vote_question=payload["voteQuestion"],
            vote_type=payload["voteType"],
            threshold=parse_vote_threshold(payload["voteType"]),
            result=payload["result"],
            yea_count=yea,
            nay_count=nay,
            present_count=present,
            not_voting_count=not_voting,
            bill_id=bill_id,
            amendment_id=amendment_id,
            is_party_unity=False,
            update_date=payload["updateDate"],
        )


class VotePosition(BaseModel):
    """One Member's recorded position on one Vote (domain model).

    ``position`` is free-text rather than an enum: for standard votes
    it's ``"Yea"`` / ``"Nay"`` / ``"Present"`` / ``"Not Voting"``; for
    Speaker-election rolls it's a candidate's surname. The spike confirmed
    the API uses both shapes interchangeably under the same field name.

    ``vote_party`` and ``vote_state`` are denormalized off the API
    payload so party-unity computation doesn't have to join `terms`.

    House wire shape coincides with domain shape — ``from_congress_api``
    parses House JSON rows directly. Senate's wire shape
    (:class:`SenateVotePosition`) projects into ``VotePosition`` at load
    time after the ``member_full`` → Bioguide bridge resolves.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    bioguide_id: str
    position: str
    vote_party: str
    vote_state: str

    @classmethod
    def from_congress_api(cls, row: dict[str, Any]) -> Self:
        """Project one ``/.../members`` ``results`` row into a :class:`VotePosition`.

        Tolerates the API's casing slop (``bioguideID`` vs ``bioguideId``).
        Raises ``ValueError`` if the row lacks a Bioguide ID — the API has
        occasionally emitted placeholder entries for vacant seats, and those
        cannot be linked. Other missing/wrong-type fields surface as
        ``ValidationError`` from Pydantic.
        """
        bioguide = row.get("bioguideID") or row.get("bioguideId")
        if not bioguide:
            raise ValueError(f"vote position row missing bioguide: {row!r}")
        return cls(
            bioguide_id=bioguide,
            position=row["voteCast"],
            vote_party=row["voteParty"],
            vote_state=row["voteState"],
        )


class HouseVoteMembers(BaseModel):
    """Wire shape of the ``/v3/house-vote/{c}/{s}/{r}/members`` response wrapper.

    The endpoint returns ``{"results": [<row>, ...]}`` (plus paging
    metadata the loader ignores). Per-row parsing happens in the loader
    via :meth:`VotePosition.from_congress_api` so per-vote logging context
    (``vote_id``) is preserved when a row is malformed — eager validation
    of every row at envelope-parse time would lose that context.
    """

    model_config = ConfigDict(extra="ignore")

    results: list[dict[str, Any]]

    @classmethod
    def from_congress_api(cls, payload: dict[str, Any]) -> Self:
        """Validate the response-wrapper shape; defer per-row parsing to the loader."""
        results = payload.get("results")
        if not isinstance(results, list):
            raise TypeError(f"house-vote members payload missing 'results' list: {payload!r}")
        return cls(results=results)


class SenateVotePosition(BaseModel):
    """Wire shape of one ``<member>`` row in senate.gov LIS detail XML.

    Carries ``member_full`` (the bridge string) and ``lis_member_id``
    instead of ``bioguide_id`` — the Senate XML keys positions by LIS
    member ID, and the load step resolves to a Bioguide ID via the
    senators_cfm roster + Phase 1 ``members`` table. Constructed by
    :meth:`SenateVoteDetail.from_senate_xml`; projects to
    :class:`VotePosition` once bridged.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    member_full: str
    last_name: str | None = None
    first_name: str | None = None
    party: str | None = None
    state: str | None = None
    vote_cast: str
    lis_member_id: str | None = None


class SenateVoteDetail(BaseModel):
    """Wire shape of one senate.gov LIS per-roll detail XML file.

    URL: ``https://www.senate.gov/legislative/LIS/roll_call_votes/.../vote_{c}_{s}_{roll5}.xml``.
    Constructed by :meth:`from_senate_xml` (defined in :mod:`concord.senate_xml`
    to keep the lxml-flavored parser logic local; the model declares the
    schema and the classmethod stub here, the parser implements it).

    Mirrors :class:`Vote`'s shape but carries an unresolved ``positions``
    list (each entry keyed by ``member_full``, not ``bioguide_id``). The
    loader iterates ``positions`` and resolves each entry to a Bioguide
    ID before persisting. Per ADR 0018 Rule 3, this is the wire-shape
    model that projects to the canonical :class:`Vote` domain model.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    vote_id: str
    chamber: Chamber
    congress: int
    session: SessionNumber
    roll_number: int
    vote_kind: VoteKind
    start_date: str
    update_date: str
    vote_question: str
    vote_type: str
    vote_title: str
    threshold: VoteThreshold | None = None
    result: str
    yea_count: int | None = None
    nay_count: int | None = None
    present_count: int | None = None
    not_voting_count: int | None = None
    bill_id: str | None = None
    amendment_id: str | None = None
    positions: list[SenateVotePosition]

    @field_validator("chamber", mode="before")
    @classmethod
    def _coerce_chamber(cls, value: Any) -> Any:
        return normalize_chamber(value)

    @classmethod
    def from_senate_xml(cls, xml_bytes: bytes) -> Self:
        """Parse one senate.gov LIS detail XML into a :class:`SenateVoteDetail`.

        Defers to :func:`concord.senate_xml.parse_vote_detail` for the
        actual XML walking (where the parser utilities live). Kept as a
        classmethod stub here so the wire-shape model owns its
        constructor's name and signature per ADR 0018 Rule 2.
        """
        # Local import to break the otherwise-circular models ↔ senate_xml dep.
        from concord.senate_xml import parse_vote_detail  # noqa: PLC0415

        return parse_vote_detail(xml_bytes)  # type: ignore[return-value]


def _extract_vote_party_totals(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull the ``votePartyTotal`` array, tolerating both list-shaped and
    `{"item": [...]}` shaped responses (the spike found both)."""
    raw = payload.get("votePartyTotal")
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    if isinstance(raw, dict):
        inner = raw.get("item")
        if isinstance(inner, list):
            return [r for r in inner if isinstance(r, dict)]
    return []


def _is_election_vote(party_totals: list[dict[str, Any]]) -> bool:
    """Detect election-vote shape: any party-total entry carries `candidate`."""
    return any("candidate" in entry for entry in party_totals)


__all__ = [
    "HouseVoteMembers",
    "SenateVoteDetail",
    "SenateVotePosition",
    "Vote",
    "VoteKind",
    "VotePosition",
    "VoteThreshold",
    "amendment_id_from_components",
    "parse_vote_threshold",
    "vote_id_from_components",
]
