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
from concord.fetch import Decision, Disposition, Fetcher, FetchError, RateLimitPolicy

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

#: Inter-request padding for detail fetches (defensive only).
DETAIL_REQUEST_SLEEP_SECONDS = 0.1

#: Synthetic ``message`` marker for the HTML-as-200 trap (the status is a real
#: 200, so the marker is what distinguishes it from a genuine XML fetch).
_HTML_TRAP_MARKER = "html-not-xml"


class SenateSentinelPolicy(RateLimitPolicy):
    """Reclassify senate.gov's HTML-disguised-as-200 trap as a network failure.

    senate.gov serves ``200 OK`` with an HTML error page (not a 404) for a
    roll-call file that doesn't exist yet. That is a not-found *sentinel* — a
    404 the server mislabels as 200 — so the policy rejects it inside the fetch
    seam, turning it into a terminal :class:`~concord.fetch.FetchError` recorded
    as a failed Run Event carrying the :data:`_HTML_TRAP_MARKER`, exactly the
    ledger outcome the old inline content-type check produced.

    senate.gov has no observed rate limit, so there is no throttle branch: a
    (never-observed) 429 falls through to the spine's terminal path, preserving
    the client's historical "any non-200 raises" behaviour. ``before_request``
    and ``on_success`` stay the inherited no-ops.
    """

    def on_response(self, _path: str, response: httpx.Response) -> Decision:
        content_type = response.headers.get("Content-Type", "").lower()
        if response.is_success and "text/html" in content_type:
            return Decision(Disposition.REJECT, message=_HTML_TRAP_MARKER)
        return Decision(Disposition.ALLOW)


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
        self._fetch = Fetcher(
            self._client,
            source="senate",
            policy=SenateSentinelPolicy(),
            sleep=self._sleep,
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
        """Fetch ``url`` and return the raw XML bytes.

        A thin wrapper over the shared :class:`~concord.fetch.Fetcher`, which
        owns the retry/backoff loop and Run Event recording (ADR 0021);
        :class:`SenateSentinelPolicy` reclassifies the HTML not-found trap as a
        terminal failure inside that seam. The spine's
        :class:`~concord.fetch.FetchError` is translated back into this module's
        public :class:`SenateXmlError`.
        """
        try:
            return self._fetch.get(url).content
        except FetchError as exc:
            raise SenateXmlError(str(exc)) from exc


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
