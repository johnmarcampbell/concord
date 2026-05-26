"""Tests for the Votes Stage 2 indexer — party-unity computation."""

from __future__ import annotations

from pathlib import Path

from concord.models import Vote, VotePosition
from concord.pipeline.index_votes import index
from concord.storage.sqlite import SqliteStorage


def _seed_vote(storage: SqliteStorage, vote_id: str, **overrides: object) -> None:
    base = {
        "vote_id": vote_id,
        "chamber": "house",
        "congress": 119,
        "session": 1,
        "roll_number": int(vote_id.rsplit("-", 1)[-1]),
        "vote_kind": "standard",
        "start_date": "2026-04-01T18:00:00Z",
        "vote_question": "Q",
        "vote_type": "Yea-and-Nay",
        "threshold": "simple_majority",
        "result": "Passed",
        "yea_count": 0,
        "nay_count": 0,
        "present_count": 0,
        "not_voting_count": 0,
        "bill_id": None,
        "amendment_id": None,
        "is_party_unity": False,
        "update_date": "2026-04-01T18:00:00Z",
    }
    base.update(overrides)
    storage.upsert_vote(Vote(**base), fetched_at="t")  # type: ignore[arg-type]


def _seed_positions(
    storage: SqliteStorage,
    vote_id: str,
    rows: list[tuple[str, str, str]],
) -> None:
    """Each row is (bioguide_id, position, vote_party)."""
    storage.upsert_vote_positions(
        vote_id,
        [VotePosition(bioguide_id=b, position=p, vote_party=pt) for b, p, pt in rows],
    )


class TestPartyUnityFlag:
    def test_flags_split_vote(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "db.sqlite", load_vec=False)
        try:
            _seed_vote(storage, "house-119-1-1")
            _seed_positions(
                storage,
                "house-119-1-1",
                [
                    ("R001", "Yea", "R"),
                    ("R002", "Yea", "R"),
                    ("D001", "Nay", "D"),
                    ("D002", "Nay", "D"),
                ],
            )
        finally:
            storage.close()

        index(db_path=tmp_path / "db.sqlite")

        storage = SqliteStorage(tmp_path / "db.sqlite", load_vec=False)
        try:
            row = storage.get_vote("house-119-1-1")
            assert row is not None
            assert row["is_party_unity"] == 1
        finally:
            storage.close()

    def test_does_not_flag_unanimous_vote(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "db.sqlite", load_vec=False)
        try:
            _seed_vote(storage, "house-119-1-2")
            _seed_positions(
                storage,
                "house-119-1-2",
                [
                    ("R001", "Yea", "R"),
                    ("R002", "Yea", "R"),
                    ("D001", "Yea", "D"),
                    ("D002", "Yea", "D"),
                ],
            )
        finally:
            storage.close()

        index(db_path=tmp_path / "db.sqlite")

        storage = SqliteStorage(tmp_path / "db.sqlite", load_vec=False)
        try:
            row = storage.get_vote("house-119-1-2")
            assert row["is_party_unity"] == 0
        finally:
            storage.close()

    def test_election_votes_never_flagged(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "db.sqlite", load_vec=False)
        try:
            _seed_vote(storage, "house-119-1-3", vote_kind="election", result="Johnson")
            _seed_positions(
                storage,
                "house-119-1-3",
                [
                    ("R001", "Johnson", "R"),
                    ("D001", "Jeffries", "D"),
                ],
            )
        finally:
            storage.close()
        index(db_path=tmp_path / "db.sqlite")
        storage = SqliteStorage(tmp_path / "db.sqlite", load_vec=False)
        try:
            row = storage.get_vote("house-119-1-3")
            assert row["is_party_unity"] == 0
        finally:
            storage.close()


class TestMemberPartyUnity:
    def test_numerator_and_denominator(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "db.sqlite", load_vec=False)
        try:
            _seed_vote(storage, "house-119-1-1")
            _seed_vote(storage, "house-119-1-2")
            # Two party-unity votes. R majority = Yea; D majority = Nay on both.
            for vid in ("house-119-1-1", "house-119-1-2"):
                _seed_positions(
                    storage,
                    vid,
                    [
                        ("R001", "Yea", "R"),  # numerator hits
                        ("R002", "Yea", "R"),
                        ("R003", "Nay", "R"),  # voted against R majority → numerator miss
                        ("D001", "Nay", "D"),  # numerator hits
                        ("D002", "Nay", "D"),
                        ("D003", "Yea", "D"),  # numerator miss
                    ],
                )
        finally:
            storage.close()

        index(db_path=tmp_path / "db.sqlite")

        storage = SqliteStorage(tmp_path / "db.sqlite", load_vec=False)
        try:
            rows = {r["congress"]: r for r in storage.get_party_unity_for_member("R001")}
            assert rows[119]["party"] == "R"
            assert rows[119]["party_unity_votes_cast"] == 2
            assert rows[119]["party_line_votes"] == 2
            rows = {r["congress"]: r for r in storage.get_party_unity_for_member("R003")}
            assert rows[119]["party_line_votes"] == 0
            assert rows[119]["party_unity_votes_cast"] == 2
            rows = {r["congress"]: r for r in storage.get_party_unity_for_member("D001")}
            assert rows[119]["party"] == "D"
            assert rows[119]["party_line_votes"] == 2
        finally:
            storage.close()

    def test_independent_member_not_scored(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "db.sqlite", load_vec=False)
        try:
            _seed_vote(storage, "house-119-1-1")
            _seed_positions(
                storage,
                "house-119-1-1",
                [
                    ("R001", "Yea", "R"),
                    ("R002", "Yea", "R"),
                    ("D001", "Nay", "D"),
                    ("D002", "Nay", "D"),
                    ("I001", "Yea", "I"),
                ],
            )
        finally:
            storage.close()
        index(db_path=tmp_path / "db.sqlite")
        storage = SqliteStorage(tmp_path / "db.sqlite", load_vec=False)
        try:
            rows = storage.get_party_unity_for_member("I001")
            assert rows == []
        finally:
            storage.close()

    def test_idempotent(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "db.sqlite", load_vec=False)
        try:
            _seed_vote(storage, "house-119-1-1")
            _seed_positions(
                storage,
                "house-119-1-1",
                [
                    ("R001", "Yea", "R"),
                    ("R002", "Yea", "R"),
                    ("D001", "Nay", "D"),
                    ("D002", "Nay", "D"),
                ],
            )
        finally:
            storage.close()
        index(db_path=tmp_path / "db.sqlite")
        index(db_path=tmp_path / "db.sqlite")
        storage = SqliteStorage(tmp_path / "db.sqlite", load_vec=False)
        try:
            (n,) = storage.connection.execute("SELECT COUNT(*) FROM member_party_unity").fetchone()
            assert n == 4
        finally:
            storage.close()
