"""Recorder — success counting, attempt capping + overflow, and
resolved/failed Run Event classification (ADR 0021)."""

from datetime import UTC, datetime

from concord.observability import Attempt, Recorder


def _recorder() -> Recorder:
    return Recorder(entity="bills", command="scrape bills", started_at=datetime.now(UTC))


def _attempts(n: int) -> list[Attempt]:
    return [
        Attempt(n=i + 1, status=503, transport_class=None, message="Service Unavailable")
        for i in range(n)
    ]


class TestSuccessCounting:
    def test_counts_per_bucket(self) -> None:
        rec = _recorder()
        rec.note_success("api", "/bill/119/hr")
        rec.note_success("api", "/bill/119/hr")
        rec.note_success("api", "/bill/119/hr/1")
        assert rec.successes == {"api:bill/list": 2, "api:bill/detail": 1}

    def test_no_successes_starts_empty(self) -> None:
        assert _recorder().successes == {}


class TestRunEventClassification:
    def test_resolved_event(self) -> None:
        rec = _recorder()
        rec.note_request_outcome("api", "/bill/119/hr/1", _attempts(2), resolved=True)
        assert len(rec.events) == 1
        event = rec.events[0]
        assert event.bucket == "api:bill/detail"
        assert event.final_status == "resolved"
        assert len(event.attempts) == 2
        assert event.overflow_count == 0
        assert event.ts  # a non-empty ISO timestamp

    def test_failed_event(self) -> None:
        rec = _recorder()
        rec.note_request_outcome("api", "/bill/119/hr", _attempts(6), resolved=False)
        assert rec.events[0].final_status == "failed"
        assert rec.events[0].bucket == "api:bill/list"

    def test_first_try_success_emits_no_event(self) -> None:
        rec = _recorder()
        rec.note_success("api", "/bill/119/hr")
        assert rec.events == []


class TestAttemptCapping:
    def test_under_cap_keeps_all_attempts(self) -> None:
        rec = _recorder()
        rec.note_request_outcome("api", "/bill/119/hr/1", _attempts(20), resolved=True)
        event = rec.events[0]
        assert len(event.attempts) == 20
        assert event.overflow_count == 0

    def test_over_cap_truncates_and_counts_overflow(self) -> None:
        rec = _recorder()
        rec.note_request_outcome("api", "/bill/119/hr/1", _attempts(25), resolved=True)
        event = rec.events[0]
        assert len(event.attempts) == 20
        assert event.overflow_count == 5
        # The earliest attempts are the ones retained.
        assert [a.n for a in event.attempts] == list(range(1, 21))

    def test_attempt_carries_transport_class(self) -> None:
        rec = _recorder()
        attempts = [Attempt(n=1, status=None, transport_class="ConnectError", message="boom")]
        rec.note_request_outcome("api", "/bill/119/hr", attempts, resolved=False)
        recorded = rec.events[0].attempts[0]
        assert recorded.status is None
        assert recorded.transport_class == "ConnectError"
