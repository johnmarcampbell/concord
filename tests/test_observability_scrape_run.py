"""scrape_run lifecycle — enter/exit persists a runs row + run_events, the
JSONL backup is appended, and an exception in the body still flushes with
status="error" (ADR 0021)."""

import json
from pathlib import Path

import pytest

from concord.models import Attempt
from concord.observability import active_recorder, current_run_id, scrape_run
from concord.storage.sqlite import SqliteStorage


def _read_run(db_path: Path, run_id: str) -> dict[str, object]:
    storage = SqliteStorage(db_path, load_vec=False)
    try:
        row = storage.get_run(run_id)
        assert row is not None
        return dict(row)
    finally:
        storage.close()


class TestScrapeRunPersistence:
    def test_clean_run_persists_success_counts(self, tmp_path: Path) -> None:
        db_path = tmp_path / "ledger.db"
        with scrape_run(entity="bills", command="scrape bills", db_path=db_path) as rec:
            run_id = current_run_id()
            assert run_id is not None
            assert active_recorder() is rec
            rec.note_success("api", "/bill/119/hr")
            rec.note_success("api", "/bill/119/hr/1")

        # contextvars reset on exit
        assert current_run_id() is None
        assert active_recorder() is None

        row = _read_run(db_path, run_id)
        assert row["status"] == "ok"
        assert row["entity"] == "bills"
        assert row["command"] == "scrape bills"
        assert json.loads(row["success_counts"]) == {"api:bill/list": 1, "api:bill/detail": 1}
        assert row["error_event_count"] == 0
        assert row["ended_at"]

    def test_run_events_persist_with_seq(self, tmp_path: Path) -> None:
        db_path = tmp_path / "ledger.db"
        with scrape_run(entity="bills", command="scrape bills", db_path=db_path) as rec:
            run_id = current_run_id()
            rec.note_request_outcome(
                "api",
                "/bill/119/hr/1",
                [Attempt(n=1, status=503, transport_class=None, message="x")],
                resolved=True,
            )
            rec.note_request_outcome(
                "api",
                "/bill/119/hr",
                [Attempt(n=1, status=500, transport_class=None, message="y")],
                resolved=False,
            )

        assert run_id is not None
        storage = SqliteStorage(db_path, load_vec=False)
        try:
            events = storage.list_run_events(run_id)
        finally:
            storage.close()
        assert [e["seq"] for e in events] == [0, 1]
        assert events[0]["final_status"] == "resolved"
        assert events[1]["final_status"] == "failed"
        assert json.loads(events[0]["attempts"])[0]["status"] == 503

    def test_failed_event_without_body_error_is_partial(self, tmp_path: Path) -> None:
        db_path = tmp_path / "ledger.db"
        with scrape_run(entity="bills", command="scrape bills", db_path=db_path) as rec:
            run_id = current_run_id()
            rec.note_request_outcome(
                "api",
                "/bill/119/hr",
                [Attempt(n=1, status=500, transport_class=None, message="y")],
                resolved=False,
            )
        assert run_id is not None
        assert _read_run(db_path, run_id)["status"] == "partial"

    def test_jsonl_backup_is_appended(self, tmp_path: Path) -> None:
        db_path = tmp_path / "ledger.db"
        with scrape_run(entity="bills", command="scrape bills", db_path=db_path) as rec:
            run_id = current_run_id()
            rec.note_success("api", "/bill/119/hr")

        backup = tmp_path / "runs.jsonl"
        assert backup.exists()
        lines = backup.read_text().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["run_id"] == run_id
        assert record["success_counts"] == {"api:bill/list": 1}
        assert record["events"] == []

    def test_custom_data_dir_for_backup(self, tmp_path: Path) -> None:
        db_path = tmp_path / "db" / "ledger.db"
        backup_dir = tmp_path / "audit"
        with scrape_run(
            entity="bills",
            command="scrape bills",
            db_path=db_path,
            data_dir=backup_dir,
        ):
            pass
        assert (backup_dir / "runs.jsonl").exists()


class TestScrapeRunExceptionPath:
    def test_body_exception_still_flushes_error_run(self, tmp_path: Path) -> None:
        db_path = tmp_path / "ledger.db"
        captured: dict[str, object] = {}

        def _crash() -> None:
            with scrape_run(entity="bills", command="scrape bills", db_path=db_path) as rec:
                captured["run_id"] = current_run_id()
                rec.note_success("api", "/bill/119/hr")
                raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            _crash()

        run_id = captured["run_id"]
        assert isinstance(run_id, str)
        row = _read_run(db_path, run_id)
        assert row["status"] == "error"
        # Work done before the crash is still recorded.
        assert json.loads(row["success_counts"]) == {"api:bill/list": 1}

    def test_each_run_gets_a_unique_id(self, tmp_path: Path) -> None:
        db_path = tmp_path / "ledger.db"
        ids = []
        for _ in range(3):
            with scrape_run(entity="bills", command="scrape bills", db_path=db_path):
                ids.append(current_run_id())
        assert len(set(ids)) == 3
