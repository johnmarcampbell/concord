"""Tests for the article text fetcher."""

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from concord.fetch import Disposition
from concord.text import (
    MAX_COOLDOWN,
    AdaptiveThrottle,
    AdaptiveThrottlePolicy,
    TextFetchError,
    fetch_text,
)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    """Build an httpx.Client backed by a MockTransport handler."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ok(body: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "text/html"})


def _no_sleep(_seconds: float) -> None:
    """No-op sleep: pacing applies to every request, so tests must opt out."""


def _low_jitter() -> float:
    """rng pinned to the band's low edge: pace x 0.5, cooldown x 1.0."""
    return 0.0


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
            text = fetch_text(SAMPLE_URL, client, sleep=_no_sleep)
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
            text = fetch_text(SAMPLE_URL, client, sleep=_no_sleep)
        assert "www.gpo.gov" in text
        assert "https://www.gpo.gov" not in text  # the href attribute is dropped
        # Specific content from the fixture survives unchanged.
        assert "DISCHARGE PETITIONS" in text

    def test_strips_leading_and_trailing_whitespace(self) -> None:
        html = "<pre>\n\n  hello world  \n\n</pre>"
        with _client(lambda r: _ok(html)) as client:
            text = fetch_text(SAMPLE_URL, client, sleep=_no_sleep)
        assert text == "hello world"


# -- structural / parsing errors ----------------------------------------------


class TestFetchTextParseErrors:
    def test_html_without_pre_block_raises(self) -> None:
        with (
            _client(lambda r: _ok("<html><body>nothing useful</body></html>")) as client,
            pytest.raises(TextFetchError, match="no <pre> content"),
        ):
            fetch_text(SAMPLE_URL, client, sleep=_no_sleep)

    def test_empty_pre_block_raises(self) -> None:
        with (
            _client(lambda r: _ok("<pre>   \n   </pre>")) as client,
            pytest.raises(TextFetchError, match="no <pre> content"),
        ):
            fetch_text(SAMPLE_URL, client, sleep=_no_sleep)


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
            fetch_text(SAMPLE_URL, client, sleep=_no_sleep)
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
            fetch_text(SAMPLE_URL, client, sleep=_no_sleep)
        assert exc.value.status_code == status
        # 1 initial + MAX_5XX_RETRIES retries
        assert calls == 6

    def test_5xx_then_success(self) -> None:
        responses = iter([httpx.Response(503), _ok("<pre>recovered</pre>")])
        with _client(lambda r: next(responses)) as client:
            text = fetch_text(SAMPLE_URL, client, sleep=_no_sleep)
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
            fetch_text(SAMPLE_URL, client, sleep=_no_sleep)
        assert exc.value.status_code is None
        assert calls == 6


class TestFetchTextRateLimit:
    def test_429_cooldowns_escalate_per_strike(self) -> None:
        responses = iter(
            [
                httpx.Response(429),
                httpx.Response(429),
                _ok("<pre>after cooldown</pre>"),
            ]
        )
        slept: list[float] = []
        throttle = AdaptiveThrottle(sleep=slept.append, rng=_low_jitter)
        with _client(lambda r: next(responses)) as client:
            text = fetch_text(SAMPLE_URL, client, throttle=throttle)
        assert text == "after cooldown"
        # Initial pace (1.0s x low-edge jitter 0.5), then window-sized cooldowns
        # that double per consecutive strike — not the old 1s/2s nibbles that
        # kept re-entering the Cloudflare block window.
        assert slept == [0.5, 30.0, 60.0]

    def test_429_honors_retry_after_when_longer_than_schedule(self) -> None:
        responses = iter(
            [
                httpx.Response(429, headers={"retry-after": "300"}),
                _ok("<pre>ok</pre>"),
            ]
        )
        slept: list[float] = []
        throttle = AdaptiveThrottle(sleep=slept.append, rng=_low_jitter)
        with _client(lambda r: next(responses)) as client:
            fetch_text(SAMPLE_URL, client, throttle=throttle)
        assert slept == [0.5, 300.0]

    def test_429_distrusts_retry_after_shorter_than_schedule(self) -> None:
        # A 5s hint on the first strike is below the 30s window schedule;
        # honoring short hints is how the old scheme kept getting re-blocked.
        responses = iter(
            [
                httpx.Response(429, headers={"retry-after": "5"}),
                _ok("<pre>ok</pre>"),
            ]
        )
        slept: list[float] = []
        throttle = AdaptiveThrottle(sleep=slept.append, rng=_low_jitter)
        with _client(lambda r: next(responses)) as client:
            fetch_text(SAMPLE_URL, client, throttle=throttle)
        assert slept == [0.5, 30.0]

    def test_429_retry_after_clamped_to_max_cooldown(self) -> None:
        responses = iter(
            [
                httpx.Response(429, headers={"retry-after": "99999"}),
                _ok("<pre>ok</pre>"),
            ]
        )
        slept: list[float] = []
        throttle = AdaptiveThrottle(sleep=slept.append, rng=_low_jitter)
        with _client(lambda r: next(responses)) as client:
            fetch_text(SAMPLE_URL, client, throttle=throttle)
        assert slept == [0.5, MAX_COOLDOWN]

    def test_429_does_not_count_against_5xx_budget(self) -> None:
        # Many 429s followed by success: must not exhaust the 5xx retry budget.
        sequence = [httpx.Response(429)] * 20 + [_ok("<pre>finally</pre>")]
        responses = iter(sequence)
        with _client(lambda r: next(responses)) as client:
            text = fetch_text(SAMPLE_URL, client, sleep=_no_sleep)
        assert text == "finally"

    def test_403_treated_as_throttle_signal(self) -> None:
        # congress.gov uses 403 as a rate-limit signal; it carries no
        # Retry-After, so the escalating cooldown schedule is all we have.
        responses = iter(
            [
                httpx.Response(403),
                httpx.Response(403),
                _ok("<pre>after 403 cooldown</pre>"),
            ]
        )
        slept: list[float] = []
        throttle = AdaptiveThrottle(sleep=slept.append, rng=_low_jitter)
        with _client(lambda r: next(responses)) as client:
            text = fetch_text(SAMPLE_URL, client, throttle=throttle)
        assert text == "after 403 cooldown"
        assert slept == [0.5, 30.0, 60.0]

    def test_cooldown_log_reports_retry_after(self, caplog: pytest.LogCaptureFixture) -> None:
        # The log distinguishes a server-hinted cooldown from our own strike
        # schedule: 429s carry the parsed Retry-After, 403s log "none".
        responses = iter(
            [
                httpx.Response(429, headers={"retry-after": "300"}),
                httpx.Response(403),
                _ok("<pre>ok</pre>"),
            ]
        )
        throttle = AdaptiveThrottle(sleep=lambda _s: None, rng=_low_jitter)
        with (
            caplog.at_level("WARNING", logger="concord.text"),
            _client(lambda r: next(responses)) as client,
        ):
            fetch_text(SAMPLE_URL, client, throttle=throttle)
        assert "Retry-After: 300s" in caplog.messages[0]
        assert "Retry-After: none" in caplog.messages[1]

    def test_403_does_not_count_against_5xx_budget(self) -> None:
        # Many 403s followed by success must not exhaust the 5xx retry budget.
        sequence = [httpx.Response(403)] * 20 + [_ok("<pre>finally</pre>")]
        responses = iter(sequence)
        with _client(lambda r: next(responses)) as client:
            text = fetch_text(SAMPLE_URL, client, sleep=_no_sleep)
        assert text == "finally"


class TestAdaptiveThrottle:
    """The client-level AIMD pacer that persists across URLs in a run."""

    def test_paces_every_request_even_when_healthy(self) -> None:
        slept: list[float] = []
        throttle = AdaptiveThrottle(sleep=slept.append, rng=_low_jitter)
        throttle.pace()
        # 1.0s initial pace x 0.5 low-edge jitter: never full speed, even
        # before the first throttle response.
        assert slept == [0.5]

    def test_pace_jitter_spans_the_band(self) -> None:
        slept: list[float] = []
        throttle = AdaptiveThrottle(sleep=slept.append, rng=lambda: 1.0)
        throttle.pace()
        # rng at the top of the band: 1.0s pace x 1.5 jitter.
        assert slept == [1.5]

    def test_penalize_doubles_pace_and_escalates_cooldown(self) -> None:
        slept: list[float] = []
        throttle = AdaptiveThrottle(sleep=slept.append, rng=_low_jitter)
        assert throttle.penalize(None) == 30.0
        assert throttle.penalize(None) == 60.0
        assert throttle.penalize(None) == 120.0
        assert throttle.strikes == 3
        # Pace doubled per strike: 1 → 2 → 4 → 8.
        assert throttle.pace_seconds == 8.0

    def test_cooldown_jitter_is_upward_only(self) -> None:
        slept: list[float] = []
        throttle = AdaptiveThrottle(sleep=slept.append, rng=lambda: 1.0)
        # rng at the top of the band: 30s schedule x 1.25 — retries land past
        # the window boundary, never before it.
        assert throttle.penalize(None) == pytest.approx(37.5)

    def test_cooldown_and_pace_are_capped(self) -> None:
        slept: list[float] = []
        throttle = AdaptiveThrottle(sleep=slept.append, rng=_low_jitter)
        # Far past where 2**strikes would overflow a float if the schedule's
        # exponent were unbounded — a permanent block must wait, not crash.
        for _ in range(1100):
            throttle.penalize(None)
        assert throttle.penalize(None) == MAX_COOLDOWN
        assert max(slept) == MAX_COOLDOWN
        assert throttle.pace_seconds == 30.0

    def test_retry_after_overrides_schedule_only_upward(self) -> None:
        slept: list[float] = []
        throttle = AdaptiveThrottle(sleep=slept.append, rng=_low_jitter)
        # Longer than the 30s first-strike schedule: honored.
        assert throttle.penalize(120.0) == 120.0
        # Shorter than the 60s second-strike schedule: distrusted.
        assert throttle.penalize(5.0) == 60.0

    def test_recover_resets_strikes_but_decays_pace_slowly(self) -> None:
        slept: list[float] = []
        throttle = AdaptiveThrottle(sleep=slept.append, rng=_low_jitter)
        throttle.penalize(None)
        assert throttle.strikes == 1
        throttle.recover()
        assert throttle.strikes == 0
        # One clean fetch claws back only _PACE_DECAY of the doubled pace —
        # a steady drip of "one 403 per URL" still ratchets the rate down.
        assert throttle.pace_seconds == pytest.approx(1.95)
        # Strikes reset means the next cooldown starts back at the base…
        assert throttle.penalize(None) == 30.0
        # …but the pace keeps compounding.
        assert throttle.pace_seconds == pytest.approx(3.9)

    def test_pace_decays_to_floor_not_zero(self) -> None:
        slept: list[float] = []
        throttle = AdaptiveThrottle(sleep=slept.append, rng=_low_jitter)
        for _ in range(100):
            throttle.recover()
        assert throttle.pace_seconds == 0.5
        throttle.pace()
        # Floor pace x low-edge jitter: still never zero.
        assert slept == [0.25]

    def test_pacing_persists_across_urls(self) -> None:
        # A shared throttle threaded through two fetches: the first URL's 403s
        # double the pace, so the second URL is paced harder before its very
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
        throttle = AdaptiveThrottle(sleep=slept.append, rng=_low_jitter)
        with _client(lambda r: next(responses)) as client:
            first = fetch_text(SAMPLE_URL, client, throttle=throttle)
            second = fetch_text(SAMPLE_URL, client, throttle=throttle)
        assert (first, second) == ("first", "second")
        # URL 1: pace 0.5, cooldowns 30/60 (pace climbs to 4.0), success decays
        # it to 3.95. URL 2: paced at 3.95 x 0.5 up front; its own success
        # decays the pace once more.
        assert slept == pytest.approx([0.5, 30.0, 60.0, 1.975])
        assert throttle.pace_seconds == pytest.approx(3.90)

    def test_per_call_throttle_does_not_leak_state(self) -> None:
        # Omitting `throttle` gives each call a fresh one — no cross-call
        # pacing. Default throttles keep real jitter, so assert on shape
        # (sleep count, cooldown magnitude) rather than exact values.
        responses = iter([httpx.Response(403), _ok("<pre>a</pre>"), _ok("<pre>b</pre>")])
        slept_first: list[float] = []
        slept_second: list[float] = []
        with _client(lambda r: next(responses)) as client:
            fetch_text(SAMPLE_URL, client, sleep=slept_first.append)
            fetch_text(SAMPLE_URL, client, sleep=slept_second.append)
        # First call: one pace + one 403 cooldown.
        assert len(slept_first) == 2
        assert slept_first[1] >= 30.0
        # Second call: a fresh throttle paces once and never cools down.
        assert len(slept_second) == 1
        assert slept_second[0] < 30.0


class TestAdaptiveThrottlePolicy:
    """The adapter wiring AdaptiveThrottle onto the concord.fetch policy seam."""

    def test_before_request_paces_every_request(self) -> None:
        slept: list[float] = []
        throttle = AdaptiveThrottle(sleep=slept.append, rng=_low_jitter)
        AdaptiveThrottlePolicy(throttle).before_request()
        # The initial 1.0s pace x low-edge jitter — before_request maps to pace().
        assert slept == [0.5]

    @pytest.mark.parametrize("status", [403, 429])
    def test_throttle_status_penalizes_and_signals_throttle(self, status: int) -> None:
        slept: list[float] = []
        throttle = AdaptiveThrottle(sleep=slept.append, rng=_low_jitter)
        decision = AdaptiveThrottlePolicy(throttle).on_response(SAMPLE_URL, httpx.Response(status))
        assert decision.disposition is Disposition.THROTTLE
        # penalize slept the first-strike cooldown and escalated the throttle.
        assert slept == [30.0]
        assert throttle.strikes == 1

    def test_throttle_decision_carries_no_delay(self) -> None:
        # penalize owns the cooldown wait, so the decision must be zero-delay or
        # the fetch spine would sleep the same cooldown a second time.
        throttle = AdaptiveThrottle(sleep=lambda _s: None, rng=_low_jitter)
        decision = AdaptiveThrottlePolicy(throttle).on_response(SAMPLE_URL, httpx.Response(429))
        assert decision.delay == 0.0

    def test_success_status_allows_without_penalizing(self) -> None:
        slept: list[float] = []
        throttle = AdaptiveThrottle(sleep=slept.append, rng=_low_jitter)
        decision = AdaptiveThrottlePolicy(throttle).on_response(SAMPLE_URL, httpx.Response(200))
        assert decision.disposition is Disposition.ALLOW
        assert slept == []
        assert throttle.strikes == 0

    def test_on_success_recovers_the_throttle(self) -> None:
        throttle = AdaptiveThrottle(sleep=lambda _s: None, rng=_low_jitter)
        policy = AdaptiveThrottlePolicy(throttle)
        policy.on_response(SAMPLE_URL, httpx.Response(429))
        assert throttle.strikes == 1
        policy.on_success()
        assert throttle.strikes == 0


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
            text = fetch_text("https://example.com/start", client, sleep=_no_sleep)

        assert text == "redirected content"
        # Both URLs were hit — proving redirect was followed.
        assert len(requests_seen) == 2
        assert requests_seen[0].endswith("/start")
        assert requests_seen[1].endswith("/final")
