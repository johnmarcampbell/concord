"""Tests for the Vote pydantic models and parse helpers."""

from __future__ import annotations

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
    def test_bill_vote(self) -> None:
        v = parse_vote(_detail("detail_house_119_1_240.json"))
        assert isinstance(v, Vote)
        assert v.vote_id == "house-119-1-240"
        assert v.chamber == "house"
        assert v.vote_kind == "standard"
        assert v.bill_id == "119-hr-3424"
        assert v.amendment_id is None
        assert v.yea_count == 222
        assert v.nay_count == 203
        assert v.threshold == "simple_majority"

    def test_amendment_vote_populates_both_ids(self) -> None:
        v = parse_vote(_detail("detail_house_119_1_241_amendment.json"))
        assert v.bill_id == "119-hr-3424"
        assert v.amendment_id == "119-hamdt-85"
        assert v.vote_kind == "standard"

    def test_election_vote(self) -> None:
        v = parse_vote(_detail("detail_house_119_1_2_election.json"))
        assert v.vote_kind == "election"
        # Counts NULL because party totals are bucketed by candidate.
        assert v.yea_count is None
        assert v.nay_count is None
        assert v.result == "Johnson"

    def test_procedural_two_thirds(self) -> None:
        v = parse_vote(_detail("detail_house_119_1_300_procedural.json"))
        assert v.threshold == "two_thirds"
        assert v.bill_id is None
        assert v.amendment_id is None


class TestParseVotePositions:
    def test_extracts_bioguide_and_party(self) -> None:
        payload = _fixture("members_house_119_1_240.json")["houseRollCallVoteMemberVotes"]
        positions = parse_vote_positions(payload)
        assert len(positions) == 4
        by_bg = {p.bioguide_id: p for p in positions}
        assert by_bg["S001176"].position == "Yea"
        assert by_bg["S001176"].vote_party == "R"
        assert by_bg["P000197"].position == "Nay"
        assert by_bg["P000197"].vote_party == "D"

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
