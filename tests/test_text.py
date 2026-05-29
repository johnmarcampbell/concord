"""Tests for the article text fetcher."""

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from concord.text import (
    MAX_BACKOFF,
    AdaptiveThrottle,
    TextFetchError,
    fetch_text,
)


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
    @pytest.mark.parametrize("status", [404, 410])
    def test_non_retryable_4xx_raises_immediately(self, status: int) -> None:
        calls = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(status)

        with (
            _client(handler) as client,
            pytest.raises(TextFetchError) as exc,
        ):
            fetch_text(SAMPLE_URL, client, sleep=lambda _s: None)
        assert exc.value.status_code == status
        assert calls == 1

    @pytest.mark.parametrize("status", [500, 502, 503])
    def test_5xx_retried_then_surfaces(self, status: int) -> None:
        calls = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(status)

        with (
            _client(handler) as client,
            pytest.raises(TextFetchError) as exc,
        ):
            fetch_text(SAMPLE_URL, client, sleep=lambda _s: None)
        assert exc.value.status_code == status
        # 1 initial + MAX_5XX_RETRIES retries
        assert calls == 6

    def test_5xx_then_success(self) -> None:
        responses = iter([httpx.Response(503), _ok("<pre>recovered</pre>")])
        with _client(lambda r: next(responses)) as client:
            text = fetch_text(SAMPLE_URL, client, sleep=lambda _s: None)
        assert text == "recovered"

    def test_transport_error_retried_then_surfaces(self) -> None:
        calls = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ConnectError("simulated")

        with (
            _client(handler) as client,
            pytest.raises(TextFetchError, match="transport error") as exc,
        ):
            fetch_text(SAMPLE_URL, client, sleep=lambda _s: None)
        assert exc.value.status_code is None
        assert calls == 6


class TestFetchTextRateLimit:
    def test_429_retries_until_success(self) -> None:
        responses = iter(
            [
                httpx.Response(429),
                httpx.Response(429),
                _ok("<pre>after backoff</pre>"),
            ]
        )
        slept: list[float] = []
        with _client(lambda r: next(responses)) as client:
            text = fetch_text(SAMPLE_URL, client, sleep=slept.append)
        assert text == "after backoff"
        # Two 429s -> two sleeps, and the backoff *escalates* (1s, then 2s)
        # rather than sitting at a flat 1s — the bug this fix exists for.
        assert slept == [1.0, 2.0]

    def test_429_honors_retry_after_header(self) -> None:
        responses = iter(
            [
                httpx.Response(429, headers={"retry-after": "7"}),
                _ok("<pre>ok</pre>"),
            ]
        )
        slept: list[float] = []
        with _client(lambda r: next(responses)) as client:
            fetch_text(SAMPLE_URL, client, sleep=slept.append)
        assert slept == [7.0]

    def test_429_retry_after_clamped_to_max_backoff(self) -> None:
        responses = iter(
            [
                httpx.Response(429, headers={"retry-after": "99999"}),
                _ok("<pre>ok</pre>"),
            ]
        )
        slept: list[float] = []
        with _client(lambda r: next(responses)) as client:
            fetch_text(SAMPLE_URL, client, sleep=slept.append)
        assert slept == [MAX_BACKOFF]

    def test_429_does_not_count_against_5xx_budget(self) -> None:
        # Many 429s followed by success: must not exhaust the 5xx retry budget.
        sequence = [httpx.Response(429)] * 20 + [_ok("<pre>finally</pre>")]
        responses = iter(sequence)
        with _client(lambda r: next(responses)) as client:
            text = fetch_text(SAMPLE_URL, client, sleep=lambda _s: None)
        assert text == "finally"

    def test_403_retries_until_success(self) -> None:
        # congress.gov uses 403 as a rate-limit signal; must back off and retry.
        responses = iter(
            [
                httpx.Response(403),
                httpx.Response(403),
                _ok("<pre>after 403 backoff</pre>"),
            ]
        )
        slept: list[float] = []
        with _client(lambda r: next(responses)) as client:
            text = fetch_text(SAMPLE_URL, client, sleep=slept.append)
        assert text == "after 403 backoff"
        # 403s escalate identically to 429s (1s, then 2s) — congress.gov's 403
        # carries no Retry-After, so our own escalating backoff is all we have.
        assert slept == [1.0, 2.0]

    def test_403_honors_retry_after_header(self) -> None:
        responses = iter(
            [
                httpx.Response(403, headers={"retry-after": "5"}),
                _ok("<pre>ok</pre>"),
            ]
        )
        slept: list[float] = []
        with _client(lambda r: next(responses)) as client:
            fetch_text(SAMPLE_URL, client, sleep=slept.append)
        assert slept == [5.0]

    def test_403_does_not_count_against_5xx_budget(self) -> None:
        # Many 403s followed by success must not exhaust the 5xx retry budget.
        sequence = [httpx.Response(403)] * 20 + [_ok("<pre>finally</pre>")]
        responses = iter(sequence)
        with _client(lambda r: next(responses)) as client:
            text = fetch_text(SAMPLE_URL, client, sleep=lambda _s: None)
        assert text == "finally"


class TestAdaptiveThrottle:
    """The client-level backoff that persists across URLs in a run."""

    def test_escalates_and_recovers(self) -> None:
        slept: list[float] = []
        throttle = AdaptiveThrottle(sleep=slept.append)

        # Healthy: pacing is a no-op.
        throttle.pace()
        assert slept == []
        assert throttle.level == 0

        # Each throttle hit escalates the backoff and the level.
        assert throttle.penalize(None) == 1.0
        assert throttle.penalize(None) == 2.0
        assert throttle.penalize(None) == 4.0
        assert throttle.level == 3

        # A Retry-After header overrides the schedule but still escalates level.
        assert throttle.penalize(30.0) == 30.0
        assert throttle.level == 4
        assert slept == [1.0, 2.0, 4.0, 30.0]

        # Clean fetches relax one level only after _DECAY_AFTER_SUCCESSES of them,
        # so a single success can't cancel a throttle.
        throttle.recover()
        throttle.recover()
        assert throttle.level == 4
        throttle.recover()
        assert throttle.level == 3

    def test_level_and_delay_are_capped(self) -> None:
        slept: list[float] = []
        throttle = AdaptiveThrottle(sleep=slept.append)
        for _ in range(20):
            throttle.penalize(None)
        # Level saturates and the per-wait delay never exceeds MAX_BACKOFF.
        assert throttle.level == 7
        assert throttle.penalize(None) == MAX_BACKOFF
        assert max(slept) == MAX_BACKOFF

    def test_backoff_persists_across_urls(self) -> None:
        # A shared throttle threaded through two fetches: the first URL's 403s
        # leave the level raised, so the second URL is *paced* before its very
        # first request — even though that request would have succeeded.
        responses = iter(
            [
                httpx.Response(403),
                httpx.Response(403),
                _ok("<pre>first</pre>"),
                _ok("<pre>second</pre>"),
            ]
        )
        slept: list[float] = []
        throttle = AdaptiveThrottle(sleep=slept.append)
        with _client(lambda r: next(responses)) as client:
            first = fetch_text(SAMPLE_URL, client, throttle=throttle)
            second = fetch_text(SAMPLE_URL, client, throttle=throttle)
        assert (first, second) == ("first", "second")
        # URL 1: penalize 1s, 2s (climbs to level 2). URL 2: pace 2s up front.
        assert slept == [1.0, 2.0, 2.0]
        assert throttle.level == 2

    def test_per_call_throttle_does_not_leak_state(self) -> None:
        # Omitting `throttle` gives each call a fresh one — no cross-call pacing.
        responses = iter([httpx.Response(403), _ok("<pre>a</pre>"), _ok("<pre>b</pre>")])
        slept: list[float] = []
        with _client(lambda r: next(responses)) as client:
            fetch_text(SAMPLE_URL, client, sleep=slept.append)
            fetch_text(SAMPLE_URL, client, sleep=slept.append)
        # Only the first call's single 403 slept; the second starts fresh (no pace).
        assert slept == [1.0]


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
