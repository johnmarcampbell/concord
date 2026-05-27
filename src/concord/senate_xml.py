"""HTTP client + parsers for senate.gov LIS XML feeds.

Phase 3b's data source. Unlike Phase 3a's House votes (which come from
api.congress.gov as parsed JSON), Senate votes come from senate.gov's
publicly-exposed Legislative Information System (LIS) XML feeds. Three
distinct URLs:

- Menu XML — one file per ``(congress, session)``; lists every roll
  number plus a summary block. Used for discovery only; not persisted.
- Detail XML — one file per roll call; carries chamber totals + the
  full per-member position roster in a single document.
- Roster XML (``senators_cfm.xml``) — the current sitting Senate
  roster; the LIS↔Bioguide bridge for the vote-position loader.

This module owns the HTTP client (:class:`SenateClient`) and the
parsing helpers (:func:`parse_vote_menu`, :func:`parse_vote_detail`,
:func:`parse_senate_roster`). The parsers return typed pydantic models
(:class:`ParsedVoteDetail`, :class:`ParsedVotePosition`) — see
:mod:`concord.models`.

Senate timestamps in the detail XML are wall-clock ET without a
timezone offset; :func:`_parse_senate_date` localizes them via
``zoneinfo.ZoneInfo("America/New_York")`` and formats as ISO 8601 with
offset.
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime
from types import TracebackType
from zoneinfo import ZoneInfo

import httpx

from . import __version__
from .models import (
    ParsedVoteDetail,
    ParsedVotePosition,
    amendment_id_from_components,
    bill_id_from_components,
    vote_id_from_components,
)

_log = logging.getLogger("concord.senate_xml")

USER_AGENT = f"concord/{__version__} (+https://github.com/johnmarcampbell/concord)"

#: Base URL for senate.gov roll-call LIS feeds.
SENATE_LIS_BASE = "https://www.senate.gov/legislative/LIS"

#: Menu XML URL template. One file per (congress, session) slot.
MENU_URL = f"{SENATE_LIS_BASE}/roll_call_lists/vote_menu_{{congress}}_{{session}}.xml"

#: Detail XML URL template. ``roll`` is zero-padded to five digits.
DETAIL_URL = (
    f"{SENATE_LIS_BASE}/roll_call_votes/vote{{congress}}{{session}}"
    f"/vote_{{congress}}_{{session}}_{{roll5}}.xml"
)

#: Current-sitting roster XML.
ROSTER_URL = "https://www.senate.gov/general/contact_information/senators_cfm.xml"

#: Retry policy: total attempts for transport errors / transient 5xx.
MAX_RETRIES = 3

#: Exponential backoff base.
_BACKOFF_BASE = 2.0

#: Inter-request padding for detail fetches (defensive only).
DETAIL_REQUEST_SLEEP_SECONDS = 0.1

#: HTTP status codes treated specially. Same idiom as :mod:`concord.api`.
HTTP_OK = 200
HTTP_SERVER_ERROR_MIN = 500
HTTP_SERVER_ERROR_MAX = 600  # exclusive upper bound

#: Senate XML document-type codes that map onto Concord ``bill_type`` codes.
_BILL_TYPE_MAP: dict[str, str] = {
    "S.": "s",
    "S.J.Res.": "sjres",
    "S.Res.": "sres",
    "S.Con.Res.": "sconres",
    "H.R.": "hr",
    "H.J.Res.": "hjres",
    "H.Res.": "hres",
    "H.Con.Res.": "hconres",
}

#: Eastern Time zone — Senate timestamps are wall-clock ET.
_ET = ZoneInfo("America/New_York")

#: Senate detail XML's ``majority_requirement`` → Concord threshold code.
_THRESHOLD_MAP: dict[str, str] = {
    "1/2": "simple_majority",
    "3/5": "three_fifths",
    "2/3": "two_thirds",
}


class SenateXmlError(Exception):
    """Raised when senate.gov returns a non-XML response or a transport error."""


class SenateClient:
    """HTTP client over senate.gov LIS XML feeds.

    Owns an :class:`httpx.Client`; pass a custom ``transport`` (e.g.
    :class:`httpx.MockTransport`) to intercept requests in tests.

    Senate.gov has no documented rate limit and has shown no 429s in
    spike testing; this client uses a small inter-request padding only.
    HTML responses (the "404-disguised-as-200" trap senate.gov sets for
    missing roll-call files) are detected via Content-Type and raise
    :class:`SenateXmlError`.
    """

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        user_agent: str = USER_AGENT,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._sleep = sleep
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout,
            headers={"User-Agent": user_agent},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SenateClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- endpoints --------------------------------------------------------

    def get_current_senators_xml(self) -> bytes:
        """Fetch ``senators_cfm.xml`` (the current Senate roster)."""
        return self._get_xml(ROSTER_URL)

    def list_roll_call_numbers(self, congress: int, session: int) -> list[int]:
        """Return all roll-call numbers for a ``(congress, session)`` slot.

        Discovery-only: the menu XML is fetched, parsed, and discarded.
        Returns roll numbers sorted ascending (oldest first) so a scrape
        produces a stable left-to-right walk.
        """
        url = MENU_URL.format(congress=congress, session=session)
        return parse_vote_menu(self._get_xml(url))

    def get_roll_call_xml(
        self,
        congress: int,
        session: int,
        roll_number: int,
    ) -> bytes:
        """Fetch one detail XML file. ``roll_number`` is zero-padded."""
        url = DETAIL_URL.format(
            congress=congress,
            session=session,
            roll5=f"{int(roll_number):05d}",
        )
        return self._get_xml(url)

    # -- internals --------------------------------------------------------

    def _get_xml(self, url: str) -> bytes:
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                response = self._client.get(url)
            except httpx.HTTPError as exc:
                last_exc = exc
                _log.warning(
                    "senate.gov transport error (attempt %d/%d) at %s: %s",
                    attempt + 1,
                    MAX_RETRIES,
                    url,
                    exc,
                )
            else:
                status = response.status_code
                if HTTP_SERVER_ERROR_MIN <= status < HTTP_SERVER_ERROR_MAX:
                    last_exc = SenateXmlError(f"senate.gov returned {status} for {url}")
                    _log.warning(
                        "senate.gov %d (attempt %d/%d) at %s",
                        status,
                        attempt + 1,
                        MAX_RETRIES,
                        url,
                    )
                elif status != HTTP_OK:
                    raise SenateXmlError(f"senate.gov returned {status} for {url}")
                else:
                    _check_xml_content_type(response, url)
                    return response.content
            if attempt < MAX_RETRIES - 1:
                self._sleep(_BACKOFF_BASE**attempt)
        assert last_exc is not None  # noqa: S101
        raise SenateXmlError(f"senate.gov failed after {MAX_RETRIES} attempts at {url}: {last_exc}")


def _check_xml_content_type(response: httpx.Response, url: str) -> None:
    """Reject HTML responses — senate.gov returns these for missing files."""
    content_type = response.headers.get("Content-Type", "").lower()
    if "text/html" in content_type:
        raise SenateXmlError(f"got HTML response, expected XML, at {url}")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_vote_menu(xml_bytes: bytes) -> list[int]:
    """Return roll numbers from ``vote_menu_{c}_{s}.xml``, sorted ascending.

    The menu XML returns newest-first per the spike; this helper sorts
    ascending so scrape walks are stable left-to-right.
    """
    root = _parse(xml_bytes)
    numbers: list[int] = []
    for vote in root.iter("vote"):
        raw = (vote.findtext("vote_number") or "").strip()
        if not raw:
            _log.warning("vote_menu: skipping entry with empty vote_number")
            continue
        try:
            numbers.append(int(raw))
        except ValueError:
            _log.warning("vote_menu: skipping non-integer vote_number %r", raw)
    numbers.sort()
    return numbers


def parse_senate_roster(xml_bytes: bytes) -> dict[str, str]:
    """Parse ``senators_cfm.xml`` → ``{member_full: bioguide_id}``.

    The roster's ``<member_full>`` element is exactly the format used in
    Senate vote-detail XML (``"Surname (P-ST)"``), so it can be used
    directly as the bridge key.
    """
    root = _parse(xml_bytes)
    bridge: dict[str, str] = {}
    for member in root.iter("member"):
        member_full = (member.findtext("member_full") or "").strip()
        bioguide = (member.findtext("bioguide_id") or "").strip()
        if not member_full or not bioguide:
            _log.warning(
                "senators_cfm: skipping entry (member_full=%r bioguide=%r)",
                member_full,
                bioguide,
            )
            continue
        bridge[member_full] = bioguide
    return bridge


def parse_vote_detail(xml_bytes: bytes) -> ParsedVoteDetail:
    """Parse one detail XML file into a typed :class:`ParsedVoteDetail`.

    Implements the subject-branching documented in
    ``docs/plans/phase-3b-votes-senate.md`` — amendment votes precede
    bill votes; document types outside the known bill-type set
    (``"PN"`` for nominations, treaty types) drop both FK columns.

    En-bloc detection: when ``<en_bloc>`` is present and ``<question>``
    is empty, the row's ``vote_question`` is taken from
    ``<vote_title>`` and both subject FKs are NULL. The per-matter
    ``<en_bloc><matter>`` breakdown is preserved in the raw XML payload
    but not surfaced on the SQLite row in this phase.
    """
    root = _parse(xml_bytes)

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
    threshold = _THRESHOLD_MAP.get(majority_req)
    if majority_req and threshold is None:
        _log.warning(
            "unknown majority_requirement %r at %d/%d/%d",
            majority_req,
            congress,
            session,
            roll_number,
        )

    yea_count = _to_int(root.findtext("count/yeas"))
    nay_count = _to_int(root.findtext("count/nays"))
    present_count = _to_int(root.findtext("count/present"))
    not_voting_count = _to_int(root.findtext("count/absent"))

    is_en_bloc = root.find("en_bloc") is not None and not vote_type
    bill_id, amendment_id = _resolve_subject(root, congress, is_en_bloc)

    # When the vote's subject doesn't land on a Bill or Amendment row,
    # ``vote_title`` carries the only human-readable identity in the
    # XML (nominee name, treaty title, en-bloc batch label). Prefer it
    # over the short ``vote_question_text`` so the profile page renders
    # something meaningful instead of "Procedural — no bill or
    # amendment subject."
    if bill_id is None and amendment_id is None and vote_title:
        vote_question = vote_title

    positions = list(_iter_positions(root))

    return ParsedVoteDetail(
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


def _resolve_subject(
    root: ET.Element,
    congress: int,
    is_en_bloc: bool,
) -> tuple[str | None, str | None]:
    """Apply subject branching for the detail XML.

    Returns ``(bill_id, amendment_id)``. En-bloc rolls always return
    ``(None, None)`` — their identity lives in ``vote_title``.
    """
    if is_en_bloc:
        return None, None

    amendment_number_raw = (root.findtext("amendment/amendment_number") or "").strip()
    if amendment_number_raw:
        amendment_id = _build_amendment_id_from_xml(congress, amendment_number_raw)
        bill_id = _build_bill_id_from_amendment_target(
            congress,
            (root.findtext("amendment/amendment_to_document_number") or "").strip(),
        )
        return bill_id, amendment_id

    doc_type = (root.findtext("document/document_type") or "").strip()
    doc_number = (root.findtext("document/document_number") or "").strip()
    bill_id = _build_bill_id_from_xml(congress, doc_type, doc_number)
    return bill_id, None


def _iter_positions(root: ET.Element):
    for member in root.iterfind("members/member"):
        member_full = (member.findtext("member_full") or "").strip()
        vote_cast = (member.findtext("vote_cast") or "").strip()
        if not member_full or not vote_cast:
            continue
        yield ParsedVotePosition(
            member_full=member_full,
            last_name=(member.findtext("last_name") or "").strip() or None,
            first_name=(member.findtext("first_name") or "").strip() or None,
            party=(member.findtext("party") or "").strip() or None,
            state=(member.findtext("state") or "").strip() or None,
            vote_cast=vote_cast,
            lis_member_id=(member.findtext("lis_member_id") or "").strip() or None,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse(xml_bytes: bytes) -> ET.Element:
    try:
        return ET.fromstring(xml_bytes)  # noqa: S314 — senate.gov XML is trusted (no DTD, no external entities)
    except ET.ParseError as exc:
        raise SenateXmlError(f"malformed XML: {exc}") from exc


def _to_int(text: str | None) -> int | None:
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
    aware = naive.replace(tzinfo=_ET)
    return aware.isoformat()


def _build_amendment_id_from_xml(congress: int, amendment_number_text: str) -> str | None:
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


def _build_bill_id_from_xml(
    congress: int,
    document_type: str,
    document_number: str,
) -> str | None:
    """Canonicalize the senate.gov XML ``document_type`` to a Concord bill_id.

    Returns ``None`` for ``PN`` (Presidential Nominations), treaty codes,
    or any other type not in :data:`_BILL_TYPE_MAP`.
    """
    if not document_type or not document_number:
        return None
    bill_type = _BILL_TYPE_MAP.get(document_type.strip())
    if bill_type is None:
        return None
    try:
        number = int(document_number.strip())
    except ValueError:
        return None
    return bill_id_from_components(congress, bill_type, number)


def _build_bill_id_from_amendment_target(
    congress: int,
    target_text: str,
) -> str | None:
    """Parse the amendment's ``amendment_to_document_number`` (e.g. ``"S. 5"``).

    The senate.gov amendment block uses a single combined "type + number"
    string for the underlying bill rather than two separate fields. This
    helper splits on whitespace and routes through
    :func:`_build_bill_id_from_xml`.
    """
    if not target_text:
        return None
    tokens = target_text.strip().split()
    if len(tokens) < 2:  # noqa: PLR2004 — split form is "<type> <number>"
        return None
    return _build_bill_id_from_xml(congress, tokens[0], tokens[-1])


__all__ = [
    "DETAIL_REQUEST_SLEEP_SECONDS",
    "DETAIL_URL",
    "MENU_URL",
    "ROSTER_URL",
    "USER_AGENT",
    "SenateClient",
    "SenateXmlError",
    "parse_senate_roster",
    "parse_vote_detail",
    "parse_vote_menu",
]
