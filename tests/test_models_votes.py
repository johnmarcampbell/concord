"""Tests for the Vote pydantic models and parse helpers."""

import json
from pathlib import Path
from typing import Any

import pytest

from concord.models import (
    Vote,
    VotePosition,
    amendment_id_from_components,
    parse_vote,
    parse_vote_positions,
    parse_vote_threshold,
    vote_id_from_components,
)

FIXTURES = Path(__file__).parent / "fixtures" / "api" / "votes"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def _detail(name: str) -> dict[str, Any]:
    """Pull the inner houseRollCallVote object from a detail fixture."""
    return _fixture(name)["houseRollCallVote"]


class TestVoteIdFromComponents:
    def test_shape(self) -> None:
        assert vote_id_from_components("house", 119, 1, 240) == "house-119-1-240"

    def test_lowercases_chamber(self) -> None:
        assert vote_id_from_components("HOUSE", 119, 1, 240) == "house-119-1-240"


class TestAmendmentIdFromComponents:
    def test_shape(self) -> None:
        assert amendment_id_from_components(119, "HAMDT", 85) == "119-hamdt-85"


class TestParseVoteThreshold:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Yea-and-Nay", "simple_majority"),
            ("YEA-AND-NAY", "simple_majority"),
            ("Recorded Vote", "simple_majority"),
            ("Quorum", "simple_majority"),
            ("2/3 Yea-And-Nay", "two_thirds"),
            ("3/5 Recorded Vote", "three_fifths"),
            ("Something unfamiliar", None),
            ("", None),
            (None, None),
        ],
    )
    def test_known_and_unknown(self, raw: str | None, expected: str | None) -> None:
        assert parse_vote_threshold(raw) == expected


class TestParseVote:
    def test_bill_vote_real_capture(self) -> None:
        # Real spike capture: HR 3424, "On Motion to Suspend the Rules
        # and Pass", 2/3 Yea-And-Nay → two_thirds threshold.
        v = parse_vote(_detail("detail_house_119_1_240.json"))
        assert isinstance(v, Vote)
        assert v.vote_id == "house-119-1-240"
        assert v.chamber == "house"
        assert v.vote_kind == "standard"
        assert v.bill_id == "119-hr-3424"
        assert v.amendment_id is None
        # Real totals across R/D/I: 202+195+0 yea, 0+1+0 nay.
        assert v.yea_count == 397
        assert v.nay_count == 1
        assert v.threshold == "two_thirds"

    def test_bill_vote_synthetic_party_split(self) -> None:
        # Synthetic fixture contrived to be party-unity-positive
        # (R majority Yea opposes D majority Nay).
        v = parse_vote(_detail("synthetic_bill_vote_party_unity_detail.json"))
        assert v.vote_id == "house-119-1-240"
        assert v.yea_count == 222
        assert v.nay_count == 203
        assert v.threshold == "simple_majority"

    def test_amendment_vote_populates_both_ids(self) -> None:
        # Real spike capture: roll 245, amendment HAMDT 85 to HR 3838.
        v = parse_vote(_detail("detail_house_119_1_subject_amendment.json"))
        assert v.bill_id == "119-hr-3838"
        assert v.amendment_id == "119-hamdt-85"
        assert v.vote_kind == "standard"

    def test_election_vote(self) -> None:
        # Real spike capture: Speaker election, roll 2, candidate-bucketed
        # party totals.
        v = parse_vote(_detail("detail_house_119_1_subject_procedural.json"))
        assert v.vote_kind == "election"
        # Counts NULL because party totals are bucketed by candidate.
        assert v.yea_count is None
        assert v.nay_count is None
        assert "Johnson" in v.result

    def test_procedural_two_thirds(self) -> None:
        # Synthetic procedural fixture: "On Approving the Journal", 2/3.
        v = parse_vote(_detail("detail_house_119_1_300_procedural.json"))
        assert v.threshold == "two_thirds"
        assert v.bill_id is None
        assert v.amendment_id is None


class TestParseVotePositions:
    def test_extracts_full_real_roster(self) -> None:
        # Real spike fixture: ~430 House Members.
        payload = _fixture("members_house_119_1_240.json")["houseRollCallVoteMemberVotes"]
        positions = parse_vote_positions(payload)
        assert len(positions) >= 400
        by_bg = {p.bioguide_id: p for p in positions}
        # Sample Member from the captured payload.
        aderholt = by_bg["A000055"]
        assert aderholt.position == "Yea"
        assert aderholt.vote_party == "R"
        assert aderholt.vote_state == "AL"

    def test_election_positions_carry_surnames(self) -> None:
        payload = _fixture("members_house_119_1_2_election.json")["houseRollCallVoteMemberVotes"]
        positions = parse_vote_positions(payload)
        by_bg = {p.bioguide_id: p for p in positions}
        assert by_bg["S001176"].position == "Johnson"
        assert by_bg["P000197"].position == "Jeffries"


class TestVoteModel:
    def test_lowercases_chamber(self) -> None:
        v = Vote(
            vote_id="house-119-1-1",
            chamber="House",  # type: ignore[arg-type]
            congress=119,
            session=1,
            roll_number=1,
            start_date="2026-01-01T00:00:00Z",
            vote_question="Q",
            vote_type="Yea-and-Nay",
            result="Passed",
            update_date="2026-01-01",
        )
        assert v.chamber == "house"

    def test_vote_position_minimal(self) -> None:
        p = VotePosition(bioguide_id="X000001", position="Yea")
        assert p.vote_party is None
        assert p.vote_state is None
