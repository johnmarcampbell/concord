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

import logging
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from datetime import datetime
from typing import Any, Literal, Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from concord.errors import SenateXmlError
from concord.models._common import Chamber, SessionNumber, normalize_chamber
from concord.models.bills import bill_id_from_components

_log = logging.getLogger("concord.models.votes")

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
        # Canonicalize chamber inline (per ADR 0018 Rule 3 — no
        # @field_validator semantic shims on the wire-shape model).
        chamber_canonical = normalize_chamber(chamber)

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
            vote_id=vote_id_from_components(chamber_canonical, congress, session, roll_number),
            chamber=chamber_canonical,
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
    Constructed by :meth:`from_senate_xml`, which owns the XML walk and
    field projection in one place per ADR 0018 Rule 2.

    Mirrors :class:`Vote`'s shape but carries an unresolved ``positions``
    list (each entry keyed by ``member_full``, not ``bioguide_id``). The
    loader iterates ``positions`` and resolves each entry to a Bioguide
    ID before persisting. Per ADR 0018 Rule 3, this is the wire-shape
    model that projects to the canonical :class:`Vote` domain model.

    ``chamber`` is always ``"senate"`` — the class only parses senate.gov
    XML, so no normalization shim is needed (and none is allowed on a
    wire-shape model per ADR 0018 Rule 3).
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

    @classmethod
    def from_senate_xml(cls, xml_bytes: bytes) -> Self:
        """Parse one senate.gov LIS detail XML into a :class:`SenateVoteDetail`.

        Implements the subject-branching documented in
        ``docs/plans/phase-3b-votes-senate.md`` — amendment votes precede
        bill votes; document types outside the known bill-type set
        (``"PN"`` for nominations, treaty types) drop both FK columns.

        En-bloc detection: when ``<en_bloc>`` is present and ``<question>``
        is empty, the row's ``vote_question`` is taken from
        ``<vote_title>`` and both subject FKs are NULL. The per-matter
        ``<en_bloc><matter>`` breakdown is preserved in the raw XML payload
        but not surfaced on the SQLite row in this phase.

        Raises :exc:`concord.errors.SenateXmlError` for malformed XML
        or missing required date fields; raises ``pydantic.ValidationError``
        for any field that doesn't match the model's schema.
        """
        try:
            root = ET.fromstring(xml_bytes)  # noqa: S314 — senate.gov XML is trusted (no DTD, no external entities)
        except ET.ParseError as exc:
            raise SenateXmlError(f"malformed XML: {exc}") from exc

        congress = int((root.findtext("congress") or "0").strip())
        session = int((root.findtext("session") or "0").strip())
        roll_number = int((root.findtext("vote_number") or "0").strip())

        start_date = _parse_senate_date(root.findtext("vote_date"))
        update_date = _parse_senate_date(root.findtext("modify_date")) or start_date
        if not start_date or not update_date:
            raise SenateXmlError(
                f"detail XML missing vote_date/modify_date for {congress}/{session}/{roll_number}"
            )

        vote_question = (root.findtext("vote_question_text") or "").strip()
        vote_type = (root.findtext("question") or "").strip()
        vote_title = (root.findtext("vote_title") or "").strip()
        result = (root.findtext("vote_result") or "").strip()
        majority_req = (root.findtext("majority_requirement") or "").strip()
        threshold = _SENATE_THRESHOLD_MAP.get(majority_req)
        if majority_req and threshold is None:
            _log.warning(
                "unknown majority_requirement %r at %d/%d/%d",
                majority_req,
                congress,
                session,
                roll_number,
            )

        yea_count = _xml_text_to_int(root.findtext("count/yeas"))
        nay_count = _xml_text_to_int(root.findtext("count/nays"))
        present_count = _xml_text_to_int(root.findtext("count/present"))
        not_voting_count = _xml_text_to_int(root.findtext("count/absent"))

        is_en_bloc = root.find("en_bloc") is not None and not vote_type
        bill_id, amendment_id = _resolve_senate_subject(root, congress, is_en_bloc)

        # When the vote's subject doesn't land on a Bill or Amendment row,
        # ``vote_title`` carries the only human-readable identity in the
        # XML (nominee name, treaty title, en-bloc batch label). Prefer it
        # over the short ``vote_question_text`` so the profile page renders
        # something meaningful instead of "Procedural — no bill or
        # amendment subject."
        if bill_id is None and amendment_id is None and vote_title:
            vote_question = vote_title

        positions = list(_iter_senate_positions(root))

        return cls(
            vote_id=vote_id_from_components("senate", congress, session, roll_number),
            chamber="senate",
            congress=congress,
            session=session,  # type: ignore[arg-type]
            roll_number=roll_number,
            vote_kind="standard",
            start_date=start_date,
            update_date=update_date,
            vote_question=vote_question,
            vote_type=vote_type,
            vote_title=vote_title,
            threshold=threshold,  # type: ignore[arg-type]
            result=result,
            yea_count=yea_count,
            nay_count=nay_count,
            present_count=present_count,
            not_voting_count=not_voting_count,
            bill_id=bill_id,
            amendment_id=amendment_id,
            positions=positions,
        )


# ---------------------------------------------------------------------------
# senate.gov LIS detail-XML helpers — collocated with SenateVoteDetail per
# ADR 0018 Rule 2 so the factory method owns the parsing logic in one place.
# ---------------------------------------------------------------------------

#: Senate timestamps are wall-clock ET (no offset in the XML).
_ET_ZONE = ZoneInfo("America/New_York")

#: Senate detail XML's ``majority_requirement`` text → Concord threshold code.
_SENATE_THRESHOLD_MAP: dict[str, str] = {
    "1/2": "simple_majority",
    "3/5": "three_fifths",
    "2/3": "two_thirds",
}

#: senate.gov XML ``document_type`` strings → Concord ``bill_type`` codes.
_SENATE_BILL_TYPE_MAP: dict[str, str] = {
    "S.": "s",
    "S.J.Res.": "sjres",
    "S.Res.": "sres",
    "S.Con.Res.": "sconres",
    "H.R.": "hr",
    "H.J.Res.": "hjres",
    "H.Res.": "hres",
    "H.Con.Res.": "hconres",
}


def _xml_text_to_int(text: str | None) -> int | None:
    """Best-effort int conversion for XML element text; ``None`` on miss."""
    if text is None:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _parse_senate_date(text: str | None) -> str | None:
    """Parse Senate wall-clock ET timestamps into ISO 8601 with offset.

    Senate timestamps look like ``"January 20, 2025,  06:12 PM"`` — note
    the double space before the time. Returns ``None`` for missing /
    unparseable input rather than raising; callers detect missing
    ``start_date`` separately.
    """
    if text is None:
        return None
    raw = text.strip()
    if not raw:
        return None
    # Collapse repeated whitespace introduced by senate.gov's templating.
    normalized = " ".join(raw.split())
    try:
        naive = datetime.strptime(normalized, "%B %d, %Y, %I:%M %p")
    except ValueError as exc:
        _log.warning("could not parse senate timestamp %r: %s", raw, exc)
        return None
    aware = naive.replace(tzinfo=_ET_ZONE)
    return aware.isoformat()


def _resolve_senate_subject(
    root: ET.Element,
    congress: int,
    is_en_bloc: bool,
) -> tuple[str | None, str | None]:
    """Apply subject branching for the senate detail XML.

    Returns ``(bill_id, amendment_id)``. En-bloc rolls always return
    ``(None, None)`` — their identity lives in ``vote_title``.
    """
    if is_en_bloc:
        return None, None

    amendment_number_raw = (root.findtext("amendment/amendment_number") or "").strip()
    if amendment_number_raw:
        amendment_id = _build_senate_amendment_id(congress, amendment_number_raw)
        bill_id = _build_senate_bill_id_from_amendment_target(
            congress,
            (root.findtext("amendment/amendment_to_document_number") or "").strip(),
        )
        return bill_id, amendment_id

    doc_type = (root.findtext("document/document_type") or "").strip()
    doc_number = (root.findtext("document/document_number") or "").strip()
    bill_id = _build_senate_bill_id(congress, doc_type, doc_number)
    return bill_id, None


def _iter_senate_positions(root: ET.Element) -> Iterator[SenateVotePosition]:
    for member in root.iterfind("members/member"):
        member_full = (member.findtext("member_full") or "").strip()
        vote_cast = (member.findtext("vote_cast") or "").strip()
        if not member_full or not vote_cast:
            continue
        yield SenateVotePosition(
            member_full=member_full,
            last_name=(member.findtext("last_name") or "").strip() or None,
            first_name=(member.findtext("first_name") or "").strip() or None,
            party=(member.findtext("party") or "").strip() or None,
            state=(member.findtext("state") or "").strip() or None,
            vote_cast=vote_cast,
            lis_member_id=(member.findtext("lis_member_id") or "").strip() or None,
        )


def _build_senate_amendment_id(congress: int, amendment_number_text: str) -> str | None:
    """Parse the XML form ``"S.Amdt. 14"`` → ``"119-samdt-14"``."""
    parts = amendment_number_text.replace(".", "").split()
    if len(parts) < 2:  # noqa: PLR2004 — splits "S.Amdt. 14" into type + number
        _log.warning("could not parse amendment number %r", amendment_number_text)
        return None
    amendment_type_raw = parts[0].lower()
    if amendment_type_raw.startswith("s") and "amdt" in amendment_type_raw:
        amendment_type = "samdt"
    elif amendment_type_raw.startswith("h") and "amdt" in amendment_type_raw:
        amendment_type = "hamdt"
    else:
        amendment_type = amendment_type_raw
    try:
        number = int(parts[-1])
    except ValueError:
        _log.warning("could not parse amendment number int from %r", amendment_number_text)
        return None
    return amendment_id_from_components(congress, amendment_type, number)


def _build_senate_bill_id(
    congress: int,
    document_type: str,
    document_number: str,
) -> str | None:
    """Canonicalize the senate.gov XML ``document_type`` to a Concord bill_id.

    Returns ``None`` for ``PN`` (Presidential Nominations), treaty codes,
    or any other type not in :data:`_SENATE_BILL_TYPE_MAP`.
    """
    if not document_type or not document_number:
        return None
    bill_type = _SENATE_BILL_TYPE_MAP.get(document_type.strip())
    if bill_type is None:
        return None
    try:
        number = int(document_number.strip())
    except ValueError:
        return None
    return bill_id_from_components(congress, bill_type, number)


def _build_senate_bill_id_from_amendment_target(
    congress: int,
    target_text: str,
) -> str | None:
    """Parse the amendment's ``amendment_to_document_number`` (e.g. ``"S. 5"``).

    The senate.gov amendment block uses a single combined "type + number"
    string for the underlying bill rather than two separate fields. This
    helper splits on whitespace and routes through :func:`_build_senate_bill_id`.
    """
    if not target_text:
        return None
    tokens = target_text.strip().split()
    if len(tokens) < 2:  # noqa: PLR2004 — split form is "<type> <number>"
        return None
    return _build_senate_bill_id(congress, tokens[0], tokens[-1])


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
