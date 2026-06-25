"""Tests for the shared resilient fetch module."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import httpx
import pytest

from concord.fetch import Fetcher, FetchError, RetryAfterPolicy
from concord.observability import Recorder, _recorder


@contextmanager
def _active_recorder() -> Iterator[Recorder]:
    rec = Recorder(entity="bills", command="scrape bills", started_at=datetime.now(UTC))
    token = _recorder.set(rec)
    try:
        yield rec
    finally:
        _recorder.reset(token)


def _fetcher(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    sleep: Callable[[float], None] | None = None,
    policy: RetryAfterPolicy | None = None,
) -> Fetcher:
    client = httpx.Client(
        base_url="https://api.congress.gov/v3", transport=httpx.MockTransport(handler)
    )
    return Fetcher(
        client,
        source="api",
        policy=policy,
        sleep=sleep or (lambda _s: None),
    )


def _sequenced(
    items: list[BaseException | httpx.Response],
) -> Callable[[httpx.Request], httpx.Response]:
    iterator = iter(items)

    def handler(_: httpx.Request) -> httpx.Response:
        item = next(iterator)
        if isinstance(item, httpx.Response):
            return item
        raise item

    return handler


def _ok() -> httpx.Response:
    return httpx.Response(
        200, content=b'{"ok": true}', headers={"content-type": "application/json"}
    )


class TestFetchSuccessRecording:
    def test_clean_request_bumps_bucket_and_emits_no_event(self) -> None:
        with _active_recorder() as rec:
            fetcher = _fetcher(lambda _r: _ok())
            body = fetcher.get("/daily-congressional-record")
        assert body == b'{"ok": true}'
        assert rec.successes == {"api:daily-record/list": 1}
        assert rec.events == []

    def test_no_recorder_is_a_noop(self) -> None:
        fetcher = _fetcher(lambda _r: _ok())
        assert fetcher.get("/daily-congressional-record") == b'{"ok": true}'


class TestFetchErrorRecording:
    def test_503_then_200_emits_one_resolved_event(self) -> None:
        handler = _sequenced([httpx.Response(503), _ok()])
        with _active_recorder() as rec:
            fetcher = _fetcher(handler)
            fetcher.get("/daily-congressional-record")
        assert rec.successes == {"api:daily-record/list": 1}
        assert len(rec.events) == 1
        event = rec.events[0]
        assert event.endpoint_bucket == "api:daily-record/list"
        assert event.final_status == "resolved"
        assert [a.status for a in event.attempts] == [503]

    def test_terminal_503_emits_one_failed_event(self) -> None:
        handler = _sequenced([httpx.Response(503) for _ in range(6)])
        fetcher = _fetcher(handler)
        with _active_recorder() as rec, pytest.raises(FetchError, match="gave up after 5 attempts"):
            fetcher.get("/daily-congressional-record")
        assert rec.successes == {}
        assert len(rec.events) == 1
        assert rec.events[0].final_status == "failed"
        assert [a.status for a in rec.events[0].attempts] == [503] * 6

    def test_non_retryable_404_emits_failed_event(self) -> None:
        fetcher = _fetcher(lambda _r: httpx.Response(404))
        with _active_recorder() as rec, pytest.raises(FetchError) as exc:
            fetcher.get("/daily-congressional-record")
        assert exc.value.status_code == 404
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

        with _active_recorder() as rec:
            fetcher = _fetcher(handler)
            fetcher.get("/daily-congressional-record")
        assert len(rec.events) == 1
        attempt = rec.events[0].attempts[0]
        assert attempt.status is None
        assert attempt.transport_class == "ConnectError"


class TestRetryAfterPolicy:
    def test_retry_after_is_honored_and_recorded(self) -> None:
        sleeps: list[float] = []
        policy = RetryAfterPolicy(sleep=sleeps.append, source="api")
        handler = _sequenced([httpx.Response(429, headers={"Retry-After": "3"}), _ok()])

        with _active_recorder() as rec:
            fetcher = _fetcher(handler, sleep=sleeps.append, policy=policy)
            fetcher.get("/daily-congressional-record")

        assert sleeps == [3.0]
        assert rec.successes == {"api:daily-record/list": 1}
        assert len(rec.events) == 1
        assert rec.events[0].final_status == "resolved"
        assert [a.status for a in rec.events[0].attempts] == [429]

    def test_429s_do_not_consume_transient_budget(self) -> None:
        sleeps: list[float] = []
        policy = RetryAfterPolicy(sleep=sleeps.append, source="api")
        responses = [httpx.Response(429) for _ in range(8)]
        responses.append(_ok())

        with _active_recorder() as rec:
            fetcher = _fetcher(_sequenced(responses), sleep=sleeps.append, policy=policy)
            fetcher.get("/daily-congressional-record")

        assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]
        assert rec.successes == {"api:daily-record/list": 1}
        assert len(rec.events) == 1
        assert rec.events[0].final_status == "resolved"
        assert [a.status for a in rec.events[0].attempts] == [429] * 8
