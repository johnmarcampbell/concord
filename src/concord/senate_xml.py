"""HTTP client + parsers for senate.gov LIS XML feeds.

Phase 3b's data source. Unlike Phase 3a's House votes (which come from
api.congress.gov as parsed JSON), Senate votes come from senate.gov's
publicly-exposed Legislative Information System (LIS) XML feeds. Three
distinct URLs:

- Menu XML — one file per ``(congress, session)``; lists every roll
  number plus a summary block. Used for discovery only; not persisted.
- Detail XML — one file per roll call; carries chamber totals + the
  full per-member position roster in a single document. Parsed by
  :meth:`concord.models.SenateVoteDetail.from_senate_xml` per ADR 0018 —
  the factory method owns the XML walk so the wire-shape model and its
  parsing logic live together.
- Roster XML (``senators_cfm.xml``) — the current sitting Senate
  roster; the LIS↔Bioguide bridge for the vote-position loader.

This module owns the HTTP client (:class:`SenateClient`) and the two
single-purpose parsers that don't construct models
(:func:`parse_vote_menu` → ``list[int]``, :func:`parse_senate_roster` →
``dict[str, str]``). Detail-XML parsing lives on
:class:`concord.models.SenateVoteDetail` per ADR 0018 Rule 2.
"""

import logging
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from types import TracebackType

import httpx

from concord import __version__
from concord.errors import SenateXmlError
from concord.models.runs import Attempt
from concord.observability import Recorder, active_recorder

_log = logging.getLogger("concord.senate_xml")

USER_AGENT = f"concord/{__version__}"

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

    def __enter__(self) -> "SenateClient":
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
        # Observability (ADR 0021): mirror api.py's chokepoint. ``attempts``
        # accumulates every non-success try (transport failure, 5xx, the
        # non-200/HTML-trap terminal cases) so a resolved-on-retry fetch can
        # report what it weathered. ``rec`` is None outside an active scrape,
        # making the recording a no-op. Retry *behavior* is unchanged.
        rec = active_recorder()
        attempts: list[Attempt] = []

        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                response = self._client.get(url)
            except httpx.HTTPError as exc:
                last_exc = exc
                _note_attempt(attempts, transport_class=type(exc).__name__, message=str(exc))
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
                    _note_attempt(attempts, status=status, message=f"senate.gov returned {status}")
                    _log.warning(
                        "senate.gov %d (attempt %d/%d) at %s",
                        status,
                        attempt + 1,
                        MAX_RETRIES,
                        url,
                    )
                elif status != HTTP_OK:
                    _note_attempt(attempts, status=status, message=f"senate.gov returned {status}")
                    _record_failure(rec, url, attempts)
                    raise SenateXmlError(f"senate.gov returned {status} for {url}")
                else:
                    # 200 OK, but senate.gov serves an HTML error page (not a
                    # 404) for missing roll-call files. That HTML-as-200 trap is
                    # a structural failure even though the status was 200 —
                    # record it before re-raising.
                    try:
                        _check_xml_content_type(response, url)
                    except SenateXmlError:
                        _record_html_trap(rec, url, attempts)
                        raise
                    _record_success(rec, url, attempts)
                    return response.content
            if attempt < MAX_RETRIES - 1:
                self._sleep(_BACKOFF_BASE**attempt)
        _record_failure(rec, url, attempts)
        assert last_exc is not None  # noqa: S101
        raise SenateXmlError(f"senate.gov failed after {MAX_RETRIES} attempts at {url}: {last_exc}")


def _check_xml_content_type(response: httpx.Response, url: str) -> None:
    """Reject HTML responses — senate.gov returns these for missing files."""
    content_type = response.headers.get("Content-Type", "").lower()
    if "text/html" in content_type:
        raise SenateXmlError(f"got HTML response, expected XML, at {url}")


# -- observability helpers --------------------------------------------------
#
# Module-level (rather than inline blocks) so each recording stays one
# statement in the retry loop. The route table maps the full senate.gov URL to
# the right ``senate:*`` bucket, so callers pass ``"senate"`` + the URL.

#: Synthetic ``message`` marker for the HTML-as-200 trap (status is a real 200,
#: so the marker is what distinguishes it from a genuine fetch).
_HTML_TRAP_MARKER = "html-not-xml"


def _note_attempt(
    attempts: list[Attempt],
    *,
    status: int | None = None,
    transport_class: str | None = None,
    message: str,
) -> None:
    """Append one non-success :class:`Attempt`; ``n`` derives from list position."""
    attempts.append(
        Attempt(
            n=len(attempts) + 1,
            status=status,
            transport_class=transport_class,
            message=message,
        )
    )


def _record_success(rec: Recorder | None, url: str, attempts: list[Attempt]) -> None:
    """Record a successful XML fetch, plus a resolved Run Event if it retried."""
    if rec is None:
        return
    rec.note_success("senate", url)
    if attempts:
        rec.note_request_outcome("senate", url, attempts, resolved=True)


def _record_failure(rec: Recorder | None, url: str, attempts: list[Attempt]) -> None:
    """Record a terminal XML fetch failure as a failed Run Event (no-op without a recorder)."""
    if rec is not None:
        rec.note_request_outcome("senate", url, attempts, resolved=False)


def _record_html_trap(rec: Recorder | None, url: str, attempts: list[Attempt]) -> None:
    """Record the HTML-as-200 trap as a failed Run Event.

    The HTTP status was 200, so a synthetic attempt carrying status 200 and the
    :data:`_HTML_TRAP_MARKER` makes the "looked OK, was an HTML 404" cause legible.

    Note the deliberate asymmetry with ``text.py``'s "no <pre>" structural
    failure: there the HTTP request is a genuine success (counted) *and* a
    separate failed event records the unparseable body, because the content
    check happens after the chokepoint returns. Here the content check lives
    inside the chokepoint, so the trap is recorded as a failure *only* — never a
    success. Both are correct given where each parse check sits; the success
    counts mean "successful network requests", and an HTML 404 is not one.
    """
    if rec is None:
        return
    _note_attempt(attempts, status=HTTP_OK, message=_HTML_TRAP_MARKER)
    rec.note_request_outcome("senate", url, attempts, resolved=False)


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse(xml_bytes: bytes) -> ET.Element:
    """Parse XML bytes into an element tree, raising :class:`SenateXmlError`."""
    try:
        return ET.fromstring(xml_bytes)  # noqa: S314 — senate.gov XML is trusted (no DTD, no external entities)
    except ET.ParseError as exc:
        raise SenateXmlError(f"malformed XML: {exc}") from exc


__all__ = [
    "DETAIL_REQUEST_SLEEP_SECONDS",
    "DETAIL_URL",
    "MENU_URL",
    "ROSTER_URL",
    "USER_AGENT",
    "SenateClient",
    "parse_senate_roster",
    "parse_vote_menu",
]
