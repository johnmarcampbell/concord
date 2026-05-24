"""Tests for the api.congress.gov client.

All tests inject an ``httpx.MockTransport`` so nothing leaves the process.
JSON responses are real captures from the live API, stored under
``tests/fixtures/api/``.
"""

from __future__ import annotations

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
    return Client(api_key="test-key", transport=_mock_transport(handler))


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
            return _json_response(payload)

        with _make_client(handler) as client:
            articles = client.list_articles(volume=172, issue_number=88)

        # Fixture has 6 + 3 + 11 = 20 articles across 3 sections.
        assert len(articles) == 20
        sections = {a.section for a in articles}
        assert sections == {"Daily Digest", "Extensions of Remarks Section", "House Section"}

        # Section names propagate from the parent payload, not the article.
        assert all(a.section in sections for a in articles)

        # Every article has a granule_id derived from its text_url.
        assert all(a.granule_id.startswith("CREC-") for a in articles)

        req = captured[0]
        assert req.url.path == "/v3/daily-congressional-record/172/88/articles"

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
