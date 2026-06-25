"""Typed client for `api.congress.gov <https://api.congress.gov/>`_.

Network resilience lives in :mod:`concord.fetch`. This module layers the
api.congress.gov specifics above that spine: URL construction, JSON envelope
parsing, and conversion into the project's typed models.
"""

import json
import logging
import os
import time
from collections.abc import Callable, Iterator
from types import TracebackType
from typing import Any

import httpx

from concord import __version__
from concord.fetch import Fetcher, FetchError, RetryAfterPolicy
from concord.models.proceedings import Article, Issue

API_BASE = "https://api.congress.gov/v3"
USER_AGENT = f"concord/{__version__}"
ENV_API_KEY = "CONGRESS_API_KEY"

#: Per-page size when walking the ``/articles`` endpoint. The API maxes out
#: at 250 results per page; using the max minimizes round trips on issues
#: with hundreds of proceedings (the Senate routinely produces 100+).
ARTICLES_PAGE_SIZE = 250

#: Per-page size when walking ``/member/congress/{congress}``.
MEMBERS_PAGE_SIZE = 250

#: Per-page size when walking ``/bill/{congress}/{billType}``. The API
#: caps at 250; using the max keeps pagination cost down on a 6K-bill
#: Congress.
BILLS_PAGE_SIZE = 250

#: Per-page size when walking a Bill sub-endpoint (cosponsors, actions,
#: subjects, titles, summaries). Same 250 cap as the parent list
#: endpoint; minimizes round trips on bills with hundreds of cosponsors
#: or actions.
BILL_SUB_PAGE_SIZE = 250

#: Per-page size when walking ``/house-vote/{congress}/{session}``. The
#: API caps at 250; a single House session produces ~600-800 rolls so
#: the list endpoint settles in three to four pages.
VOTES_PAGE_SIZE = 250

_log = logging.getLogger("concord.api")


class ApiError(Exception):
    """Raised when api.congress.gov returns a non-success status or a transport error.

    ``status_code`` is the HTTP status when the failure was an HTTP response,
    or ``None`` for transport-level failures (DNS, timeout, connection reset).
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class Client:
    """Typed wrapper over ``api.congress.gov``.

    The client owns an ``httpx.Client`` underneath; pass a custom ``transport``
    (e.g. :class:`httpx.MockTransport`) to intercept requests in tests.

    Use as a context manager so the underlying connection pool is closed::

        with Client(api_key="...") as client:
            issues, next_offset = client.list_issues()
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        resolved = api_key if api_key is not None else os.environ.get(ENV_API_KEY)
        if not resolved:
            raise ApiError(f"API key required: pass api_key=... or set {ENV_API_KEY}")
        self._api_key = resolved
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=API_BASE,
            transport=transport,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
        )
        self._fetch = Fetcher(
            self._client,
            source="api",
            policy=RetryAfterPolicy(logger=_log),
            sleep=self._sleep,
        )

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- endpoints -----------------------------------------------------------

    def list_issues(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Issue], int | None]:
        """List daily Congressional Record issues, newest first.

        Returns ``(issues, next_offset)``. ``next_offset`` is ``None`` once the
        last page has been served (the API omits ``pagination.next``).

        The API does not support a date filter on this endpoint; callers
        paginate until they walk past their target date.
        """
        payload = self._get(
            "/daily-congressional-record",
            params={"limit": limit, "offset": offset},
        )
        rows = payload.get("dailyCongressionalRecord", [])
        issues = [Issue.from_congress_api(row) for row in rows]
        has_next = "next" in payload.get("pagination", {})
        next_offset = offset + limit if has_next else None
        return issues, next_offset

    def list_articles(self, volume: int, issue_number: int) -> list[Article]:
        """List **every** article in one issue, flattening the section nesting.

        The API groups articles by section (``Senate Section``, ``House
        Section``, ``Extensions of Remarks Section``, ``Daily Digest``) and
        paginates the response — the default page size is 20 and the API
        caps at 250. This method walks every page (``pagination.next``)
        until exhausted and returns one flat list, populating each
        :class:`Article`'s ``section`` from the parent ``name``.
        """
        out: list[Article] = []
        offset = 0
        path = f"/daily-congressional-record/{volume}/{issue_number}/articles"
        while True:
            payload = self._get(path, params={"limit": ARTICLES_PAGE_SIZE, "offset": offset})
            page_count = 0
            for section in payload.get("articles", []):
                section_name = section["name"]
                for art in section.get("sectionArticles", []):
                    try:
                        out.append(Article.from_congress_api(art, section=section_name))
                    except ValueError as exc:
                        raise ApiError(str(exc)) from exc
                    page_count += 1
            if "next" not in payload.get("pagination", {}):
                break
            if page_count == 0:
                # Defensive: server claims "next" but page is empty. Stop
                # rather than loop forever.
                break
            offset += ARTICLES_PAGE_SIZE
        return out

    def list_members(
        self,
        congress: int,
        *,
        on_total: Callable[[int], None] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield every Member of one Congress as a raw API payload dict.

        Walks ``GET /v3/member/congress/{congress}`` until ``pagination.next``
        is absent. Returns raw dicts; structured parsing into
        :class:`concord.models.Member` happens at the next layer per
        ADR 0007.
        """
        offset = 0
        path = f"/member/congress/{congress}"
        while True:
            payload = self._get(path, params={"limit": MEMBERS_PAGE_SIZE, "offset": offset})
            if on_total is not None and offset == 0:
                total = payload.get("pagination", {}).get("count")
                if isinstance(total, int):
                    on_total(total)
            page_count = 0
            for raw in payload.get("members", []):
                yield raw
                page_count += 1
            if "next" not in payload.get("pagination", {}):
                return
            if page_count == 0:
                # Defensive: server advertises "next" but returned nothing.
                return
            offset += MEMBERS_PAGE_SIZE

    def list_bills(
        self,
        congress: int,
        bill_type: str,
        *,
        on_total: Callable[[int], None] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield every Bill stub for one Congress + bill type.

        Walks ``GET /v3/bill/{congress}/{bill_type}`` until
        ``pagination.next`` is absent. Returns raw stub dicts — the full
        identity record lives on :meth:`get_bill_detail`. ``bill_type``
        is canonicalized to lowercase before URL formatting; the API
        accepts both cases but the rest of the codebase stores lowercase.
        """
        bt = bill_type.lower()
        offset = 0
        path = f"/bill/{congress}/{bt}"
        while True:
            payload = self._get(path, params={"limit": BILLS_PAGE_SIZE, "offset": offset})
            if on_total is not None and offset == 0:
                total = payload.get("pagination", {}).get("count")
                if isinstance(total, int):
                    on_total(total)
            page_count = 0
            for raw in payload.get("bills", []):
                yield raw
                page_count += 1
            if "next" not in payload.get("pagination", {}):
                return
            if page_count == 0:
                # Defensive: server advertises "next" but returned nothing.
                return
            offset += BILLS_PAGE_SIZE

    def get_bill_detail(
        self,
        congress: int,
        bill_type: str,
        bill_number: int,
    ) -> dict[str, Any]:
        """Fetch the detail record for one Bill.

        Returns the ``bill`` object from ``/v3/bill/{c}/{t}/{n}`` — the
        full identity payload (sponsor, latestAction, policyArea, …).
        ``bill_type`` is canonicalized to lowercase before URL formatting.
        """
        bt = bill_type.lower()
        payload = self._get(f"/bill/{congress}/{bt}/{bill_number}")
        bill = payload.get("bill")
        if not isinstance(bill, dict):
            raise ApiError(
                f"expected 'bill' object in detail response for "
                f"{congress}/{bt}/{bill_number}; got {type(bill).__name__}"
            )
        return bill

    def get_bill_cosponsors(
        self,
        congress: int,
        bill_type: str,
        bill_number: int,
    ) -> dict[str, Any]:
        """Fetch every cosponsor of one Bill, paginating to completion.

        Returns the sub-endpoint response with the ``cosponsors`` array
        concatenated across all pages. Other top-level keys (``pagination``,
        ``request``) come from the final page.
        """
        return self._paginate_sub_endpoint(
            congress, bill_type, bill_number, "cosponsors", array_key="cosponsors"
        )

    def get_bill_actions(
        self,
        congress: int,
        bill_type: str,
        bill_number: int,
    ) -> dict[str, Any]:
        """Fetch every action in one Bill's legislative history, paginating to completion."""
        return self._paginate_sub_endpoint(
            congress, bill_type, bill_number, "actions", array_key="actions"
        )

    def get_bill_subjects(
        self,
        congress: int,
        bill_type: str,
        bill_number: int,
    ) -> dict[str, Any]:
        """Fetch every CRS-assigned subject for one Bill, paginating to completion.

        The subjects endpoint nests its array one level deep
        (``subjects.legislativeSubjects``); pagination concatenates that
        inner list across pages while preserving the outer ``policyArea``
        sibling from the final page.
        """
        bt = bill_type.lower()
        path = f"/bill/{congress}/{bt}/{bill_number}/subjects"
        offset = 0
        merged: dict[str, Any] = {}
        merged_legislative: list[Any] = []
        while True:
            payload = self._get(path, params={"limit": BILL_SUB_PAGE_SIZE, "offset": offset})
            subjects_obj = payload.get("subjects") or {}
            page_legislative = (
                subjects_obj.get("legislativeSubjects", [])
                if isinstance(subjects_obj, dict)
                else []
            )
            merged_legislative.extend(page_legislative)
            merged = payload
            if "next" not in payload.get("pagination", {}):
                break
            if not page_legislative:
                break
            offset += BILL_SUB_PAGE_SIZE
        # Overwrite the final-page legislativeSubjects with the concatenated list.
        outer = merged.get("subjects")
        if isinstance(outer, dict):
            outer = {**outer, "legislativeSubjects": merged_legislative}
            merged = {**merged, "subjects": outer}
        return merged

    def get_bill_titles(
        self,
        congress: int,
        bill_type: str,
        bill_number: int,
    ) -> dict[str, Any]:
        """Fetch every title variant for one Bill.

        The titles endpoint advertises pagination but in practice every
        bill's full set of titles fits on one page; the implementation
        still walks ``pagination.next`` defensively.
        """
        return self._paginate_sub_endpoint(
            congress, bill_type, bill_number, "titles", array_key="titles"
        )

    def get_bill_summaries(
        self,
        congress: int,
        bill_type: str,
        bill_number: int,
    ) -> dict[str, Any]:
        """Fetch every CRS-written summary version for one Bill."""
        return self._paginate_sub_endpoint(
            congress, bill_type, bill_number, "summaries", array_key="summaries"
        )

    # -- House votes (Phase 3a) ---------------------------------------------

    def list_house_votes(
        self,
        congress: int,
        session: int,
        *,
        on_total: Callable[[int], None] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield every House roll-call vote stub for one ``(congress, session)``.

        Walks ``GET /v3/house-vote/{congress}/{session}`` until
        ``pagination.next`` is absent. Returns raw stub dicts — the full
        vote record lives on :meth:`get_house_vote_detail`, and the
        per-member positions live on :meth:`get_house_vote_members`.
        Stubs are not persisted by the scraper; they exist only to drive
        discovery of roll numbers.
        """
        offset = 0
        path = f"/house-vote/{congress}/{session}"
        while True:
            payload = self._get(path, params={"limit": VOTES_PAGE_SIZE, "offset": offset})
            if on_total is not None and offset == 0:
                total = payload.get("pagination", {}).get("count")
                if isinstance(total, int):
                    on_total(total)
            page_count = 0
            for raw in payload.get("houseRollCallVotes", []):
                yield raw
                page_count += 1
            if "next" not in payload.get("pagination", {}):
                return
            if page_count == 0:
                # Defensive: server advertises "next" but returned nothing.
                return
            offset += VOTES_PAGE_SIZE

    def get_house_vote_detail(
        self,
        congress: int,
        session: int,
        roll_number: int,
    ) -> dict[str, Any]:
        """Fetch the detail record for one House roll-call vote.

        Returns the ``houseRollCallVote`` object from
        ``/v3/house-vote/{c}/{s}/{roll}`` — the full record (vote_type,
        result, totals, the subject's bill/amendment linkage if any,
        and the per-party totals breakdown).
        """
        payload = self._get(f"/house-vote/{congress}/{session}/{roll_number}")
        vote = payload.get("houseRollCallVote")
        if not isinstance(vote, dict):
            raise ApiError(
                f"expected 'houseRollCallVote' object in detail response for "
                f"{congress}/{session}/{roll_number}; got {type(vote).__name__}"
            )
        return vote

    def get_house_vote_members(
        self,
        congress: int,
        session: int,
        roll_number: int,
    ) -> dict[str, Any]:
        """Fetch the full per-member position roster for one House roll-call vote.

        Returns the ``houseRollCallVoteMemberVotes`` object from
        ``/v3/house-vote/{c}/{s}/{roll}/members`` — one entry per Member
        in the ``results`` array, Bioguide-keyed, carrying the
        Member's recorded position, party at the time of the vote, and
        state.
        """
        payload = self._get(f"/house-vote/{congress}/{session}/{roll_number}/members")
        members = payload.get("houseRollCallVoteMemberVotes")
        if not isinstance(members, dict):
            raise ApiError(
                f"expected 'houseRollCallVoteMemberVotes' object in members response for "
                f"{congress}/{session}/{roll_number}; got {type(members).__name__}"
            )
        return members

    def _paginate_sub_endpoint(
        self,
        congress: int,
        bill_type: str,
        bill_number: int,
        sub_path: str,
        *,
        array_key: str,
    ) -> dict[str, Any]:
        """Walk one Bill sub-endpoint until ``pagination.next`` is absent.

        Returns the final page's payload with ``payload[array_key]``
        replaced by the concatenation of every page's array. The path is
        ``/v3/bill/{c}/{t}/{n}/{sub_path}`` with ``bill_type`` canonicalized
        to lowercase.
        """
        bt = bill_type.lower()
        path = f"/bill/{congress}/{bt}/{bill_number}/{sub_path}"
        offset = 0
        merged_array: list[Any] = []
        merged: dict[str, Any] = {}
        while True:
            payload = self._get(path, params={"limit": BILL_SUB_PAGE_SIZE, "offset": offset})
            page = payload.get(array_key, [])
            if isinstance(page, list):
                merged_array.extend(page)
            merged = payload
            if "next" not in payload.get("pagination", {}):
                break
            if not isinstance(page, list) or not page:
                # Defensive: server claims "next" but page is empty/wrong shape.
                break
            offset += BILL_SUB_PAGE_SIZE
        return {**merged, array_key: merged_array}

    # -- internals -----------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        merged: dict[str, Any] = {"format": "json", "api_key": self._api_key}
        if params:
            merged.update(params)

        try:
            response = self._fetch.get(path, params=merged)
        except FetchError as exc:
            raise ApiError(str(exc), status_code=exc.status_code) from exc

        try:
            data: Any = json.loads(response.content)
        except json.JSONDecodeError as exc:
            raise ApiError(f"expected JSON object from {path}, got invalid JSON") from exc
        if not isinstance(data, dict):
            raise ApiError(f"expected JSON object from {path}, got {type(data).__name__}")
        return data
