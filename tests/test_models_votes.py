"""Tests for the Vote pydantic models and parse helpers."""

import json
from pathlib import Path
from typing import Any

import pytest

from concord.models import (
    Vote,
    VotePosition,
    amendment_id_from_components,
    parse_vote_threshold,
    vote_id_from_components,
)
from concord.models.votes import (
    _build_senate_amendment_id,
    _build_senate_bill_id_from_amendment_target,
    _parse_senate_date,
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
        ],
    )
    def test_known_and_unknown(self, raw: str, expected: str | None) -> None:
        assert parse_vote_threshold(raw) == expected


class TestVoteFromCongressApi:
    def test_bill_vote_real_capture(self) -> None:
        # Real spike capture: HR 3424, "On Motion to Suspend the Rules
        # and Pass", 2/3 Yea-And-Nay → two_thirds threshold.
        v = Vote.from_congress_api(_detail("detail_house_119_1_240.json"), chamber="house")
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
        v = Vote.from_congress_api(
            _detail("synthetic_bill_vote_party_unity_detail.json"), chamber="house"
        )
        assert v.vote_id == "house-119-1-240"
        assert v.yea_count == 222
        assert v.nay_count == 203
        assert v.threshold == "simple_majority"

    def test_amendment_vote_populates_both_ids(self) -> None:
        # Real spike capture: roll 245, amendment HAMDT 85 to HR 3838.
        v = Vote.from_congress_api(
            _detail("detail_house_119_1_subject_amendment.json"), chamber="house"
        )
        assert v.bill_id == "119-hr-3838"
        assert v.amendment_id == "119-hamdt-85"
        assert v.vote_kind == "standard"

    def test_election_vote(self) -> None:
        # Real spike capture: Speaker election, roll 2, candidate-bucketed
        # party totals.
        v = Vote.from_congress_api(
            _detail("detail_house_119_1_subject_procedural.json"), chamber="house"
        )
        assert v.vote_kind == "election"
        # Counts NULL because party totals are bucketed by candidate.
        assert v.yea_count is None
        assert v.nay_count is None
        assert "Johnson" in v.result

    def test_procedural_two_thirds(self) -> None:
        # Synthetic procedural fixture: "On Approving the Journal", 2/3.
        v = Vote.from_congress_api(
            _detail("detail_house_119_1_300_procedural.json"), chamber="house"
        )
        assert v.threshold == "two_thirds"
        assert v.bill_id is None
        assert v.amendment_id is None


class TestVotePositionFromCongressApi:
    def test_extracts_full_real_roster(self) -> None:
        # Real spike fixture: ~430 House Members.
        payload = _fixture("members_house_119_1_240.json")["houseRollCallVoteMemberVotes"]
        positions = [VotePosition.from_congress_api(row) for row in payload["results"]]
        assert len(positions) >= 400
        by_bg = {p.bioguide_id: p for p in positions}
        # Sample Member from the captured payload.
        aderholt = by_bg["A000055"]
        assert aderholt.position == "Yea"
        assert aderholt.vote_party == "R"
        assert aderholt.vote_state == "AL"

    def test_election_positions_carry_surnames(self) -> None:
        payload = _fixture("members_house_119_1_2_election.json")["houseRollCallVoteMemberVotes"]
        positions = [VotePosition.from_congress_api(row) for row in payload["results"]]
        by_bg = {p.bioguide_id: p for p in positions}
        assert by_bg["S001176"].position == "Johnson"
        assert by_bg["P000197"].position == "Jeffries"

    def test_raises_for_row_without_bioguide(self) -> None:
        with pytest.raises(ValueError, match="bioguide"):
            VotePosition.from_congress_api({"voteCast": "Yea"})


class TestVoteModel:
    def test_lowercases_chamber(self) -> None:
        v = Vote(
            vote_id="house-119-1-1",
            chamber="House",  # type: ignore[arg-type]
            congress=119,
            session=1,
            roll_number=1,
            vote_kind="standard",
            start_date="2026-01-01T00:00:00Z",
            vote_question="Q",
            vote_type="Yea-and-Nay",
            result="Passed",
            update_date="2026-01-01",
        )
        assert v.chamber == "house"

    def test_vote_position_round_trip(self) -> None:
        p = VotePosition(bioguide_id="X000001", position="Yea", vote_party="R", vote_state="AL")
        assert p.bioguide_id == "X000001"
        assert p.position == "Yea"
        assert p.vote_party == "R"
        assert p.vote_state == "AL"


# ---------------------------------------------------------------------------
# SenateVoteDetail private parsing helpers (collocated with the classmethod
# per ADR 0018; end-to-end tests live in tests/test_senate_xml.py against
# real senate.gov XML fixtures)
# ---------------------------------------------------------------------------


class TestParseSenateDate:
    def test_double_space_format(self) -> None:
        assert _parse_senate_date("January 20, 2025,  06:12 PM") == "2025-01-20T18:12:00-05:00"

    def test_single_space_format(self) -> None:
        assert _parse_senate_date("January 20, 2025, 06:12 PM") == "2025-01-20T18:12:00-05:00"

    def test_empty_returns_none(self) -> None:
        assert _parse_senate_date(None) is None
        assert _parse_senate_date("") is None

    def test_malformed_returns_none(self) -> None:
        assert _parse_senate_date("not a date") is None


class TestBuildSenateIds:
    def test_amendment_id_strips_dots(self) -> None:
        assert _build_senate_amendment_id(119, "S.Amdt. 14") == "119-samdt-14"
        assert _build_senate_amendment_id(119, "H.Amdt. 27") == "119-hamdt-27"

    def test_amendment_id_returns_none_for_unparseable(self) -> None:
        assert _build_senate_amendment_id(119, "") is None
        assert _build_senate_amendment_id(119, "S.Amdt.") is None

    def test_bill_id_from_amendment_target(self) -> None:
        assert _build_senate_bill_id_from_amendment_target(119, "S. 5") == "119-s-5"
        assert _build_senate_bill_id_from_amendment_target(119, "H.R. 1234") == "119-hr-1234"

    def test_bill_id_from_amendment_target_unknown_type(self) -> None:
        assert _build_senate_bill_id_from_amendment_target(119, "PN 11") is None
