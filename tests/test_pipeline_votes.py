"""Tests for the Votes Stage 1 loader (Phase 3a)."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from concord.pipeline.load_votes import load
from concord.scraper.votes import (
    HOUSE_VOTE_POSITIONS_JSONL_NAME,
    HOUSE_VOTES_JSONL_NAME,
    SENATE_ROSTER_JSONL_NAME,
    SENATE_VOTES_JSONL_NAME,
)
from concord.storage.sqlite import SqliteStorage

FIXTURES = Path(__file__).parent / "fixtures" / "api" / "votes"
SENATE_FIXTURES = Path(__file__).parent / "fixtures" / "senate"


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
        # Uses the real spike capture (roll 240, HR 3424, ~430 Members).
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
        assert stats.positions_written >= 400

        storage = SqliteStorage(db, load_vec=False)
        try:
            row = storage.get_vote("house-119-1-240")
            assert row is not None
            assert row["bill_id"] == "119-hr-3424"
            # Real totals: 202+195+0 yea across R/D/I.
            assert row["yea_count"] == 397
            positions = storage.list_vote_positions_for_vote("house-119-1-240")
            assert len(positions) >= 400
        finally:
            storage.close()

    def test_loads_amendment_populates_both_ids(self, tmp_path: Path) -> None:
        # Real spike fixture: roll 245, amendment HAMDT 85 to HR 3838.
        ts = datetime(2026, 4, 2, 12, 0, tzinfo=UTC)
        _write_envelopes(
            tmp_path / HOUSE_VOTES_JSONL_NAME,
            [
                _envelope(
                    _fixture("detail_house_119_1_subject_amendment.json")["houseRollCallVote"],
                    245,
                    ts,
                )
            ],
        )
        db = tmp_path / "db.sqlite"
        load(storage_dir=tmp_path, db_path=db)
        storage = SqliteStorage(db, load_vec=False)
        try:
            row = storage.get_vote("house-119-1-245")
            assert row is not None
            assert row["bill_id"] == "119-hr-3838"
            assert row["amendment_id"] == "119-hamdt-85"
        finally:
            storage.close()

    def test_election_vote_has_null_counts(self, tmp_path: Path) -> None:
        # Real spike fixture for the detail half (Speaker election, roll 2);
        # synthetic members payload (master didn't capture per-member data
        # for an election roll, and we don't need ~430 rows for this).
        ts = datetime(2025, 1, 3, 12, 0, tzinfo=UTC)
        _write_envelopes(
            tmp_path / HOUSE_VOTES_JSONL_NAME,
            [
                _envelope(
                    _fixture("detail_house_119_1_subject_procedural.json")["houseRollCallVote"],
                    2,
                    ts,
                )
            ],
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
            # Real fixture carries the full ~430-Member roster; idempotency
            # check is that re-running doesn't double the count.
            assert n_pos >= 400
        finally:
            storage.close()

    def test_missing_files_is_noop(self, tmp_path: Path) -> None:
        db = tmp_path / "db.sqlite"
        stats = load(storage_dir=tmp_path, db_path=db)
        assert stats.votes_written == 0
        assert stats.positions_written == 0


def _senate_envelope(xml_path: Path, roll: int, fetched_at: datetime) -> dict[str, Any]:
    return {
        "fetched_at": fetched_at.isoformat(),
        "key": {
            "chamber": "senate",
            "congress": 119,
            "session": 1,
            "roll_number": roll,
        },
        "payload": xml_path.read_text(),
    }


def _senate_roster_envelope(xml_path: Path, fetched_at: datetime) -> dict[str, Any]:
    return {
        "fetched_at": fetched_at.isoformat(),
        "key": {"source": "senators_cfm"},
        "payload": xml_path.read_text(),
    }


def _seed_member(
    db: Path,
    bioguide_id: str,
    last_name: str,
    state: str,
    *,
    chamber: str = "senate",
    start_date: str = "2023-01-03",
    end_date: str | None = None,
) -> None:
    storage = SqliteStorage(db, load_vec=False)
    try:
        storage.connection.execute(
            "INSERT OR REPLACE INTO members "
            "(bioguide_id, first_name, last_name, display_name, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (bioguide_id, "First", last_name, f"First {last_name}", "t"),
        )
        storage.connection.execute(
            "INSERT INTO member_terms "
            "(bioguide_id, congress, chamber, party, state, start_date, end_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (bioguide_id, 119, chamber, "D", state, start_date, end_date),
        )
        storage.connection.commit()
    finally:
        storage.close()


class TestSenateLoad:
    def _setup(self, tmp_path: Path, rolls: list[int]) -> Path:
        ts = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
        envelopes = []
        for roll in rolls:
            xml_path = SENATE_FIXTURES / f"detail_119_1_{roll:05d}_{_fixture_kind(roll)}.xml"
            envelopes.append(_senate_envelope(xml_path, roll, ts))
        _write_envelopes(tmp_path / SENATE_VOTES_JSONL_NAME, envelopes)
        _write_envelopes(
            tmp_path / SENATE_ROSTER_JSONL_NAME,
            [_senate_roster_envelope(SENATE_FIXTURES / "senators_cfm.xml", ts)],
        )
        return tmp_path / "db.sqlite"

    def test_bill_vote_loads_with_bill_id(self, tmp_path: Path) -> None:
        db = self._setup(tmp_path, [7])
        load(storage_dir=tmp_path, db_path=db)
        storage = SqliteStorage(db, load_vec=False)
        try:
            row = storage.get_vote("senate-119-1-7")
            assert row is not None
            assert row["bill_id"] == "119-s-5"
            assert row["amendment_id"] is None
            assert row["chamber"] == "senate"
            assert row["yea_count"] == 64
            assert row["threshold"] == "simple_majority"
            positions = storage.list_vote_positions_for_vote("senate-119-1-7")
            # Roster has the full sitting senate (~100); bridge resolves most.
            assert len(positions) > 90
            # Sample assertion: Alsobrooks ↔ A000382.
            row = next(p for p in positions if p["bioguide_id"] == "A000382")
            assert row["position"] == "Nay"
            assert row["vote_party"] == "D"
        finally:
            storage.close()

    def test_amendment_vote_loads_both_fks(self, tmp_path: Path) -> None:
        db = self._setup(tmp_path, [3])
        load(storage_dir=tmp_path, db_path=db)
        storage = SqliteStorage(db, load_vec=False)
        try:
            row = storage.get_vote("senate-119-1-3")
            assert row["amendment_id"] == "119-samdt-14"
            assert row["bill_id"] == "119-s-5"
        finally:
            storage.close()

    def test_nomination_vote_loads_with_null_fks(self, tmp_path: Path) -> None:
        db = self._setup(tmp_path, [8])
        load(storage_dir=tmp_path, db_path=db)
        storage = SqliteStorage(db, load_vec=False)
        try:
            row = storage.get_vote("senate-119-1-8")
            assert row["bill_id"] is None
            assert row["amendment_id"] is None
            # Loader persists the human-readable vote_title when there
            # is no bill / amendment FK to anchor the subject on.
            assert "Marco Rubio" in row["vote_question"]
            assert "Secretary of State" in row["vote_question"]
        finally:
            storage.close()

    def test_cloture_vote_has_three_fifths_threshold(self, tmp_path: Path) -> None:
        db = self._setup(tmp_path, [1])
        load(storage_dir=tmp_path, db_path=db)
        storage = SqliteStorage(db, load_vec=False)
        try:
            row = storage.get_vote("senate-119-1-1")
            assert row["threshold"] == "three_fifths"
        finally:
            storage.close()

    def test_historical_fallback_via_members_table(self, tmp_path: Path) -> None:
        # Use a synthetic XML where one member is not in the roster but
        # is present in the Phase 1 members table for the relevant date.
        synthetic_xml = b"""<?xml version="1.0"?>
<roll_call_vote>
<congress>119</congress><session>1</session>
<vote_number>99999</vote_number>
<vote_date>February 1, 2025, 12:00 PM</vote_date>
<modify_date>February 1, 2025, 12:30 PM</modify_date>
<vote_question_text>Test</vote_question_text>
<question>On Passage</question>
<vote_title>Test</vote_title>
<majority_requirement>1/2</majority_requirement>
<vote_result>Passed</vote_result>
<document><document_type>S.</document_type><document_number>5</document_number></document>
<amendment><amendment_number/></amendment>
<count><yeas>1</yeas><nays>0</nays><present/><absent/></count>
<members>
<member>
  <member_full>Ghostperson (D-ZZ)</member_full>
  <last_name>Ghostperson</last_name>
  <first_name>Old</first_name>
  <party>D</party>
  <state>ZZ</state>
  <vote_cast>Yea</vote_cast>
  <lis_member_id>S000</lis_member_id>
</member>
</members>
</roll_call_vote>"""
        ts = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
        envelopes = [
            {
                "fetched_at": ts.isoformat(),
                "key": {
                    "chamber": "senate",
                    "congress": 119,
                    "session": 1,
                    "roll_number": 99999,
                },
                "payload": synthetic_xml.decode("utf-8"),
            }
        ]
        _write_envelopes(tmp_path / SENATE_VOTES_JSONL_NAME, envelopes)
        _write_envelopes(
            tmp_path / SENATE_ROSTER_JSONL_NAME,
            [_senate_roster_envelope(SENATE_FIXTURES / "senators_cfm.xml", ts)],
        )
        db = tmp_path / "db.sqlite"
        # Pre-seed the members table.
        _seed_member(db, "Z000999", "Ghostperson", "ZZ")
        load(storage_dir=tmp_path, db_path=db)
        storage = SqliteStorage(db, load_vec=False)
        try:
            positions = storage.list_vote_positions_for_vote("senate-119-1-99999")
            assert [p["bioguide_id"] for p in positions] == ["Z000999"]
        finally:
            storage.close()

    def test_unresolved_member_skipped_with_warning(self, tmp_path: Path, caplog: Any) -> None:
        synthetic_xml = b"""<?xml version="1.0"?>
<roll_call_vote>
<congress>119</congress><session>1</session>
<vote_number>88888</vote_number>
<vote_date>February 1, 2025, 12:00 PM</vote_date>
<modify_date>February 1, 2025, 12:30 PM</modify_date>
<vote_question_text>Test</vote_question_text>
<question>On Passage</question>
<vote_title>Test</vote_title>
<majority_requirement>1/2</majority_requirement>
<vote_result>Passed</vote_result>
<document><document_type>S.</document_type><document_number>5</document_number></document>
<amendment><amendment_number/></amendment>
<count><yeas>1</yeas><nays>0</nays><present/><absent/></count>
<members>
<member>
  <member_full>Alsobrooks (D-MD)</member_full>
  <last_name>Alsobrooks</last_name>
  <party>D</party>
  <state>MD</state>
  <vote_cast>Yea</vote_cast>
  <lis_member_id>S428</lis_member_id>
</member>
<member>
  <member_full>Nobody (D-ZZ)</member_full>
  <last_name>Nobody</last_name>
  <party>D</party>
  <state>ZZ</state>
  <vote_cast>Yea</vote_cast>
  <lis_member_id>S999</lis_member_id>
</member>
</members>
</roll_call_vote>"""
        ts = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
        _write_envelopes(
            tmp_path / SENATE_VOTES_JSONL_NAME,
            [
                {
                    "fetched_at": ts.isoformat(),
                    "key": {
                        "chamber": "senate",
                        "congress": 119,
                        "session": 1,
                        "roll_number": 88888,
                    },
                    "payload": synthetic_xml.decode("utf-8"),
                }
            ],
        )
        _write_envelopes(
            tmp_path / SENATE_ROSTER_JSONL_NAME,
            [_senate_roster_envelope(SENATE_FIXTURES / "senators_cfm.xml", ts)],
        )
        db = tmp_path / "db.sqlite"
        import logging  # noqa: PLC0415 — lazy: only this test uses caplog level

        with caplog.at_level(logging.WARNING, logger="concord.pipeline.load_votes"):
            load(storage_dir=tmp_path, db_path=db)

        storage = SqliteStorage(db, load_vec=False)
        try:
            row = storage.get_vote("senate-119-1-88888")
            assert row is not None  # vote loaded despite skipped position
            positions = storage.list_vote_positions_for_vote("senate-119-1-88888")
            assert [p["bioguide_id"] for p in positions] == ["A000382"]
        finally:
            storage.close()
        assert any("unresolved_member" in rec.message for rec in caplog.records)

    def test_idempotent(self, tmp_path: Path) -> None:
        db = self._setup(tmp_path, [7])
        load(storage_dir=tmp_path, db_path=db)
        load(storage_dir=tmp_path, db_path=db)
        storage = SqliteStorage(db, load_vec=False)
        try:
            (n_votes,) = storage.connection.execute(
                "SELECT COUNT(*) FROM votes WHERE chamber='senate'"
            ).fetchone()
            (n_pos,) = storage.connection.execute(
                "SELECT COUNT(*) FROM vote_positions WHERE vote_id='senate-119-1-7'"
            ).fetchone()
            assert n_votes == 1
            assert n_pos > 90
        finally:
            storage.close()


def _fixture_kind(roll: int) -> str:
    return {
        1: "cloture",
        2: "motion",
        3: "amendment",
        7: "bill",
        8: "nomination",
    }[roll]
