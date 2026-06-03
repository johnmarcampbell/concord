"""api.py chokepoint recording — successes bump the right bucket, error
outcomes emit Run Events with retry resolution, and 429s are recorded as
errors (ADR 0021). Retry *behavior* is covered by test_api.py and unchanged.

All tests run inside a manually-installed Recorder contextvar rather than a
full ``scrape_run`` so they assert against in-memory state without touching
SQLite.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import httpx
import pytest

from concord.api import ApiError, Client
from concord.observability import Recorder, _recorder


@contextmanager
def _active_recorder() -> Iterator[Recorder]:
    rec = Recorder(entity="bills", command="scrape bills", started_at=datetime.now(UTC))
    token = _recorder.set(rec)
    try:
        yield rec
    finally:
        _recorder.reset(token)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> Client:
    return Client(api_key="test-key", transport=httpx.MockTransport(handler), sleep=lambda _s: None)


def _sequenced(items: list[httpx.Response]) -> Callable[[httpx.Request], httpx.Response]:
    iterator = iter(items)

    def handler(_: httpx.Request) -> httpx.Response:
        return next(iterator)

    return handler


def _ok() -> httpx.Response:
    return httpx.Response(
        200,
        content=b'{"dailyCongressionalRecord": [], "pagination": {"count": 0}}',
        headers={"content-type": "application/json"},
    )


class TestSuccessRecording:
    def test_clean_request_bumps_bucket_and_emits_no_event(self) -> None:
        with _active_recorder() as rec, _client(lambda _r: _ok()) as client:
            client.list_issues()
        assert rec.successes == {"api:daily-record/list": 1}
        assert rec.events == []

    def test_no_recorder_is_a_noop(self) -> None:
        # Outside a run there is no recorder; the client must not raise.
        with _client(lambda _r: _ok()) as client:
            issues, _ = client.list_issues()
        assert issues == []


class TestErrorRecording:
    def test_503_then_200_emits_one_resolved_event(self) -> None:
        handler = _sequenced([httpx.Response(503), _ok()])
        with _active_recorder() as rec, _client(handler) as client:
            client.list_issues()
        assert rec.successes == {"api:daily-record/list": 1}
        assert len(rec.events) == 1
        event = rec.events[0]
        assert event.bucket == "api:daily-record/list"
        assert event.final_status == "resolved"
        assert [a.status for a in event.attempts] == [503]

    def test_terminal_503_emits_one_failed_event(self) -> None:
        handler = _sequenced([httpx.Response(503) for _ in range(6)])
        with (
            _active_recorder() as rec,
            _client(handler) as client,
            pytest.raises(ApiError, match="gave up after 5 attempts"),
        ):
            client.list_issues()
        assert rec.successes == {}
        assert len(rec.events) == 1
        event = rec.events[0]
        assert event.final_status == "failed"
        # Six attempts: the initial try + five retries, all 503.
        assert [a.status for a in event.attempts] == [503] * 6

    def test_429_then_200_records_resolved_event_with_429_attempt(self) -> None:
        handler = _sequenced([httpx.Response(429), _ok()])
        with _active_recorder() as rec, _client(handler) as client:
            client.list_issues()
        assert rec.successes == {"api:daily-record/list": 1}
        assert len(rec.events) == 1
        event = rec.events[0]
        assert event.final_status == "resolved"
        assert [a.status for a in event.attempts] == [429]

    def test_non_retryable_4xx_emits_failed_event(self) -> None:
        with (
            _active_recorder() as rec,
            _client(lambda _r: httpx.Response(404)) as client,
            pytest.raises(ApiError),
        ):
            client.list_issues()
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
            client.list_issues()
        assert len(rec.events) == 1
        attempt = rec.events[0].attempts[0]
        assert attempt.status is None
        assert attempt.transport_class == "ConnectError"
