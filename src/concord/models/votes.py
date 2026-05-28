"""Vote-related models (Phase 3a).

- :class:`Vote` — the canonical row written to the ``votes`` SQLite table.
- :class:`VotePosition` — one Member's recorded position on one Vote.
- :class:`VoteSnapshot` / :class:`VotePositionsSnapshot` — ADR 0006 envelopes.
- :class:`ParsedVotePosition` / :class:`ParsedVoteDetail` — Senate XML
  intermediates carried by :mod:`concord.senate_xml` until the loader
  resolves their ``member_full`` bridge strings to Bioguide IDs.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from concord.models._common import Chamber, SessionNumber, coerce_int, normalize_chamber
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


def parse_vote_threshold(vote_type: str | None) -> VoteThreshold | None:
    """Map the API's free-text ``voteType`` to a normalized threshold code.

    Case-insensitive substring matches against the strings the spike
    found in House data: ``"2/3 Yea-And-Nay"`` → ``'two_thirds'``,
    ``"3/5"`` → ``'three_fifths'``, ``"Yea-and-Nay"`` / ``"Recorded
    Vote"`` / ``"Quorum"`` → ``'simple_majority'``. Anything else
    returns ``None`` and the loader leaves the column NULL.
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
    """One recorded roll-call decision in a chamber.

    Identified by ``(chamber, congress, session, roll_number)``; the
    flattened ``vote_id`` is the SQL primary key. ``bill_id`` and
    ``amendment_id`` are bare TEXT references (no FK) so ingest is
    robust to gaps in the Bills / Amendments tables.

    ``is_party_unity`` defaults to False; the indexer (Stage 2)
    populates it across all rows.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    vote_id: str
    chamber: Chamber
    congress: int
    session: SessionNumber
    roll_number: int
    vote_kind: VoteKind = "standard"
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
    is_party_unity: bool = False
    update_date: str

    @field_validator("chamber", mode="before")
    @classmethod
    def _coerce_chamber(cls, value: Any) -> Any:
        return normalize_chamber(value)

    @classmethod
    def from_congress_api(cls, payload: dict[str, Any]) -> "Vote":
        """Project a ``/v3/house-vote/{c}/{s}/{roll}`` detail payload into a :class:`Vote`.

        Handles three shape quirks the spike surfaced:

        1. ``votePartyTotal`` arrives as a list on some payloads and as
           ``{"item": [...]}`` on others — :func:`_extract_vote_party_totals`
           normalizes the two.
        2. Election votes (Speaker elections, etc.) bucket totals by
           candidate name rather than by party; in that shape we leave the
           count columns NULL and set ``vote_kind='election'``.
        3. Amendment votes carry both the amendment's identity *and* the
           underlying bill in ``legislationType`` + ``legislationNumber``
           — the "amendment trap". Both `bill_id` and `amendment_id` get
           populated for that case.

        Raises ``ValidationError`` / ``ValueError`` on a malformed payload
        (missing required ids, missing ``updateDate`` / ``startDate``, …).
        """
        chamber = normalize_chamber(payload.get("chamber") or "house")
        congress = int(payload["congress"])
        session = int(payload["sessionNumber"])
        roll_number = int(payload["rollCallNumber"])

        party_totals = _extract_vote_party_totals(payload)
        is_election = _is_election_vote(party_totals)

        bill_id = None
        leg_type = payload.get("legislationType")
        leg_number = payload.get("legislationNumber")
        if leg_type and leg_number is not None:
            try:
                bill_id = bill_id_from_components(congress, str(leg_type), int(leg_number))
            except (TypeError, ValueError):
                bill_id = None

        amendment_id = None
        amd_type = payload.get("amendmentType")
        amd_number = payload.get("amendmentNumber")
        if amd_type and amd_number is not None:
            try:
                amendment_id = amendment_id_from_components(
                    congress, str(amd_type), int(amd_number)
                )
            except (TypeError, ValueError):
                amendment_id = None

        yea = nay = present = not_voting = None
        if not is_election:
            yea = 0
            nay = 0
            present = 0
            not_voting = 0
            for entry in party_totals:
                yea += coerce_int(entry.get("yeaTotal")) or 0
                nay += coerce_int(entry.get("nayTotal")) or 0
                present += coerce_int(entry.get("presentTotal")) or 0
                not_voting += coerce_int(entry.get("notVotingTotal")) or 0

        vote_type_raw = str(payload.get("voteType") or "")
        threshold = parse_vote_threshold(vote_type_raw)

        update_date_raw = payload.get("updateDate") or payload.get("startDate")
        if not update_date_raw:
            raise ValueError(f"vote payload missing updateDate: {payload!r}")

        start_date_raw = payload.get("startDate") or payload.get("date")
        if not start_date_raw:
            raise ValueError(f"vote payload missing startDate: {payload!r}")

        return cls(
            vote_id=vote_id_from_components(chamber, congress, session, roll_number),
            chamber=chamber,
            congress=congress,
            session=session,  # type: ignore[arg-type]
            roll_number=roll_number,
            vote_kind="election" if is_election else "standard",
            start_date=str(start_date_raw),
            vote_question=str(payload.get("voteQuestion") or ""),
            vote_type=vote_type_raw,
            threshold=threshold,
            result=str(payload.get("result") or ""),
            yea_count=yea,
            nay_count=nay,
            present_count=present,
            not_voting_count=not_voting,
            bill_id=bill_id,
            amendment_id=amendment_id,
            is_party_unity=False,
            update_date=str(update_date_raw),
        )


class VotePosition(BaseModel):
    """One Member's recorded position on one Vote.

    ``position`` is free-text rather than an enum: for standard votes
    it's ``"Yea"`` / ``"Nay"`` / ``"Present"`` / ``"Not Voting"``; for
    Speaker-election rolls it's a candidate's surname. The spike confirmed
    the API uses both shapes interchangeably under the same field name.

    ``vote_party`` and ``vote_state`` are denormalized off the API
    payload so party-unity computation doesn't have to join `terms`.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    bioguide_id: str
    position: str
    vote_party: str | None = None
    vote_state: str | None = None

    @classmethod
    def from_congress_api(cls, row: dict[str, Any]) -> "VotePosition":
        """Project one ``/.../members`` ``results`` row into a :class:`VotePosition`.

        Tolerates the API's casing slop (``bioguideID`` vs ``bioguideId``).
        Raises ``ValueError`` if the row lacks a Bioguide ID or ``voteCast``
        — the API has occasionally emitted placeholder entries for vacant
        seats, and those cannot be linked.
        """
        bioguide = row.get("bioguideID") or row.get("bioguideId")
        if not isinstance(bioguide, str) or not bioguide:
            raise ValueError(f"vote position row missing bioguide: {row!r}")
        position = row.get("voteCast")
        if not isinstance(position, str) or not position:
            raise ValueError(f"vote position row missing voteCast: {row!r}")
        return cls(
            bioguide_id=bioguide,
            position=position,
            vote_party=row.get("voteParty"),
            vote_state=row.get("voteState"),
        )


class VoteSnapshot(BaseModel):
    """ADR 0006 envelope wrapping one ``/house-vote/{c}/{s}/{roll}`` response.

    Stage 0 appends one of these per detail fetch to
    ``data/house_votes.jsonl``. The Stage 1 loader groups by
    ``(chamber, congress, session, roll_number)`` and keeps the latest
    ``fetched_at`` per key.
    """

    model_config = ConfigDict(extra="ignore")

    fetched_at: datetime
    key: dict[str, str | int]
    payload: dict[str, Any]


class VotePositionsSnapshot(BaseModel):
    """ADR 0006 envelope wrapping one ``/.../members`` response.

    Stage 0 appends one of these per members fetch to
    ``data/house_vote_positions.jsonl``. Same `key` shape as
    :class:`VoteSnapshot` so the loader joins them by key.
    """

    model_config = ConfigDict(extra="ignore")

    fetched_at: datetime
    key: dict[str, str | int]
    payload: dict[str, Any]


class ParsedVotePosition(BaseModel):
    """One Senate-detail-XML ``<member>`` row, ready for the loader.

    Carries ``member_full`` (the bridge string) and ``lis_member_id``
    instead of ``bioguide_id`` — the Senate XML keys positions by LIS
    member ID, and the load step resolves to a Bioguide ID via the
    senators_cfm roster + Phase 1 ``members`` table.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    member_full: str
    last_name: str | None = None
    first_name: str | None = None
    party: str | None = None
    state: str | None = None
    vote_cast: str
    lis_member_id: str | None = None


class ParsedVoteDetail(BaseModel):
    """One Senate-detail-XML payload, parsed into typed loader input.

    Mirrors :class:`Vote`'s shape but carries an unresolved
    ``positions`` list (each entry keyed by ``member_full``, not
    ``bioguide_id``). The loader iterates ``positions`` and resolves
    each entry to a Bioguide ID before persisting.

    Distinct from :class:`Vote` because the bridge resolution happens
    at load time, not parse time — the parser doesn't have access to
    the SQLite ``members`` table or the senators_cfm roster.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    vote_id: str
    chamber: Chamber
    congress: int
    session: SessionNumber
    roll_number: int
    vote_kind: VoteKind = "standard"
    start_date: str
    update_date: str
    vote_question: str
    vote_type: str
    vote_title: str = ""
    threshold: VoteThreshold | None = None
    result: str
    yea_count: int | None = None
    nay_count: int | None = None
    present_count: int | None = None
    not_voting_count: int | None = None
    bill_id: str | None = None
    amendment_id: str | None = None
    positions: list[ParsedVotePosition] = []

    @field_validator("chamber", mode="before")
    @classmethod
    def _coerce_chamber(cls, value: Any) -> Any:
        return normalize_chamber(value)


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
    "ParsedVoteDetail",
    "ParsedVotePosition",
    "Vote",
    "VoteKind",
    "VotePosition",
    "VotePositionsSnapshot",
    "VoteSnapshot",
    "VoteThreshold",
    "amendment_id_from_components",
    "parse_vote_threshold",
    "vote_id_from_components",
]
