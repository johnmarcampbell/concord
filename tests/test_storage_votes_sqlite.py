"""Tests for the Votes SQLite schema + storage methods (Phase 3a)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from concord.models import Vote, VotePosition
from concord.storage.sqlite import SqliteStorage


def _make_vote(**overrides: object) -> Vote:
    base = {
        "vote_id": "house-119-1-240",
        "chamber": "house",
        "congress": 119,
        "session": 1,
        "roll_number": 240,
        "vote_kind": "standard",
        "start_date": "2026-04-01T18:32:00Z",
        "vote_question": "On Passage of the Bill",
        "vote_type": "Yea-and-Nay",
        "threshold": "simple_majority",
        "result": "Passed",
        "yea_count": 222,
        "nay_count": 203,
        "present_count": 1,
        "not_voting_count": 8,
        "bill_id": "119-hr-3424",
        "amendment_id": None,
        "is_party_unity": False,
        "update_date": "2026-04-01T18:35:00Z",
    }
    base.update(overrides)
    return Vote(**base)  # type: ignore[arg-type]


class TestVotesSchema:
    def test_schema_applies_cleanly(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "db.sqlite", load_vec=False)
        try:
            tables = {
                row["name"]
                for row in storage.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "votes" in tables
            assert "vote_positions" in tables
            assert "member_party_unity" in tables
        finally:
            storage.close()

    def test_chamber_check_constraint_via_raw_sql(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "db.sqlite", load_vec=False)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                storage.connection.execute(
                    "INSERT INTO votes (vote_id, chamber, congress, session, roll_number, "
                    "vote_kind, start_date, vote_question, vote_type, result, update_date, "
                    "fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("x", "committee", 119, 1, 1, "standard", "d", "q", "t", "r", "u", "f"),
                )
        finally:
            storage.close()

    def test_session_check_constraint_via_raw_sql(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "db.sqlite", load_vec=False)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                storage.connection.execute(
                    "INSERT INTO votes (vote_id, chamber, congress, session, roll_number, "
                    "vote_kind, start_date, vote_question, vote_type, result, update_date, "
                    "fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("x", "house", 119, 3, 1, "standard", "d", "q", "t", "r", "u", "f"),
                )
        finally:
            storage.close()

    def test_vote_kind_check_constraint_via_raw_sql(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "db.sqlite", load_vec=False)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                storage.connection.execute(
                    "INSERT INTO votes (vote_id, chamber, congress, session, roll_number, "
                    "vote_kind, start_date, vote_question, vote_type, result, update_date, "
                    "fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("x", "house", 119, 1, 1, "unknown", "d", "q", "t", "r", "u", "f"),
                )
        finally:
            storage.close()


class TestUpsertVote:
    def test_idempotent(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "db.sqlite", load_vec=False)
        try:
            v = _make_vote()
            storage.upsert_vote(v, fetched_at="2026-04-01T00:00:00")
            storage.upsert_vote(v, fetched_at="2026-04-02T00:00:00")
            (n,) = storage.connection.execute("SELECT COUNT(*) FROM votes").fetchone()
            assert n == 1
            row = storage.get_vote("house-119-1-240")
            assert row is not None
            assert row["fetched_at"] == "2026-04-02T00:00:00"
        finally:
            storage.close()


def _pos(bg: str, pos: str, party: str | None = None, state: str | None = None) -> VotePosition:
    return VotePosition(bioguide_id=bg, position=pos, vote_party=party, vote_state=state)


class TestVotePositions:
    def test_bulk_replace(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "db.sqlite", load_vec=False)
        try:
            storage.upsert_vote(_make_vote(), fetched_at="t")
            positions = [
                _pos("A000001", "Yea", "R", "LA"),
                _pos("B000002", "Nay", "D", "CA"),
            ]
            n = storage.upsert_vote_positions("house-119-1-240", positions)
            assert n == 2
            # Replace with smaller list — DELETE-then-INSERT semantics.
            n = storage.upsert_vote_positions(
                "house-119-1-240",
                [_pos("C000003", "Present", "I", "VT")],
            )
            assert n == 1
            rows = storage.list_vote_positions_for_vote("house-119-1-240")
            assert len(rows) == 1
            assert rows[0]["bioguide_id"] == "C000003"
        finally:
            storage.close()


def _insert_party_unity_row(
    storage: SqliteStorage,
    party: str,
    *,
    chamber: str = "house",
    bioguide_id: str = "X000001",
    congress: int = 119,
) -> None:
    storage.connection.execute(
        "INSERT INTO member_party_unity "
        "(bioguide_id, congress, chamber, party, "
        "party_unity_votes_cast, party_line_votes) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (bioguide_id, congress, chamber, party, 10, 9),
    )
    storage.connection.commit()


class TestMemberPartyUnityCheckConstraint:
    def test_rejects_independent(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "db.sqlite", load_vec=False)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_party_unity_row(storage, "I")
        finally:
            storage.close()

    def test_rejects_invalid_chamber(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "db.sqlite", load_vec=False)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_party_unity_row(storage, "D", chamber="commons")
        finally:
            storage.close()

    def test_same_bioguide_and_congress_across_chambers_allowed(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "db.sqlite", load_vec=False)
        try:
            _insert_party_unity_row(storage, "D", chamber="house")
            _insert_party_unity_row(storage, "D", chamber="senate")
            rows = storage.get_party_unity_for_member("X000001")
            assert [r["chamber"] for r in rows] == ["house", "senate"]
        finally:
            storage.close()

    def test_get_party_unity_orders_by_congress_then_chamber(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "db.sqlite", load_vec=False)
        try:
            _insert_party_unity_row(storage, "D", chamber="senate", congress=118)
            _insert_party_unity_row(storage, "D", chamber="house", congress=119)
            _insert_party_unity_row(storage, "D", chamber="senate", congress=119)
            rows = storage.get_party_unity_for_member("X000001")
            assert [(r["congress"], r["chamber"]) for r in rows] == [
                (119, "house"),
                (119, "senate"),
                (118, "senate"),
            ]
        finally:
            storage.close()
