"""Tests for the api.congress.gov client.

All tests inject an ``httpx.MockTransport`` so nothing leaves the process.
JSON responses are real captures from the live API, stored under
``tests/fixtures/api/``.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from concord.api import ENV_API_KEY, ApiError, Client


def _mock_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _json_response(payload: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=json.dumps(payload), headers={"content-type": "application/json"}
    )


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> Client:
    """Default test client: never actually sleeps during retries."""
    return Client(api_key="test-key", transport=_mock_transport(handler), sleep=lambda _s: None)


def _make_article(granule_id: str) -> dict[str, Any]:
    """Build a minimal article dict matching the /articles response shape."""
    base = "https://www.congress.gov/119/crec/2026/05/22/172/88"
    return {
        "title": f"Sample {granule_id}",
        "startPage": "S1",
        "endPage": "S2",
        "text": [
            {"type": "Formatted Text", "url": f"{base}/modified/{granule_id}.htm"},
            {"type": "PDF", "url": f"{base}/{granule_id}.pdf"},
        ],
    }


# -- construction -------------------------------------------------------------


class TestConstruction:
    def test_uses_explicit_api_key(self) -> None:
        client = Client(api_key="explicit", transport=_mock_transport(lambda r: _json_response({})))
        assert client._api_key == "explicit"

    def test_falls_back_to_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_API_KEY, "from-env")
        client = Client(transport=_mock_transport(lambda r: _json_response({})))
        assert client._api_key == "from-env"

    def test_missing_key_raises_apierror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_API_KEY, raising=False)
        with pytest.raises(ApiError, match="API key required"):
            Client(transport=_mock_transport(lambda r: _json_response({})))

    def test_context_manager_closes_underlying_client(self) -> None:
        with _make_client(lambda r: _json_response({})) as client:
            assert not client._client.is_closed
        assert client._client.is_closed


# -- list_issues --------------------------------------------------------------


class TestListIssues:
    def test_parses_first_page(self, fixtures_dir: Path) -> None:
        payload = json.loads((fixtures_dir / "api/list_issues_page1.json").read_text())

        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return _json_response(payload)

        with _make_client(handler) as client:
            issues, next_offset = client.list_issues(limit=10, offset=0)

        assert len(issues) == 10
        # First entry in the captured fixture is volume 172, issue 88.
        assert issues[0].volume == 172
        assert issues[0].issue_number == 88
        # Page 1 with more behind it -> next_offset = limit
        assert next_offset == 10

        # Verify request shape: correct path, JSON format, api_key, limit, offset.
        req = captured[0]
        assert req.url.path == "/v3/daily-congressional-record"
        params = dict(req.url.params)
        assert params["format"] == "json"
        assert params["api_key"] == "test-key"
        assert params["limit"] == "10"
        assert params["offset"] == "0"

    def test_last_page_returns_none_next_offset(self, fixtures_dir: Path) -> None:
        payload = json.loads((fixtures_dir / "api/list_issues_last_page.json").read_text())
        with _make_client(lambda r: _json_response(payload)) as client:
            issues, next_offset = client.list_issues(limit=10, offset=5800)

        assert len(issues) == 10
        assert next_offset is None

    def test_passes_through_offset_for_pagination(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return _json_response({"dailyCongressionalRecord": [], "pagination": {"count": 0}})

        with _make_client(handler) as client:
            client.list_issues(limit=25, offset=100)

        params = dict(captured[0].url.params)
        assert params["limit"] == "25"
        assert params["offset"] == "100"


# -- list_articles ------------------------------------------------------------


class TestListArticles:
    def test_flattens_sections(self, fixtures_dir: Path) -> None:
        payload = json.loads((fixtures_dir / "api/articles_172_88.json").read_text())

        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            # The fixture has `pagination.next` (count=24 in 20-per-page
            # chunks). list_articles walks pagination now, so the mock
            # must terminate it: serve the fixture for the first request
            # (offset omitted or 0), serve an empty terminating page after.
            offset = int(request.url.params.get("offset", "0"))
            if offset == 0:
                return _json_response(payload)
            return _json_response({"articles": [], "pagination": {"count": 24}})

        with _make_client(handler) as client:
            articles = client.list_articles(volume=172, issue_number=88)

        # Fixture has 6 + 3 + 11 = 20 articles across 3 sections (first
        # page only — the real /articles response has more, but our
        # stubbed page-2 is empty for the test).
        assert len(articles) == 20
        sections = {a.section for a in articles}
        assert sections == {"Daily Digest", "Extensions of Remarks Section", "House Section"}

        # Section names propagate from the parent payload, not the article.
        assert all(a.section in sections for a in articles)

        # Every article has a granule_id derived from its text_url.
        assert all(a.granule_id.startswith("CREC-") for a in articles)

        # First request was to the right path; pagination triggered exactly
        # one follow-up (because the fixture had pagination.next).
        assert captured[0].url.path == "/v3/daily-congressional-record/172/88/articles"
        assert len(captured) == 2

    def test_walks_pagination_to_completion(self) -> None:
        """When ``pagination.next`` is present, list_articles follows it."""

        # Three pages: 2 articles each on pages 1+2, 1 article on page 3, no next.
        def _page(granule_ids: list[str], *, has_next: bool) -> dict[str, Any]:
            return {
                "articles": [
                    {
                        "name": "Senate Section",
                        "sectionArticles": [_make_article(g) for g in granule_ids],
                    }
                ],
                "pagination": {"count": 5, **({"next": "..."} if has_next else {})},
            }

        page_payloads = [
            _page(["CREC-2026-05-22-pt1-PgS0", "CREC-2026-05-22-pt1-PgS1"], has_next=True),
            _page(["CREC-2026-05-22-pt1-PgS2", "CREC-2026-05-22-pt1-PgS3"], has_next=True),
            _page(["CREC-2026-05-22-pt1-PgS4"], has_next=False),
        ]
        call_idx = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            n = call_idx["n"]
            call_idx["n"] += 1
            return _json_response(page_payloads[n])

        with _make_client(handler) as client:
            articles = client.list_articles(volume=172, issue_number=88)
        assert len(articles) == 5
        assert call_idx["n"] == 3

    def test_missing_text_format_raises(self) -> None:
        # An article entry without the required "Formatted Text" URL is a
        # contract violation worth surfacing clearly rather than silently
        # producing a half-built Article.
        payload = {
            "articles": [
                {
                    "name": "Senate Section",
                    "sectionArticles": [
                        {
                            "title": "Bad article",
                            "startPage": "S1",
                            "endPage": "S1",
                            "text": [
                                {"type": "PDF", "url": "https://example.com/x.pdf"},
                            ],
                        }
                    ],
                }
            ]
        }
        with (
            _make_client(lambda r: _json_response(payload)) as client,
            pytest.raises(ApiError, match="missing text format"),
        ):
            client.list_articles(volume=1, issue_number=1)


# -- error handling -----------------------------------------------------------


class TestErrors:
    @pytest.mark.parametrize("status", [401, 403, 404, 500, 502, 503])
    def test_http_error_raises_apierror(self, status: int) -> None:
        with (
            _make_client(lambda r: httpx.Response(status, json={"error": "x"})) as client,
            pytest.raises(ApiError) as exc,
        ):
            client.list_issues()
        assert exc.value.status_code == status

    def test_transport_error_raises_apierror(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated")

        with (
            _make_client(handler) as client,
            pytest.raises(ApiError, match="transport error") as exc,
        ):
            client.list_issues()
        assert exc.value.status_code is None

    def test_non_object_json_raises(self) -> None:
        with (
            _make_client(lambda r: _json_response(["not", "an", "object"])) as client,
            pytest.raises(ApiError, match="expected JSON object"),
        ):
            client.list_issues()


# -- retry / rate-limit -------------------------------------------------------


def _sequenced_handler(
    responses: list[httpx.Response],
) -> Callable[[httpx.Request], httpx.Response]:
    """Return a handler that yields the given responses one per call."""
    iterator = iter(responses)

    def handler(_: httpx.Request) -> httpx.Response:
        try:
            return next(iterator)
        except StopIteration as exc:
            raise AssertionError("handler ran out of canned responses") from exc

    return handler


def _sequenced_exceptions(
    items: list[BaseException | httpx.Response],
) -> Callable[[httpx.Request], httpx.Response]:
    """Like _sequenced_handler but each element may be a Response or an exception."""
    iterator = iter(items)

    def handler(_: httpx.Request) -> httpx.Response:
        item = next(iterator)
        if isinstance(item, httpx.Response):
            return item
        raise item

    return handler


def _client_with_sleep_recorder(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[Client, list[float]]:
    """Client whose sleep() records its arguments instead of waiting."""
    sleeps: list[float] = []
    client = Client(
        api_key="test-key",
        transport=_mock_transport(handler),
        sleep=sleeps.append,
    )
    return client, sleeps


class TestRetry5xx:
    def test_recovers_after_transient_5xx(self) -> None:
        # Two 503s, then success.
        handler = _sequenced_handler(
            [
                httpx.Response(503),
                httpx.Response(503),
                _json_response({"dailyCongressionalRecord": [], "pagination": {"count": 0}}),
            ]
        )
        client, sleeps = _client_with_sleep_recorder(handler)
        with client:
            issues, _ = client.list_issues()
        assert issues == []
        # Slept between the two retries (1s, 2s).
        assert sleeps == [1.0, 2.0]

    def test_gives_up_after_max_attempts(self) -> None:
        # Always 500. Should retry MAX_5XX_RETRIES (5) times, then raise.
        handler = _sequenced_handler([httpx.Response(500) for _ in range(6)])
        client, sleeps = _client_with_sleep_recorder(handler)
        with client, pytest.raises(ApiError, match="gave up after 5 attempts") as exc:
            client.list_issues()
        assert exc.value.status_code == 500
        # Five sleeps before the final attempt fails out.
        assert len(sleeps) == 5
        # Backoff schedule: 1, 2, 4, 8, 16
        assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0]

    def test_4xx_other_than_429_is_not_retried(self) -> None:
        # A 404 must NOT trigger retries — it's a permanent error.
        handler = _sequenced_handler([httpx.Response(404)])
        client, sleeps = _client_with_sleep_recorder(handler)
        with client, pytest.raises(ApiError) as exc:
            client.list_issues()
        assert exc.value.status_code == 404
        assert sleeps == []  # Never slept.


class TestRetryTransport:
    def test_recovers_after_transient_connect_error(self) -> None:
        handler = _sequenced_exceptions(
            [
                httpx.ConnectError("first"),
                _json_response({"dailyCongressionalRecord": [], "pagination": {"count": 0}}),
            ]
        )
        client, sleeps = _client_with_sleep_recorder(handler)
        with client:
            client.list_issues()
        assert sleeps == [1.0]

    def test_gives_up_after_max_transport_errors(self) -> None:
        handler = _sequenced_exceptions([httpx.ConnectError("x") for _ in range(6)])
        client, sleeps = _client_with_sleep_recorder(handler)
        with client, pytest.raises(ApiError, match="gave up after 5 attempts") as exc:
            client.list_issues()
        assert exc.value.status_code is None
        assert len(sleeps) == 5


class TestRetry429:
    def test_respects_retry_after_header(self) -> None:
        handler = _sequenced_handler(
            [
                httpx.Response(429, headers={"Retry-After": "3"}),
                _json_response({"dailyCongressionalRecord": [], "pagination": {"count": 0}}),
            ]
        )
        client, sleeps = _client_with_sleep_recorder(handler)
        with client:
            client.list_issues()
        # Slept the 3 seconds the server requested, exactly once.
        assert sleeps == [3.0]

    def test_falls_back_to_backoff_without_retry_after(self) -> None:
        handler = _sequenced_handler(
            [
                httpx.Response(429),  # no header
                _json_response({"dailyCongressionalRecord": [], "pagination": {"count": 0}}),
            ]
        )
        client, sleeps = _client_with_sleep_recorder(handler)
        with client:
            client.list_issues()
        assert sleeps == [1.0]  # _backoff_seconds(0) = 1

    def test_retries_indefinitely(self) -> None:
        """Many 429s in a row should NOT count against the 5xx budget."""
        # 10 consecutive 429s — would exceed MAX_5XX_RETRIES — then succeed.
        responses = [httpx.Response(429) for _ in range(10)]
        responses.append(
            _json_response({"dailyCongressionalRecord": [], "pagination": {"count": 0}})
        )
        handler = _sequenced_handler(responses)
        client, sleeps = _client_with_sleep_recorder(handler)
        with client:
            client.list_issues()
        # Slept once per 429, all with the same delay (no escalation).
        assert len(sleeps) == 10

    def test_retry_after_capped_to_max_backoff(self) -> None:
        # A pathological server says "wait 24 hours" — clamp to MAX_BACKOFF.
        handler = _sequenced_handler(
            [
                httpx.Response(429, headers={"Retry-After": "86400"}),
                _json_response({"dailyCongressionalRecord": [], "pagination": {"count": 0}}),
            ]
        )
        client, sleeps = _client_with_sleep_recorder(handler)
        with client:
            client.list_issues()
        assert sleeps == [60.0]  # MAX_BACKOFF

    def test_malformed_retry_after_falls_back_to_backoff(self) -> None:
        handler = _sequenced_handler(
            [
                httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
                _json_response({"dailyCongressionalRecord": [], "pagination": {"count": 0}}),
            ]
        )
        client, sleeps = _client_with_sleep_recorder(handler)
        with client:
            client.list_issues()
        # HTTP-date form not supported; falls back to exponential.
        assert sleeps == [1.0]


# -- list_members ------------------------------------------------------------


class TestListMembers:
    def test_iterates_single_page(self, fixtures_dir: Path) -> None:
        payload = json.loads((fixtures_dir / "api/members/current_house.json").read_text())
        client = _make_client(lambda r: _json_response(payload))
        with client:
            members = list(client.list_members(congress=119))
        assert len(members) == 1
        assert members[0]["bioguideId"] == "O000172"

    def test_paginates_until_no_next(self, fixtures_dir: Path) -> None:
        page1 = json.loads((fixtures_dir / "api/members/current_house.json").read_text())
        page1["pagination"] = {"next": "https://example.invalid/next", "count": 2}
        page2 = json.loads((fixtures_dir / "api/members/current_senate.json").read_text())

        responses = iter([page1, page2])

        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(next(responses))

        client = _make_client(handler)
        with client:
            members = list(client.list_members(congress=119))
        assert [m["bioguideId"] for m in members] == ["O000172", "S000033"]

    def test_passes_congress_into_path(self) -> None:
        captured: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request.url.path)
            return _json_response({"members": [], "pagination": {"count": 0}})

        client = _make_client(handler)
        with client:
            list(client.list_members(congress=118))
        assert captured == ["/v3/member/congress/118"]


# -- list_bills --------------------------------------------------------------


class TestListBills:
    def test_iterates_single_page(self, fixtures_dir: Path) -> None:
        payload = json.loads((fixtures_dir / "api/bills/list_hr_119.json").read_text())
        client = _make_client(lambda r: _json_response(payload))
        with client:
            bills = list(client.list_bills(congress=119, bill_type="hr"))
        assert [b["number"] for b in bills] == ["1", "22"]

    def test_paginates_until_no_next(self, fixtures_dir: Path) -> None:
        page1 = json.loads((fixtures_dir / "api/bills/list_hr_119.json").read_text())
        page1["pagination"] = {"next": "https://example.invalid/next", "count": 4}
        page2 = {
            "bills": [
                {"congress": 119, "type": "HR", "number": "47"},
                {"congress": 119, "type": "HR", "number": "88"},
            ],
            "pagination": {"count": 4},
        }
        responses = iter([page1, page2])

        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(next(responses))

        client = _make_client(handler)
        with client:
            bills = list(client.list_bills(congress=119, bill_type="hr"))
        assert [b["number"] for b in bills] == ["1", "22", "47", "88"]

    def test_canonicalizes_bill_type_to_lowercase(self) -> None:
        captured: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request.url.path)
            return _json_response({"bills": [], "pagination": {"count": 0}})

        client = _make_client(handler)
        with client:
            list(client.list_bills(congress=119, bill_type="HR"))
        assert captured == ["/v3/bill/119/hr"]


# -- get_bill_detail ---------------------------------------------------------


class TestGetBillDetail:
    def test_parses_detail_payload(self, fixtures_dir: Path) -> None:
        payload = json.loads((fixtures_dir / "api/bills/detail_119_hr_1.json").read_text())
        client = _make_client(lambda r: _json_response(payload))
        with client:
            bill = client.get_bill_detail(congress=119, bill_type="hr", bill_number=1)
        assert bill["number"] == "1"
        assert bill["sponsors"][0]["bioguideId"] == "S001176"
        assert bill["policyArea"]["name"] == "Energy"

    def test_canonicalizes_bill_type_in_path(self) -> None:
        captured: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request.url.path)
            return _json_response({"bill": {"number": "1"}})

        client = _make_client(handler)
        with client:
            client.get_bill_detail(congress=119, bill_type="HR", bill_number=1)
        assert captured == ["/v3/bill/119/hr/1"]

    def test_missing_bill_object_raises(self) -> None:
        client = _make_client(lambda r: _json_response({"notBill": {}}))
        with client, pytest.raises(ApiError, match="expected 'bill' object"):
            client.get_bill_detail(congress=119, bill_type="hr", bill_number=1)


# -- bill sub-endpoints (Phase 2b) -------------------------------------------


class TestGetBillCosponsors:
    def test_returns_cosponsors_payload(self, fixtures_dir: Path) -> None:
        payload = json.loads((fixtures_dir / "api/bills/cosponsors_119_hr_22.json").read_text())
        client = _make_client(lambda r: _json_response(payload))
        with client:
            result = client.get_bill_cosponsors(119, "hr", 22)
        assert len(result["cosponsors"]) == 3
        assert result["cosponsors"][0]["bioguideId"] == "B001302"

    def test_paginates_until_no_next(self, fixtures_dir: Path) -> None:
        page1 = json.loads((fixtures_dir / "api/bills/cosponsors_119_hr_22.json").read_text())
        page1 = {**page1, "pagination": {"count": 5, "next": "https://example.invalid/next"}}
        page2 = {
            "cosponsors": [
                {
                    "bioguideId": "X000001",
                    "sponsorshipDate": "2025-03-01",
                    "isOriginalCosponsor": False,
                    "sponsorshipWithdrawnDate": None,
                },
                {
                    "bioguideId": "X000002",
                    "sponsorshipDate": "2025-03-02",
                    "isOriginalCosponsor": False,
                    "sponsorshipWithdrawnDate": None,
                },
            ],
            "pagination": {"count": 5},
        }
        responses = iter([page1, page2])

        def handler(_: httpx.Request) -> httpx.Response:
            return _json_response(next(responses))

        client = _make_client(handler)
        with client:
            result = client.get_bill_cosponsors(119, "hr", 22)
        assert len(result["cosponsors"]) == 5
        assert result["cosponsors"][-1]["bioguideId"] == "X000002"

    def test_canonicalizes_bill_type(self) -> None:
        captured: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request.url.path)
            return _json_response({"cosponsors": [], "pagination": {"count": 0}})

        client = _make_client(handler)
        with client:
            client.get_bill_cosponsors(119, "HR", 1)
        assert captured == ["/v3/bill/119/hr/1/cosponsors"]


class TestGetBillActions:
    def test_returns_actions_payload(self, fixtures_dir: Path) -> None:
        payload = json.loads((fixtures_dir / "api/bills/actions_119_hr_1.json").read_text())
        client = _make_client(lambda r: _json_response(payload))
        with client:
            result = client.get_bill_actions(119, "hr", 1)
        assert len(result["actions"]) == 6
        assert result["actions"][0]["actionDate"] == "2026-03-30"

    def test_paginates(self) -> None:
        page1 = {
            "actions": [{"actionDate": "2026-03-30", "text": "A"}],
            "pagination": {"count": 2, "next": "..."},
        }
        page2 = {
            "actions": [{"actionDate": "2026-03-29", "text": "B"}],
            "pagination": {"count": 2},
        }
        responses = iter([page1, page2])

        def handler(_: httpx.Request) -> httpx.Response:
            return _json_response(next(responses))

        client = _make_client(handler)
        with client:
            result = client.get_bill_actions(119, "hr", 1)
        assert [a["text"] for a in result["actions"]] == ["A", "B"]


class TestGetBillSubjects:
    def test_concatenates_legislative_subjects_across_pages(self) -> None:
        page1 = {
            "subjects": {
                "legislativeSubjects": [{"name": "Energy"}, {"name": "Oil"}],
                "policyArea": {"name": "Energy"},
            },
            "pagination": {"count": 4, "next": "..."},
        }
        page2 = {
            "subjects": {
                "legislativeSubjects": [{"name": "Gas"}, {"name": "Renewables"}],
                "policyArea": {"name": "Energy"},
            },
            "pagination": {"count": 4},
        }
        responses = iter([page1, page2])

        def handler(_: httpx.Request) -> httpx.Response:
            return _json_response(next(responses))

        client = _make_client(handler)
        with client:
            result = client.get_bill_subjects(119, "hr", 1)
        names = [s["name"] for s in result["subjects"]["legislativeSubjects"]]
        assert names == ["Energy", "Oil", "Gas", "Renewables"]
        assert result["subjects"]["policyArea"]["name"] == "Energy"


class TestGetBillTitles:
    def test_returns_titles_payload(self, fixtures_dir: Path) -> None:
        payload = json.loads((fixtures_dir / "api/bills/titles_119_hr_1.json").read_text())
        client = _make_client(lambda r: _json_response(payload))
        with client:
            result = client.get_bill_titles(119, "hr", 1)
        assert len(result["titles"]) == 4


class TestGetBillSummaries:
    def test_returns_summaries_payload(self, fixtures_dir: Path) -> None:
        payload = json.loads((fixtures_dir / "api/bills/summaries_119_hr_1.json").read_text())
        client = _make_client(lambda r: _json_response(payload))
        with client:
            result = client.get_bill_summaries(119, "hr", 1)
        assert len(result["summaries"]) == 3
        assert result["summaries"][0]["versionCode"] == "00"


# -- House votes (Phase 3a) -------------------------------------------------


class TestListHouseVotes:
    def test_iterates_single_page(self, fixtures_dir: Path) -> None:
        payload = json.loads((fixtures_dir / "api/votes/list_house_119_1.json").read_text())
        client = _make_client(lambda r: _json_response(payload))
        with client:
            votes = list(client.list_house_votes(congress=119, session=1))
        assert [v["rollCallNumber"] for v in votes] == [240, 241]

    def test_paginates_until_no_next(self, fixtures_dir: Path) -> None:
        page1 = json.loads((fixtures_dir / "api/votes/list_house_119_1.json").read_text())
        page1 = {**page1, "pagination": {"count": 4, "next": "https://example.invalid/x"}}
        page2 = {
            "houseRollCallVotes": [
                {"congress": 119, "sessionNumber": 1, "rollCallNumber": 242},
                {"congress": 119, "sessionNumber": 1, "rollCallNumber": 243},
            ],
            "pagination": {"count": 4},
        }
        responses = iter([page1, page2])

        def handler(_: httpx.Request) -> httpx.Response:
            return _json_response(next(responses))

        client = _make_client(handler)
        with client:
            votes = list(client.list_house_votes(congress=119, session=1))
        assert [v["rollCallNumber"] for v in votes] == [240, 241, 242, 243]

    def test_uses_correct_path(self) -> None:
        captured: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request.url.path)
            return _json_response({"houseRollCallVotes": [], "pagination": {"count": 0}})

        client = _make_client(handler)
        with client:
            list(client.list_house_votes(congress=119, session=1))
        assert captured == ["/v3/house-vote/119/1"]


class TestGetHouseVoteDetail:
    def test_unwraps_envelope(self, fixtures_dir: Path) -> None:
        payload = json.loads((fixtures_dir / "api/votes/detail_house_119_1_240.json").read_text())
        client = _make_client(lambda r: _json_response(payload))
        with client:
            vote = client.get_house_vote_detail(congress=119, session=1, roll_number=240)
        assert vote["rollCallNumber"] == 240
        assert vote["voteQuestion"] == "On Passage of the Bill"

    def test_missing_envelope_raises(self) -> None:
        client = _make_client(lambda r: _json_response({"notVote": {}}))
        with client, pytest.raises(ApiError, match="expected 'houseRollCallVote' object"):
            client.get_house_vote_detail(119, 1, 240)


class TestGetHouseVoteMembers:
    def test_unwraps_envelope(self, fixtures_dir: Path) -> None:
        payload = json.loads((fixtures_dir / "api/votes/members_house_119_1_240.json").read_text())
        client = _make_client(lambda r: _json_response(payload))
        with client:
            members = client.get_house_vote_members(congress=119, session=1, roll_number=240)
        assert len(members["results"]) == 4

    def test_uses_correct_path(self) -> None:
        captured: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request.url.path)
            return _json_response({"houseRollCallVoteMemberVotes": {"results": []}})

        client = _make_client(handler)
        with client:
            client.get_house_vote_members(119, 1, 240)
        assert captured == ["/v3/house-vote/119/1/240/members"]
