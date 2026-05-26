"""Tests for the Bills Stage 1 (load) and Stage 2 (index) pipelines."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from concord.pipeline.index_bills import index as index_bills
from concord.pipeline.load_bills import load as load_bills
from concord.scraper.bills import BILLS_JSONL_NAME
from concord.storage.sqlite import SqliteStorage

from ._snapshots import wrap_snapshot

FIXED_FETCHED_AT = datetime(2026, 5, 25, 14, 2, 11, tzinfo=UTC)


def _fixture(name: str) -> dict[str, Any]:
    here = Path(__file__).parent / "fixtures" / "api" / "bills"
    return json.loads((here / name).read_text())


def _detail_payload(name: str) -> dict[str, Any]:
    """Return just the unwrapped ``bill`` object the scraper persists."""
    return _fixture(name)["bill"]


def _envelope_for(
    payload: dict[str, Any],
    *,
    fetched_at: datetime = FIXED_FETCHED_AT,
) -> dict[str, Any]:
    return wrap_snapshot(
        payload,
        fetched_at=fetched_at,
        key={
            "congress": int(payload["congress"]),
            "bill_type": str(payload["type"]).lower(),
            "bill_number": int(payload["number"]),
        },
    )


def _write_jsonl(storage_dir: Path, envelopes: list[dict[str, Any]]) -> None:
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / BILLS_JSONL_NAME).write_text(
        "\n".join(json.dumps(e) for e in envelopes) + "\n",
        encoding="utf-8",
    )


class TestLoadBills:
    def test_basic_load(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        _write_jsonl(
            storage_dir,
            [
                _envelope_for(_detail_payload("detail_119_hr_1.json")),
                _envelope_for(_detail_payload("detail_119_hr_22.json")),
            ],
        )

        stats = load_bills(storage_dir=storage_dir, db_path=db)
        assert stats.bills_written == 2
        assert stats.snapshots_read == 2
        assert stats.malformed == 0

        with SqliteStorage(db, load_vec=False) as storage:
            row = storage.get_bill("119-hr-1")
            assert row is not None
            assert row["title"] == "Lower Energy Costs Act"
            assert row["sponsor_bioguide_id"] == "S001176"
            assert row["policy_area"] == "Energy"

    def test_latest_snapshot_wins(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        early = _detail_payload("detail_119_hr_1.json")
        late = {**early, "title": "Renamed Bill"}
        _write_jsonl(
            storage_dir,
            [
                _envelope_for(early, fetched_at=FIXED_FETCHED_AT),
                _envelope_for(late, fetched_at=FIXED_FETCHED_AT + timedelta(hours=1)),
            ],
        )
        load_bills(storage_dir=storage_dir, db_path=db)
        with SqliteStorage(db, load_vec=False) as storage:
            row = storage.get_bill("119-hr-1")
            assert row is not None
            assert row["title"] == "Renamed Bill"

    def test_limit_caps_writes(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        _write_jsonl(
            storage_dir,
            [
                _envelope_for(_detail_payload("detail_119_hr_1.json")),
                _envelope_for(_detail_payload("detail_119_hr_22.json")),
            ],
        )
        stats = load_bills(storage_dir=storage_dir, db_path=db, limit=1)
        assert stats.bills_written == 1

    def test_missing_file_is_noop(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "no_such_dir"
        db = tmp_path / "test.db"
        stats = load_bills(storage_dir=storage_dir, db_path=db)
        assert stats == (0, 0, 0)

    def test_idempotent_rerun(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        _write_jsonl(
            storage_dir,
            [_envelope_for(_detail_payload("detail_119_hr_1.json"))],
        )
        load_bills(storage_dir=storage_dir, db_path=db)
        load_bills(storage_dir=storage_dir, db_path=db)
        with SqliteStorage(db, load_vec=False) as storage:
            count = storage.connection.execute("SELECT COUNT(*) FROM bills").fetchone()[0]
            assert count == 1


class TestIndexBills:
    def test_repopulates_fts(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        _write_jsonl(
            storage_dir,
            [
                _envelope_for(_detail_payload("detail_119_hr_1.json")),
                _envelope_for(_detail_payload("detail_119_hr_22.json")),
            ],
        )
        load_bills(storage_dir=storage_dir, db_path=db)
        stats = index_bills(db_path=db)
        assert stats.indexed_bills == 2
        with SqliteStorage(db, load_vec=False) as storage:
            rows = storage.connection.execute(
                "SELECT bill_id, identifier FROM bills_fts ORDER BY bill_id"
            ).fetchall()
            assert [r["bill_id"] for r in rows] == ["119-hr-1", "119-hr-22"]
            assert rows[0]["identifier"] == "hr 1"

    def test_index_is_idempotent(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        _write_jsonl(
            storage_dir,
            [_envelope_for(_detail_payload("detail_119_hr_1.json"))],
        )
        load_bills(storage_dir=storage_dir, db_path=db)
        index_bills(db_path=db)
        index_bills(db_path=db)
        with SqliteStorage(db, load_vec=False) as storage:
            (count,) = storage.connection.execute("SELECT COUNT(*) FROM bills_fts").fetchone()
            assert count == 1

    def test_fts_matches_title_and_policy_area(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        _write_jsonl(
            storage_dir,
            [
                _envelope_for(_detail_payload("detail_119_hr_1.json")),
                _envelope_for(_detail_payload("detail_119_hr_22.json")),
            ],
        )
        load_bills(storage_dir=storage_dir, db_path=db)
        index_bills(db_path=db)
        with SqliteStorage(db, load_vec=False) as storage:
            rows = storage.connection.execute(
                "SELECT bill_id FROM bills_fts WHERE bills_fts MATCH ?",
                ("energy",),
            ).fetchall()
            assert [r["bill_id"] for r in rows] == ["119-hr-1"]
