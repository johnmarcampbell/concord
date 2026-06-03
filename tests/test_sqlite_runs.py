"""runs / run_events record tables — insert/read round-trip and the
schema-equivalence guarantee at the new _HEAD (ADR 0017, 0021)."""

import json
import sqlite3
from pathlib import Path

from concord.storage.sqlite import _HEAD, SqliteStorage, ensure_schema


class TestRunsRoundTrip:
    def test_insert_and_read_run(self, tmp_path: Path) -> None:
        db_path = tmp_path / "ledger.db"
        storage = SqliteStorage(db_path, load_vec=False)
        try:
            storage.insert_run(
                run_id="20260603T120000-deadbeef",
                entity="bills",
                command="scrape bills",
                started_at="2026-06-03T12:00:00+00:00",
                ended_at="2026-06-03T12:05:00+00:00",
                status="ok",
                success_counts={"api:bill/list": 3, "api:bill/detail": 12},
                throttle_counts=None,
                unmatched_sample=[],
                error_event_count=0,
            )
            row = storage.get_run("20260603T120000-deadbeef")
        finally:
            storage.close()

        assert row is not None
        assert row["entity"] == "bills"
        assert row["status"] == "ok"
        assert json.loads(row["success_counts"]) == {"api:bill/detail": 12, "api:bill/list": 3}
        # Empty optional JSON columns collapse to NULL.
        assert row["throttle_counts"] is None
        assert row["unmatched_sample"] is None

    def test_insert_run_events_round_trip(self, tmp_path: Path) -> None:
        db_path = tmp_path / "ledger.db"
        storage = SqliteStorage(db_path, load_vec=False)
        try:
            storage.insert_run(
                run_id="r1",
                entity="bills",
                command="scrape bills",
                started_at="2026-06-03T12:00:00+00:00",
                ended_at="2026-06-03T12:05:00+00:00",
                status="partial",
                success_counts={"api:bill/list": 1},
                throttle_counts=None,
                unmatched_sample=["/odd/path"],
                error_event_count=1,
            )
            storage.insert_run_events(
                "r1",
                [
                    {
                        "endpoint_bucket": "api:bill/detail",
                        "attempts": [{"n": 1, "status": 503, "transport_class": None, "msg": "x"}],
                        "overflow_count": 0,
                        "final_status": "resolved",
                        "ts": "2026-06-03T12:01:00+00:00",
                    }
                ],
            )
            events = storage.list_run_events("r1")
            run = storage.get_run("r1")
        finally:
            storage.close()

        assert run is not None
        assert json.loads(run["unmatched_sample"]) == ["/odd/path"]
        assert len(events) == 1
        assert events[0]["seq"] == 0
        assert events[0]["endpoint_bucket"] == "api:bill/detail"
        assert json.loads(events[0]["attempts"])[0]["status"] == 503

    def test_empty_events_is_a_noop(self, tmp_path: Path) -> None:
        db_path = tmp_path / "ledger.db"
        storage = SqliteStorage(db_path, load_vec=False)
        try:
            storage.insert_run(
                run_id="r2",
                entity="bills",
                command="scrape bills",
                started_at="2026-06-03T12:00:00+00:00",
                ended_at=None,
                status="ok",
                success_counts={},
                throttle_counts=None,
                unmatched_sample=[],
                error_event_count=0,
            )
            storage.insert_run_events("r2", [])
            assert storage.list_run_events("r2") == []
        finally:
            storage.close()

    def test_run_events_cascade_on_run_delete(self, tmp_path: Path) -> None:
        db_path = tmp_path / "ledger.db"
        storage = SqliteStorage(db_path, load_vec=False)
        try:
            storage.insert_run(
                run_id="r3",
                entity="bills",
                command="scrape bills",
                started_at="2026-06-03T12:00:00+00:00",
                ended_at=None,
                status="ok",
                success_counts={},
                throttle_counts=None,
                unmatched_sample=[],
                error_event_count=1,
            )
            storage.insert_run_events(
                "r3",
                [
                    {
                        "endpoint_bucket": "api:bill/list",
                        "attempts": [],
                        "overflow_count": 0,
                        "final_status": "failed",
                        "ts": "2026-06-03T12:01:00+00:00",
                    }
                ],
            )
            storage._conn.execute("DELETE FROM runs WHERE run_id = ?", ("r3",))
            storage._conn.commit()
            assert storage.list_run_events("r3") == []
        finally:
            storage.close()


class TestSchemaVersion:
    def test_head_is_three(self) -> None:
        assert _HEAD == 3

    def test_fresh_db_has_runs_tables_at_head(self, tmp_path: Path) -> None:
        db_path = tmp_path / "ledger.db"
        ensure_schema(db_path)
        conn = sqlite3.connect(db_path)
        try:
            tables = {
                name
                for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
        assert {"runs", "run_events"} <= tables
        assert version == 3
