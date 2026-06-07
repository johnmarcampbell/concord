"""senate_xml.py chokepoint recording — successful fetches bump the right
``senate:*`` bucket, error outcomes emit Run Events with retry resolution, and
the HTML-as-200 trap is recorded as a failed event (ADR 0021). Retry *behavior*
is covered by test_senate_xml.py and unchanged.

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
from concord.senate_xml import SenateClient, SenateXmlError

#: Minimal well-formed XML that the roster/menu parsers accept (empty result).
_XML_BODY = b"<doc></doc>"


@contextmanager
def _active_recorder() -> Iterator[Recorder]:
    rec = Recorder(entity="votes", command="scrape votes", started_at=datetime.now(UTC))
    token = _recorder.set(rec)
    try:
        yield rec
    finally:
        _recorder.reset(token)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> SenateClient:
    return SenateClient(transport=httpx.MockTransport(handler), sleep=lambda _s: None)


def _sequenced(items: list[httpx.Response]) -> Callable[[httpx.Request], httpx.Response]:
    iterator = iter(items)

    def handler(_: httpx.Request) -> httpx.Response:
        return next(iterator)

    return handler


def _ok_xml() -> httpx.Response:
    return httpx.Response(200, content=_XML_BODY, headers={"content-type": "application/xml"})


def _ok_html() -> httpx.Response:
    # The senate.gov "404-disguised-as-200" trap: a 200 carrying an HTML body.
    return httpx.Response(
        200, content=b"<html>not found</html>", headers={"content-type": "text/html"}
    )


class TestSuccessRecording:
    def test_clean_roster_fetch_bumps_roster_bucket(self) -> None:
        with _active_recorder() as rec, _client(lambda _r: _ok_xml()) as client:
            client.get_current_senators_xml()
        assert rec.successes == {"senate:roster": 1}
        assert rec.events == []

    def test_clean_menu_fetch_bumps_menu_bucket(self) -> None:
        with _active_recorder() as rec, _client(lambda _r: _ok_xml()) as client:
            client.list_roll_call_numbers(119, 1)
        assert rec.successes == {"senate:menu": 1}
        assert rec.events == []

    def test_clean_detail_fetch_bumps_detail_bucket(self) -> None:
        with _active_recorder() as rec, _client(lambda _r: _ok_xml()) as client:
            client.get_roll_call_xml(119, 1, 42)
        assert rec.successes == {"senate:detail": 1}
        assert rec.events == []

    def test_no_recorder_is_a_noop(self) -> None:
        with _client(lambda _r: _ok_xml()) as client:
            content = client.get_current_senators_xml()
        assert content == _XML_BODY


class TestErrorRecording:
    def test_503_then_200_emits_one_resolved_event(self) -> None:
        handler = _sequenced([httpx.Response(503), _ok_xml()])
        with _active_recorder() as rec, _client(handler) as client:
            client.get_current_senators_xml()
        assert rec.successes == {"senate:roster": 1}
        assert len(rec.events) == 1
        event = rec.events[0]
        assert event.endpoint_bucket == "senate:roster"
        assert event.final_status == "resolved"
        assert [a.status for a in event.attempts] == [503]

    def test_terminal_503_emits_one_failed_event(self) -> None:
        handler = _sequenced([httpx.Response(503) for _ in range(3)])
        with (
            _active_recorder() as rec,
            _client(handler) as client,
            pytest.raises(SenateXmlError, match="failed after 3 attempts"),
        ):
            client.get_current_senators_xml()
        assert rec.successes == {}
        assert len(rec.events) == 1
        event = rec.events[0]
        assert event.final_status == "failed"
        assert [a.status for a in event.attempts] == [503] * 3

    def test_non_200_emits_failed_event(self) -> None:
        with (
            _active_recorder() as rec,
            _client(lambda _r: httpx.Response(404)) as client,
            pytest.raises(SenateXmlError, match="returned 404"),
        ):
            client.get_roll_call_xml(119, 1, 42)
        assert rec.successes == {}
        assert len(rec.events) == 1
        assert rec.events[0].endpoint_bucket == "senate:detail"
        assert rec.events[0].final_status == "failed"
        assert rec.events[0].attempts[0].status == 404

    def test_transport_error_then_success_records_transport_class(self) -> None:
        calls = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("dns")
            return _ok_xml()

        with _active_recorder() as rec, _client(handler) as client:
            client.get_current_senators_xml()
        assert len(rec.events) == 1
        attempt = rec.events[0].attempts[0]
        assert attempt.status is None
        assert attempt.transport_class == "ConnectError"


class TestHtmlTrapRecording:
    def test_html_as_200_records_failed_event_with_marker(self) -> None:
        with (
            _active_recorder() as rec,
            _client(lambda _r: _ok_html()) as client,
            pytest.raises(SenateXmlError, match="HTML response"),
        ):
            client.get_roll_call_xml(119, 1, 42)
        # The HTML trap is not a success — no bucket bump, one failed event.
        assert rec.successes == {}
        assert len(rec.events) == 1
        event = rec.events[0]
        assert event.endpoint_bucket == "senate:detail"
        assert event.final_status == "failed"
        attempt = event.attempts[-1]
        assert attempt.status == 200
        assert attempt.message == "html-not-xml"
