"""Tests for the Bills SQLite storage layer (Phase 2a)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from concord.models import (
    Bill,
    BillAction,
    BillSubject,
    BillSummary,
    BillTitle,
    Cosponsor,
)
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
            "INSERT INTO bills_fts "
            "(bill_id, identifier, title, policy_area, short_title, subjects) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("119-hr-1", "hr 1", "Lower Energy Costs Act", "Energy", "", ""),
        )
        rows = storage.connection.execute(
            "SELECT bill_id FROM bills_fts WHERE bills_fts MATCH ?",
            ("energy",),
        ).fetchall()
        assert [r["bill_id"] for r in rows] == ["119-hr-1"]

    def test_short_title_column_searchable(self, storage: SqliteStorage) -> None:
        storage.connection.execute(
            "INSERT INTO bills_fts "
            "(bill_id, identifier, title, policy_area, short_title, subjects) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("119-hr-1", "hr 1", "Long Official Title", None, "Cool Bill", ""),
        )
        rows = storage.connection.execute(
            "SELECT bill_id FROM bills_fts WHERE bills_fts MATCH ?",
            ("cool",),
        ).fetchall()
        assert [r["bill_id"] for r in rows] == ["119-hr-1"]


class TestBillTier2:
    def _seed_bill(self, storage: SqliteStorage) -> str:
        storage.upsert_bill(_bill(), fetched_at="2026-05-25T14:02:11+00:00")
        return "119-hr-1"

    def test_replace_cosponsors_inserts_and_stamps(self, storage: SqliteStorage) -> None:
        bill_id = self._seed_bill(storage)
        storage.replace_bill_cosponsors(
            bill_id,
            [
                Cosponsor(
                    bioguide_id="A000001", sponsorship_date="2025-01-09", is_original_cosponsor=True
                ),
                Cosponsor(
                    bioguide_id="B000002",
                    sponsorship_date="2025-02-04",
                    is_original_cosponsor=False,
                ),
            ],
            fetched_at="2026-05-26T00:00:00+00:00",
        )
        rows = storage.cosponsors_for_bill(bill_id)
        assert {r["bioguide_id"] for r in rows} == {"A000001", "B000002"}
        # is_original DESC → A000001 first.
        assert rows[0]["bioguide_id"] == "A000001"
        assert rows[0]["is_original_cosponsor"] == 1
        row = storage.get_bill(bill_id)
        assert row is not None
        assert row["cosponsors_fetched_at"] == "2026-05-26T00:00:00+00:00"

    def test_replace_cosponsors_is_idempotent(self, storage: SqliteStorage) -> None:
        bill_id = self._seed_bill(storage)
        cosponsors = [Cosponsor(bioguide_id="A000001")]
        storage.replace_bill_cosponsors(bill_id, cosponsors, fetched_at="2026-05-26T00:00:00Z")
        storage.replace_bill_cosponsors(bill_id, cosponsors, fetched_at="2026-05-27T00:00:00Z")
        rows = storage.cosponsors_for_bill(bill_id)
        assert len(rows) == 1

    def test_replace_cosponsors_drops_removed(self, storage: SqliteStorage) -> None:
        bill_id = self._seed_bill(storage)
        storage.replace_bill_cosponsors(
            bill_id,
            [Cosponsor(bioguide_id="A000001"), Cosponsor(bioguide_id="B000002")],
            fetched_at="2026-05-26T00:00:00Z",
        )
        storage.replace_bill_cosponsors(
            bill_id,
            [Cosponsor(bioguide_id="A000001")],
            fetched_at="2026-05-27T00:00:00Z",
        )
        rows = storage.cosponsors_for_bill(bill_id)
        assert [r["bioguide_id"] for r in rows] == ["A000001"]

    def test_replace_actions(self, storage: SqliteStorage) -> None:
        bill_id = self._seed_bill(storage)
        storage.replace_bill_actions(
            bill_id,
            [
                BillAction(action_date="2026-03-30", action_text="Became law"),
                BillAction(action_date="2025-01-09", action_text="Introduced"),
            ],
            fetched_at="2026-05-26T00:00:00Z",
        )
        rows = storage.actions_for_bill(bill_id)
        assert [r["action_text"] for r in rows] == ["Became law", "Introduced"]
        row = storage.get_bill(bill_id)
        assert row is not None
        assert row["actions_fetched_at"] == "2026-05-26T00:00:00Z"

    def test_actions_returned_newest_first_regardless_of_ord(self, storage: SqliteStorage) -> None:
        """Regression: ordering uses action_date, not the insert-order ord."""
        bill_id = self._seed_bill(storage)
        # Insert with intentionally-shuffled date order: middle, newest, oldest.
        storage.replace_bill_actions(
            bill_id,
            [
                BillAction(action_date="2025-06-15", action_text="middle"),
                BillAction(action_date="2026-03-30", action_text="newest"),
                BillAction(action_date="2025-01-09", action_text="oldest"),
            ],
            fetched_at="2026-05-26T00:00:00Z",
        )
        rows = storage.actions_for_bill(bill_id)
        assert [r["action_text"] for r in rows] == ["newest", "middle", "oldest"]

    def test_replace_subjects_deduplicates(self, storage: SqliteStorage) -> None:
        bill_id = self._seed_bill(storage)
        storage.replace_bill_subjects(
            bill_id,
            [BillSubject(name="Energy"), BillSubject(name="Energy"), BillSubject(name="Gas")],
            fetched_at="2026-05-26T00:00:00Z",
        )
        rows = storage.subjects_for_bill(bill_id)
        assert {r["subject"] for r in rows} == {"Energy", "Gas"}

    def test_replace_titles(self, storage: SqliteStorage) -> None:
        bill_id = self._seed_bill(storage)
        storage.replace_bill_titles(
            bill_id,
            [
                BillTitle(title_type="Display Title", title_text="Cool Act"),
                BillTitle(title_type="Short Title(s) as Introduced", title_text="Cool Act"),
            ],
            fetched_at="2026-05-26T00:00:00Z",
        )
        rows = storage.titles_for_bill(bill_id)
        assert len(rows) == 2
        assert rows[0]["title_type"] == "Display Title"

    def test_replace_summaries_dedups_by_version(self, storage: SqliteStorage) -> None:
        bill_id = self._seed_bill(storage)
        storage.replace_bill_summaries(
            bill_id,
            [
                BillSummary(version_code="00", summary_text="<p>a</p>"),
                BillSummary(version_code="18", summary_text="<p>b</p>"),
                BillSummary(version_code="00", summary_text="<p>a-newer</p>"),
            ],
            fetched_at="2026-05-26T00:00:00Z",
        )
        rows = storage.summaries_for_bill(bill_id)
        assert {r["version_code"] for r in rows} == {"00", "18"}
        text_00 = next(r["summary_text"] for r in rows if r["version_code"] == "00")
        assert text_00 == "<p>a-newer</p>"

    def test_bill_delete_cascades_to_children(self, storage: SqliteStorage) -> None:
        bill_id = self._seed_bill(storage)
        storage.replace_bill_cosponsors(
            bill_id, [Cosponsor(bioguide_id="A000001")], fetched_at="2026-05-26T00:00:00Z"
        )
        storage.replace_bill_actions(
            bill_id,
            [BillAction(action_date="2025-01-09", action_text="Introduced")],
            fetched_at="2026-05-26T00:00:00Z",
        )
        storage.connection.execute("DELETE FROM bills WHERE bill_id = ?", (bill_id,))
        storage.connection.commit()
        assert storage.cosponsors_for_bill(bill_id) == []
        assert storage.actions_for_bill(bill_id) == []

    def test_upsert_bill_preserves_fetched_at_stamps(self, storage: SqliteStorage) -> None:
        """Re-running tier-1 upsert must NOT reset *_fetched_at columns."""
        bill_id = self._seed_bill(storage)
        storage.replace_bill_cosponsors(
            bill_id, [Cosponsor(bioguide_id="A000001")], fetched_at="2026-05-26T00:00:00Z"
        )
        # Now re-upsert the parent bill row with a new title.
        storage.upsert_bill(_bill(title="Renamed"), fetched_at="2026-06-01T00:00:00+00:00")
        row = storage.get_bill(bill_id)
        assert row is not None
        assert row["title"] == "Renamed"
        # The tier-2 stamp must still be set.
        assert row["cosponsors_fetched_at"] == "2026-05-26T00:00:00Z"
