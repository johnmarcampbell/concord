"""text.py chokepoint recording — successful fetches bump ``text:article``,
error outcomes emit Run Events with retry resolution, 403/429 throttle hits
are recorded as errors, and the "no <pre>" structural failure is recorded as a
failed event (ADR 0021). Retry *behavior* is covered by test_text.py and
unchanged.

All tests run inside a manually-installed Recorder contextvar rather than a
full ``scrape_run`` so they assert against in-memory state without touching
SQLite.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import httpx
import pytest

from concord.observability import Recorder, _recorder
from concord.text import _NO_PRE_MARKER, AdaptiveThrottle, TextFetchError, fetch_text

SAMPLE_URL = (
    "https://www.congress.gov/119/crec/2026/05/22/172/88/modified/CREC-2026-05-22-pt1-PgD551-6.htm"
)


@contextmanager
def _active_recorder() -> Iterator[Recorder]:
    rec = Recorder(entity="proceedings", command="scrape proceedings", started_at=datetime.now(UTC))
    token = _recorder.set(rec)
    try:
        yield rec
    finally:
        _recorder.reset(token)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _sequenced(items: list[httpx.Response]) -> Callable[[httpx.Request], httpx.Response]:
    iterator = iter(items)

    def handler(_: httpx.Request) -> httpx.Response:
        return next(iterator)

    return handler


def _ok(body: str = "<pre>hello world</pre>") -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "text/html"})


def _throttle() -> AdaptiveThrottle:
    # Inject a no-op sleep so throttle penalties don't burn real wall time.
    return AdaptiveThrottle(sleep=lambda _s: None)


class TestSuccessRecording:
    def test_clean_fetch_bumps_bucket_and_emits_no_event(self) -> None:
        with _active_recorder() as rec, _client(lambda _r: _ok()) as client:
            fetch_text(SAMPLE_URL, client, throttle=_throttle())
        assert rec.successes == {"text:article": 1}
        assert rec.events == []

    def test_no_recorder_is_a_noop(self) -> None:
        with _client(lambda _r: _ok()) as client:
            text = fetch_text(SAMPLE_URL, client, throttle=_throttle())
        assert text == "hello world"


class TestErrorRecording:
    def test_403_then_200_records_resolved_event_with_403_attempt(self) -> None:
        handler = _sequenced([httpx.Response(403), _ok()])
        with _active_recorder() as rec, _client(handler) as client:
            fetch_text(SAMPLE_URL, client, throttle=_throttle())
        assert rec.successes == {"text:article": 1}
        assert len(rec.events) == 1
        event = rec.events[0]
        assert event.endpoint_bucket == "text:article"
        assert event.final_status == "resolved"
        assert [a.status for a in event.attempts] == [403]

    def test_429_then_200_records_resolved_event_with_429_attempt(self) -> None:
        handler = _sequenced([httpx.Response(429), _ok()])
        with _active_recorder() as rec, _client(handler) as client:
            fetch_text(SAMPLE_URL, client, throttle=_throttle())
        assert len(rec.events) == 1
        assert rec.events[0].final_status == "resolved"
        assert [a.status for a in rec.events[0].attempts] == [429]

    def test_503_then_200_emits_one_resolved_event(self) -> None:
        handler = _sequenced([httpx.Response(503), _ok()])
        with _active_recorder() as rec, _client(handler) as client:
            fetch_text(SAMPLE_URL, client, throttle=_throttle(), sleep=lambda _s: None)
        assert rec.successes == {"text:article": 1}
        assert len(rec.events) == 1
        assert rec.events[0].final_status == "resolved"
        assert [a.status for a in rec.events[0].attempts] == [503]

    def test_terminal_503_emits_one_failed_event(self) -> None:
        handler = _sequenced([httpx.Response(503) for _ in range(6)])
        with (
            _active_recorder() as rec,
            _client(handler) as client,
            pytest.raises(TextFetchError, match="gave up after 5 attempts"),
        ):
            fetch_text(SAMPLE_URL, client, throttle=_throttle(), sleep=lambda _s: None)
        assert rec.successes == {}
        assert len(rec.events) == 1
        event = rec.events[0]
        assert event.final_status == "failed"
        # Six attempts: the initial try + five retries, all 503.
        assert [a.status for a in event.attempts] == [503] * 6

    def test_non_retryable_4xx_emits_failed_event(self) -> None:
        with (
            _active_recorder() as rec,
            _client(lambda _r: httpx.Response(404)) as client,
            pytest.raises(TextFetchError),
        ):
            fetch_text(SAMPLE_URL, client, throttle=_throttle())
        assert rec.successes == {}
        assert len(rec.events) == 1
        assert rec.events[0].final_status == "failed"
        assert rec.events[0].attempts[0].status == 404

    def test_transport_error_then_success_records_transport_class(self) -> None:
        calls = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("dns")
            return _ok()

        with _active_recorder() as rec, _client(handler) as client:
            fetch_text(SAMPLE_URL, client, throttle=_throttle(), sleep=lambda _s: None)
        assert len(rec.events) == 1
        attempt = rec.events[0].attempts[0]
        assert attempt.status is None
        assert attempt.transport_class == "ConnectError"


class TestStructuralFailureRecording:
    def test_no_pre_block_records_failed_event_after_a_counted_success(self) -> None:
        # The HTTP fetch succeeds (counted as a success) but the body has no
        # <pre> block — recorded as a separate failed event so "fetched but
        # unparseable" is visible, not silently dropped.
        body = "<html><body>nothing useful</body></html>"
        with (
            _active_recorder() as rec,
            _client(lambda _r: _ok(body)) as client,
            pytest.raises(TextFetchError, match="no <pre> content"),
        ):
            fetch_text(SAMPLE_URL, client, throttle=_throttle())
        assert rec.successes == {"text:article": 1}
        assert len(rec.events) == 1
        event = rec.events[0]
        assert event.endpoint_bucket == "text:article"
        assert event.final_status == "failed"
        attempt = event.attempts[0]
        assert attempt.status is None
        assert attempt.transport_class == _NO_PRE_MARKER
