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
    def from_congress_api(cls, payload: dict[str, Any], *, chamber: str) -> "Vote":
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
    vote_party: str
    vote_state: str

    @classmethod
    def from_congress_api(cls, row: dict[str, Any]) -> "VotePosition":
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
    positions: list[ParsedVotePosition]

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
