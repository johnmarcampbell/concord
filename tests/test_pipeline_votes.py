"""Tests for the Votes Stage 1 loader (Phase 3a)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from concord.pipeline.load_votes import load
from concord.scraper.votes import (
    HOUSE_VOTE_POSITIONS_JSONL_NAME,
    HOUSE_VOTES_JSONL_NAME,
)
from concord.storage.sqlite import SqliteStorage

FIXTURES = Path(__file__).parent / "fixtures" / "api" / "votes"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def _write_envelopes(path: Path, envelopes: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for env in envelopes:
            fh.write(json.dumps(env) + "\n")


def _envelope(payload: dict[str, Any], roll: int, fetched_at: datetime) -> dict[str, Any]:
    return {
        "fetched_at": fetched_at.isoformat(),
        "key": {
            "chamber": "house",
            "congress": 119,
            "session": 1,
            "roll_number": roll,
        },
        "payload": payload,
    }


class TestLoad:
    def test_loads_bill_vote(self, tmp_path: Path) -> None:
        ts = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
        _write_envelopes(
            tmp_path / HOUSE_VOTES_JSONL_NAME,
            [_envelope(_fixture("detail_house_119_1_240.json")["houseRollCallVote"], 240, ts)],
        )
        _write_envelopes(
            tmp_path / HOUSE_VOTE_POSITIONS_JSONL_NAME,
            [
                _envelope(
                    _fixture("members_house_119_1_240.json")["houseRollCallVoteMemberVotes"],
                    240,
                    ts,
                )
            ],
        )

        db = tmp_path / "db.sqlite"
        stats = load(storage_dir=tmp_path, db_path=db)
        assert stats.votes_written == 1
        assert stats.positions_written == 4

        storage = SqliteStorage(db, load_vec=False)
        try:
            row = storage.get_vote("house-119-1-240")
            assert row is not None
            assert row["bill_id"] == "119-hr-3424"
            assert row["yea_count"] == 222
            positions = storage.list_vote_positions_for_vote("house-119-1-240")
            assert len(positions) == 4
        finally:
            storage.close()

    def test_loads_amendment_populates_both_ids(self, tmp_path: Path) -> None:
        ts = datetime(2026, 4, 2, 12, 0, tzinfo=UTC)
        _write_envelopes(
            tmp_path / HOUSE_VOTES_JSONL_NAME,
            [
                _envelope(
                    _fixture("detail_house_119_1_241_amendment.json")["houseRollCallVote"],
                    241,
                    ts,
                )
            ],
        )
        db = tmp_path / "db.sqlite"
        load(storage_dir=tmp_path, db_path=db)
        storage = SqliteStorage(db, load_vec=False)
        try:
            row = storage.get_vote("house-119-1-241")
            assert row is not None
            assert row["bill_id"] == "119-hr-3424"
            assert row["amendment_id"] == "119-hamdt-85"
        finally:
            storage.close()

    def test_election_vote_has_null_counts(self, tmp_path: Path) -> None:
        ts = datetime(2025, 1, 3, 12, 0, tzinfo=UTC)
        _write_envelopes(
            tmp_path / HOUSE_VOTES_JSONL_NAME,
            [_envelope(_fixture("detail_house_119_1_2_election.json")["houseRollCallVote"], 2, ts)],
        )
        _write_envelopes(
            tmp_path / HOUSE_VOTE_POSITIONS_JSONL_NAME,
            [
                _envelope(
                    _fixture("members_house_119_1_2_election.json")["houseRollCallVoteMemberVotes"],
                    2,
                    ts,
                )
            ],
        )
        db = tmp_path / "db.sqlite"
        load(storage_dir=tmp_path, db_path=db)
        storage = SqliteStorage(db, load_vec=False)
        try:
            row = storage.get_vote("house-119-1-2")
            assert row is not None
            assert row["vote_kind"] == "election"
            assert row["yea_count"] is None
            positions = storage.list_vote_positions_for_vote("house-119-1-2")
            assert {p["position"] for p in positions} == {"Johnson", "Jeffries"}
        finally:
            storage.close()

    def test_latest_snapshot_wins(self, tmp_path: Path) -> None:
        early = datetime(2026, 4, 1, tzinfo=UTC)
        late = datetime(2026, 4, 5, tzinfo=UTC)
        detail = _fixture("detail_house_119_1_240.json")["houseRollCallVote"]
        mutated = {**detail, "result": "Failed"}
        _write_envelopes(
            tmp_path / HOUSE_VOTES_JSONL_NAME,
            [
                _envelope(detail, 240, early),
                _envelope(mutated, 240, late),
            ],
        )
        db = tmp_path / "db.sqlite"
        load(storage_dir=tmp_path, db_path=db)
        storage = SqliteStorage(db, load_vec=False)
        try:
            row = storage.get_vote("house-119-1-240")
            assert row is not None
            assert row["result"] == "Failed"
        finally:
            storage.close()

    def test_rerun_is_idempotent(self, tmp_path: Path) -> None:
        ts = datetime(2026, 4, 1, tzinfo=UTC)
        _write_envelopes(
            tmp_path / HOUSE_VOTES_JSONL_NAME,
            [_envelope(_fixture("detail_house_119_1_240.json")["houseRollCallVote"], 240, ts)],
        )
        _write_envelopes(
            tmp_path / HOUSE_VOTE_POSITIONS_JSONL_NAME,
            [
                _envelope(
                    _fixture("members_house_119_1_240.json")["houseRollCallVoteMemberVotes"],
                    240,
                    ts,
                )
            ],
        )
        db = tmp_path / "db.sqlite"
        load(storage_dir=tmp_path, db_path=db)
        load(storage_dir=tmp_path, db_path=db)
        storage = SqliteStorage(db, load_vec=False)
        try:
            (n_votes,) = storage.connection.execute("SELECT COUNT(*) FROM votes").fetchone()
            (n_pos,) = storage.connection.execute("SELECT COUNT(*) FROM vote_positions").fetchone()
            assert n_votes == 1
            assert n_pos == 4
        finally:
            storage.close()

    def test_missing_files_is_noop(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite"
        stats = load(storage_dir=tmp_path, db_path=db)
        assert stats.votes_written == 0
        assert stats.positions_written == 0
