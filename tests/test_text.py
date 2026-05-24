"""Tests for the article text fetcher."""

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from concord.text import TextFetchError, fetch_text


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    """Build an httpx.Client backed by a MockTransport handler."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ok(body: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "text/html"})


SAMPLE_URL = (
    "https://www.congress.gov/119/crec/2026/05/22/172/88/modified/CREC-2026-05-22-pt1-PgD551-6.htm"
)


# -- happy path with real captured fixtures -----------------------------------


class TestFetchTextHappyPath:
    @pytest.mark.parametrize(
        "fixture",
        ["articles/daily_digest.html", "articles/house.html", "articles/extensions.html"],
    )
    def test_extracts_plain_text(self, fixtures_dir: Path, fixture: str) -> None:
        html = (fixtures_dir / fixture).read_text()
        with _client(lambda r: _ok(html)) as client:
            text = fetch_text(SAMPLE_URL, client)
        # Real articles always have substantive content; strip removes
        # leading/trailing whitespace inside <pre>.
        assert len(text) > 100
        # The anchor text "www.gpo.gov" is preserved even though <a> is dropped.
        assert "www.gpo.gov" in text
        # Tags themselves don't leak into output.
        assert "<a " not in text
        assert "<pre>" not in text

    def test_house_fixture_preserves_inline_anchor_text(self, fixtures_dir: Path) -> None:
        # The house fixture contains: <a href='https://www.gpo.gov'>www.gpo.gov</a>
        # Verify the visible text survives while the anchor tag is dropped.
        html = (fixtures_dir / "articles/house.html").read_text()
        with _client(lambda r: _ok(html)) as client:
            text = fetch_text(SAMPLE_URL, client)
        assert "www.gpo.gov" in text
        assert "https://www.gpo.gov" not in text  # the href attribute is dropped
        # Specific content from the fixture survives unchanged.
        assert "DISCHARGE PETITIONS" in text

    def test_strips_leading_and_trailing_whitespace(self) -> None:
        html = "<pre>\n\n  hello world  \n\n</pre>"
        with _client(lambda r: _ok(html)) as client:
            text = fetch_text(SAMPLE_URL, client)
        assert text == "hello world"


# -- structural / parsing errors ----------------------------------------------


class TestFetchTextParseErrors:
    def test_html_without_pre_block_raises(self) -> None:
        with (
            _client(lambda r: _ok("<html><body>nothing useful</body></html>")) as client,
            pytest.raises(TextFetchError, match="no <pre> content"),
        ):
            fetch_text(SAMPLE_URL, client)

    def test_empty_pre_block_raises(self) -> None:
        with (
            _client(lambda r: _ok("<pre>   \n   </pre>")) as client,
            pytest.raises(TextFetchError, match="no <pre> content"),
        ):
            fetch_text(SAMPLE_URL, client)


# -- HTTP / transport errors --------------------------------------------------


class TestFetchTextNetworkErrors:
    @pytest.mark.parametrize("status", [404, 410, 500, 502, 503])
    def test_http_error_raises(self, status: int) -> None:
        with (
            _client(lambda r: httpx.Response(status)) as client,
            pytest.raises(TextFetchError) as exc,
        ):
            fetch_text(SAMPLE_URL, client)
        assert exc.value.status_code == status

    def test_transport_error_raises(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated")

        with (
            _client(handler) as client,
            pytest.raises(TextFetchError, match="transport error") as exc,
        ):
            fetch_text(SAMPLE_URL, client)
        assert exc.value.status_code is None


# -- redirect handling --------------------------------------------------------


class TestFetchTextRedirects:
    def test_follows_redirects(self) -> None:
        """The Congressional Record sometimes redirects ``/modified/`` URLs.

        Verify follow_redirects=True is in effect by having the first response
        be a 301 to a second URL that returns the actual HTML.
        """
        requests_seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_seen.append(str(request.url))
            if request.url.path.endswith("/start"):
                return httpx.Response(301, headers={"location": "https://example.com/final"})
            return _ok("<pre>redirected content</pre>")

        with _client(handler) as client:
            text = fetch_text("https://example.com/start", client)

        assert text == "redirected content"
        # Both URLs were hit — proving redirect was followed.
        assert len(requests_seen) == 2
        assert requests_seen[0].endswith("/start")
        assert requests_seen[1].endswith("/final")
