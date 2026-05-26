"""Tests for the Bills SQLite storage layer (Phase 2a)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from concord.models import Bill
from concord.storage.sqlite import SqliteStorage


def _bill(
    bill_id: str = "119-hr-1",
    *,
    congress: int = 119,
    bill_type: str = "hr",
    bill_number: int = 1,
    origin_chamber: str = "House",
    title: str = "Lower Energy Costs Act",
    sponsor_bioguide_id: str | None = "S001176",
    policy_area: str | None = "Energy",
    latest_action_date: str | None = "2026-03-30",
    latest_action_text: str | None = "Became Public Law No: 119-1.",
    introduced_date: str | None = "2025-01-09",
    update_date: str = "2026-04-01T12:34:56Z",
) -> Bill:
    return Bill(
        bill_id=bill_id,
        congress=congress,
        bill_type=bill_type,  # type: ignore[arg-type]
        bill_number=bill_number,
        origin_chamber=origin_chamber,  # type: ignore[arg-type]
        title=title,
        introduced_date=introduced_date,
        policy_area=policy_area,
        sponsor_bioguide_id=sponsor_bioguide_id,
        latest_action_date=latest_action_date,
        latest_action_text=latest_action_text,
        update_date=update_date,
    )


@pytest.fixture
def storage(tmp_path: Path) -> SqliteStorage:
    s = SqliteStorage(tmp_path / "test.db", load_vec=False)
    yield s
    s.close()


class TestUpsertBill:
    def test_inserts_and_reads(self, storage: SqliteStorage) -> None:
        storage.upsert_bill(_bill(), fetched_at="2026-05-25T14:02:11+00:00")
        row = storage.get_bill("119-hr-1")
        assert row is not None
        assert row["title"] == "Lower Energy Costs Act"
        assert row["sponsor_bioguide_id"] == "S001176"
        assert row["policy_area"] == "Energy"
        assert row["latest_action_date"] == "2026-03-30"
        assert row["fetched_at"] == "2026-05-25T14:02:11+00:00"

    def test_upsert_replaces_row(self, storage: SqliteStorage) -> None:
        storage.upsert_bill(
            _bill(title="Original Title"),
            fetched_at="2026-01-01T00:00:00+00:00",
        )
        storage.upsert_bill(
            _bill(title="Updated Title", latest_action_date="2026-04-15"),
            fetched_at="2026-05-25T00:00:00+00:00",
        )
        row = storage.get_bill("119-hr-1")
        assert row is not None
        assert row["title"] == "Updated Title"
        assert row["latest_action_date"] == "2026-04-15"
        assert row["fetched_at"] == "2026-05-25T00:00:00+00:00"

    def test_get_missing_returns_none(self, storage: SqliteStorage) -> None:
        assert storage.get_bill("119-hr-9999") is None


class TestSchemaConstraints:
    def test_bill_type_check_rejects_unknown(self, storage: SqliteStorage) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            storage.connection.execute(
                "INSERT INTO bills "
                "(bill_id, congress, bill_type, bill_number, origin_chamber, "
                "title, update_date, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("119-xx-1", 119, "xx", 1, "House", "x", "2026-04-01", "2026-05-25T00:00:00Z"),
            )

    def test_origin_chamber_check_rejects_unknown(self, storage: SqliteStorage) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            storage.connection.execute(
                "INSERT INTO bills "
                "(bill_id, congress, bill_type, bill_number, origin_chamber, "
                "title, update_date, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("119-hr-9", 119, "hr", 9, "Lords", "x", "2026-04-01", "2026-05-25T00:00:00Z"),
            )


class TestBillsFtsTable:
    def test_table_exists_and_is_queryable(self, storage: SqliteStorage) -> None:
        storage.connection.execute(
            "INSERT INTO bills_fts (bill_id, identifier, title, policy_area) VALUES (?, ?, ?, ?)",
            ("119-hr-1", "hr 1", "Lower Energy Costs Act", "Energy"),
        )
        rows = storage.connection.execute(
            "SELECT bill_id FROM bills_fts WHERE bills_fts MATCH ?",
            ("energy",),
        ).fetchall()
        assert [r["bill_id"] for r in rows] == ["119-hr-1"]
