"""Tests for the Members Stage 1 (load) and Stage 2 (index) pipelines.

Drives the JSONL → SQLite projection against the captured API fixtures
and asserts (a) snapshot dedup keeps the latest ``fetched_at`` per
Bioguide ID, (b) Term rows are replaced on each load (not appended),
(c) the FTS5 index round-trips a name query back to the right
``bioguide_id``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from concord.pipeline.index_members import index as index_members
from concord.pipeline.load_members import load as load_members
from concord.storage.sqlite import SqliteStorage

from ._snapshots import wrap_snapshot

FIXED_FETCHED_AT = datetime(2026, 5, 25, 14, 2, 11, tzinfo=UTC)


def _fixture(name: str) -> dict[str, Any]:
    here = Path(__file__).parent / "fixtures" / "api" / "members"
    return json.loads((here / name).read_text())


def _write_jsonl(path: Path, envelopes: list[dict[str, Any]]) -> None:
    path.write_text(
        "\n".join(json.dumps(e) for e in envelopes) + "\n",
        encoding="utf-8",
    )


class TestLoadMembers:
    def test_projects_member_and_terms(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "members.jsonl"
        db = tmp_path / "test.db"
        payload = _fixture("current_house.json")["members"][0]
        _write_jsonl(
            jsonl,
            [wrap_snapshot(payload, fetched_at=FIXED_FETCHED_AT, key={"bioguide_id": "O000172"})],
        )

        stats = load_members(jsonl_path=jsonl, db_path=db)
        assert stats.members_written == 1
        assert stats.terms_written == 3

        with SqliteStorage(db, load_vec=False) as storage:
            row = storage.get_member("O000172")
            assert row is not None
            assert row["display_name"] == "Alexandria Ocasio-Cortez"
            terms = storage.terms_for_member("O000172")
            assert {t["congress"] for t in terms} == {117, 118, 119}

    def test_latest_snapshot_wins(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "members.jsonl"
        db = tmp_path / "test.db"
        payload = _fixture("current_house.json")["members"][0]
        older = wrap_snapshot(
            {**payload, "directOrderName": "OLD NAME"},
            fetched_at=FIXED_FETCHED_AT - timedelta(days=30),
            key={"bioguide_id": "O000172"},
        )
        newer = wrap_snapshot(
            payload,
            fetched_at=FIXED_FETCHED_AT,
            key={"bioguide_id": "O000172"},
        )
        _write_jsonl(jsonl, [older, newer])

        load_members(jsonl_path=jsonl, db_path=db)

        with SqliteStorage(db, load_vec=False) as storage:
            row = storage.get_member("O000172")
            assert row is not None
            assert row["display_name"] == "Alexandria Ocasio-Cortez"

    def test_terms_are_not_duplicated_on_reload(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "members.jsonl"
        db = tmp_path / "test.db"
        payload = _fixture("current_house.json")["members"][0]
        envelope = wrap_snapshot(
            payload, fetched_at=FIXED_FETCHED_AT, key={"bioguide_id": "O000172"}
        )
        _write_jsonl(jsonl, [envelope, envelope, envelope])

        stats = load_members(jsonl_path=jsonl, db_path=db)
        assert stats.snapshots_read == 3

        with SqliteStorage(db, load_vec=False) as storage:
            terms = storage.terms_for_member("O000172")
            # Three identical snapshots collapse to one Member with three
            # distinct Terms — not nine.
            assert len(terms) == 3

    def test_malformed_lines_counted_not_fatal(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "members.jsonl"
        db = tmp_path / "test.db"
        payload = _fixture("current_senate.json")["members"][0]
        good = json.dumps(
            wrap_snapshot(payload, fetched_at=FIXED_FETCHED_AT, key={"bioguide_id": "S000033"})
        )
        jsonl.write_text(
            "\n".join(
                [
                    "{not valid json",
                    '{"missing": "envelope-keys"}',
                    good,
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        stats = load_members(jsonl_path=jsonl, db_path=db)
        assert stats.malformed == 2
        assert stats.members_written == 1

    def test_multiple_members_loaded(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "members.jsonl"
        db = tmp_path / "test.db"
        house = _fixture("current_house.json")["members"][0]
        senate = _fixture("current_senate.json")["members"][0]
        _write_jsonl(
            jsonl,
            [
                wrap_snapshot(house, fetched_at=FIXED_FETCHED_AT, key={"bioguide_id": "O000172"}),
                wrap_snapshot(senate, fetched_at=FIXED_FETCHED_AT, key={"bioguide_id": "S000033"}),
            ],
        )

        stats = load_members(jsonl_path=jsonl, db_path=db)
        assert stats.members_written == 2

        with SqliteStorage(db, load_vec=False) as storage:
            assert storage.get_member("O000172") is not None
            assert storage.get_member("S000033") is not None


class TestIndexMembers:
    def test_populates_fts(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "members.jsonl"
        db = tmp_path / "test.db"
        house = _fixture("current_house.json")["members"][0]
        senate = _fixture("current_senate.json")["members"][0]
        _write_jsonl(
            jsonl,
            [
                wrap_snapshot(house, fetched_at=FIXED_FETCHED_AT, key={"bioguide_id": "O000172"}),
                wrap_snapshot(senate, fetched_at=FIXED_FETCHED_AT, key={"bioguide_id": "S000033"}),
            ],
        )
        load_members(jsonl_path=jsonl, db_path=db)

        stats = index_members(db_path=db)
        assert stats.indexed_members == 2

        with SqliteStorage(db, load_vec=False) as storage:
            rows = storage.connection.execute(
                "SELECT bioguide_id FROM members_fts WHERE members_fts MATCH ?",
                ("Sanders",),
            ).fetchall()
            assert [r["bioguide_id"] for r in rows] == ["S000033"]

    def test_idempotent(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "members.jsonl"
        db = tmp_path / "test.db"
        payload = _fixture("current_house.json")["members"][0]
        _write_jsonl(
            jsonl,
            [wrap_snapshot(payload, fetched_at=FIXED_FETCHED_AT, key={"bioguide_id": "O000172"})],
        )
        load_members(jsonl_path=jsonl, db_path=db)

        first = index_members(db_path=db)
        second = index_members(db_path=db)
        assert first.indexed_members == 1
        assert second.indexed_members == 1

        with SqliteStorage(db, load_vec=False) as storage:
            (count,) = storage.connection.execute("SELECT COUNT(*) FROM members_fts").fetchone()
            assert count == 1
