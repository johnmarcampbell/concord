"""Tests for the Bills Stage 1 (load) and Stage 2 (index) pipelines."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from concord.pipeline.index_bills import index as index_bills
from concord.pipeline.load_bills import load as load_bills
from concord.scraper.bills import BILLS_JSONL_NAME, enrichment_jsonl_name
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


def _envelope_for_section(
    payload: dict[str, Any],
    *,
    bill_key: tuple[int, str, int],
    fetched_at: datetime = FIXED_FETCHED_AT,
) -> dict[str, Any]:
    congress, bill_type, bill_number = bill_key
    return wrap_snapshot(
        payload,
        fetched_at=fetched_at,
        key={
            "congress": congress,
            "bill_type": bill_type.lower(),
            "bill_number": bill_number,
        },
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
        assert stats.bills_written == 0
        assert stats.snapshots_read == 0
        assert stats.malformed == 0
        assert stats.tier2_snapshots_read == 0
        assert stats.tier2_bills_updated == 0
        assert stats.tier2_orphans_skipped == 0

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


class TestLoadBillsTier2:
    def _write_tier1(self, storage_dir: Path) -> None:
        _write_jsonl(
            storage_dir,
            [
                _envelope_for(_detail_payload("detail_119_hr_1.json")),
                _envelope_for(_detail_payload("detail_119_hr_22.json")),
            ],
        )

    def test_tier1_only_leaves_fetched_at_columns_null(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        self._write_tier1(storage_dir)
        load_bills(storage_dir=storage_dir, db_path=db)
        with SqliteStorage(db, load_vec=False) as storage:
            row = storage.get_bill("119-hr-1")
            assert row is not None
            assert row["cosponsors_fetched_at"] is None
            assert row["actions_fetched_at"] is None
            assert row["subjects_fetched_at"] is None
            assert row["titles_fetched_at"] is None
            assert row["summaries_fetched_at"] is None

    def test_loads_all_five_sections_when_present(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        self._write_tier1(storage_dir)

        # Write tier-2 files for 119-hr-1.
        section_to_fixture = {
            "cosponsors": "cosponsors_119_hr_22.json",
            "actions": "actions_119_hr_1.json",
            "subjects": "subjects_119_hr_1.json",
            "titles": "titles_119_hr_1.json",
            "summaries": "summaries_119_hr_1.json",
        }
        for section, name in section_to_fixture.items():
            (storage_dir / enrichment_jsonl_name(section)).write_text(
                json.dumps(_envelope_for_section(_fixture(name), bill_key=(119, "hr", 1))) + "\n",
                encoding="utf-8",
            )

        stats = load_bills(storage_dir=storage_dir, db_path=db)
        assert stats.tier2_bills_updated == 5  # one bill, five sections
        assert stats.tier2_orphans_skipped == 0

        with SqliteStorage(db, load_vec=False) as storage:
            row = storage.get_bill("119-hr-1")
            assert row is not None
            assert row["cosponsors_fetched_at"] is not None
            assert row["actions_fetched_at"] is not None
            cosponsors = storage.cosponsors_for_bill("119-hr-1")
            assert len(cosponsors) == 3
            actions = storage.actions_for_bill("119-hr-1")
            assert len(actions) == 6
            subjects = storage.subjects_for_bill("119-hr-1")
            assert len(subjects) == 10
            titles = storage.titles_for_bill("119-hr-1")
            assert len(titles) == 4
            summaries = storage.summaries_for_bill("119-hr-1")
            assert len(summaries) == 3
            # The other bill (119-hr-22) stays tier-1-only.
            other = storage.get_bill("119-hr-22")
            assert other is not None
            assert other["cosponsors_fetched_at"] is None

    def test_tier2_orphan_skipped(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        storage_dir.mkdir(parents=True, exist_ok=True)
        # Tier-2 only, with a bill_id that isn't in bills.jsonl (because
        # bills.jsonl doesn't exist).
        (storage_dir / enrichment_jsonl_name("cosponsors")).write_text(
            json.dumps(
                _envelope_for_section(
                    _fixture("cosponsors_119_hr_22.json"), bill_key=(119, "hr", 22)
                )
            )
            + "\n",
            encoding="utf-8",
        )
        stats = load_bills(storage_dir=storage_dir, db_path=db)
        assert stats.bills_written == 0
        assert stats.tier2_orphans_skipped == 1

    def test_idempotent_rerun_tier2(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        self._write_tier1(storage_dir)
        (storage_dir / enrichment_jsonl_name("cosponsors")).write_text(
            json.dumps(
                _envelope_for_section(
                    _fixture("cosponsors_119_hr_22.json"), bill_key=(119, "hr", 1)
                )
            )
            + "\n",
            encoding="utf-8",
        )
        load_bills(storage_dir=storage_dir, db_path=db)
        load_bills(storage_dir=storage_dir, db_path=db)
        with SqliteStorage(db, load_vec=False) as storage:
            cosponsors = storage.cosponsors_for_bill("119-hr-1")
            assert len(cosponsors) == 3


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

    def test_short_title_and_subjects_indexed_when_present(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        _write_jsonl(
            storage_dir,
            [_envelope_for(_detail_payload("detail_119_hr_1.json"))],
        )
        # Tier-2: titles + subjects.
        titles_payload = _fixture("titles_119_hr_1.json")
        subjects_payload = _fixture("subjects_119_hr_1.json")
        (storage_dir / enrichment_jsonl_name("titles")).write_text(
            json.dumps(_envelope_for_section(titles_payload, bill_key=(119, "hr", 1))) + "\n",
            encoding="utf-8",
        )
        (storage_dir / enrichment_jsonl_name("subjects")).write_text(
            json.dumps(_envelope_for_section(subjects_payload, bill_key=(119, "hr", 1))) + "\n",
            encoding="utf-8",
        )
        load_bills(storage_dir=storage_dir, db_path=db)
        index_bills(db_path=db)
        with SqliteStorage(db, load_vec=False) as storage:
            # Short title is "Lower Energy Costs Act" — matches "lower".
            rows = storage.connection.execute(
                "SELECT bill_id FROM bills_fts WHERE bills_fts MATCH ?",
                ("lower",),
            ).fetchall()
            assert [r["bill_id"] for r in rows] == ["119-hr-1"]
            # Subjects column contains "Pipelines".
            rows = storage.connection.execute(
                "SELECT bill_id FROM bills_fts WHERE bills_fts MATCH ?",
                ("pipelines",),
            ).fetchall()
            assert [r["bill_id"] for r in rows] == ["119-hr-1"]

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
