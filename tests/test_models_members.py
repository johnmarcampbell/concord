"""Tests for Member / Term / MemberSnapshot models.

Covers (1) parsing real API payloads into typed records, (2) the
ADR 0006 envelope round-trip, and (3) the state/chamber normalization
the projection layer performs at the boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from concord.models import (
    Chamber,
    Member,
    MemberSnapshot,
    Term,
    normalize_state,
    parse_member,
)

from ._snapshots import wrap_snapshot

FIXED_FETCHED_AT = datetime(2026, 5, 25, 14, 2, 11, tzinfo=UTC)


class TestNormalization:
    @pytest.mark.parametrize(
        "input_, expected",
        [
            ("Vermont", "VT"),
            ("New York", "NY"),
            ("vermont", "VT"),  # case-insensitive
            ("VT", "VT"),       # pass-through for codes
            ("Puerto Rico", "PR"),
            (None, None),
        ],
    )
    def test_normalize_state(self, input_: str | None, expected: str | None) -> None:
        assert normalize_state(input_) == expected


class TestParseMemberCurrentHouse:
    @pytest.fixture
    def payload(self, load_json_fixture: Any) -> dict[str, Any]:
        return load_json_fixture("api/members/current_house.json")["members"][0]

    def test_parses_identity_fields(self, payload: dict[str, Any]) -> None:
        member, _ = parse_member(payload)
        assert member.bioguide_id == "O000172"
        assert member.first_name == "Alexandria"
        assert member.last_name == "Ocasio-Cortez"
        assert member.display_name == "Alexandria Ocasio-Cortez"
        assert member.photo_url == "https://www.congress.gov/img/member/o000172.jpg"

    def test_parses_terms(self, payload: dict[str, Any]) -> None:
        _, terms = parse_member(payload)
        assert len(terms) == 3
        congresses = sorted(t.congress for t in terms)
        assert congresses == [117, 118, 119]
        for t in terms:
            assert t.bioguide_id == "O000172"
            assert t.chamber == "house"
            assert t.state == "NY"
            assert t.district == 14
            assert t.party == "Democratic"

    def test_current_term_has_no_end_date(self, payload: dict[str, Any]) -> None:
        _, terms = parse_member(payload)
        current = [t for t in terms if t.congress == 119][0]
        assert current.end_date is None
        assert current.start_date == "2025-01-01"


class TestParseMemberCurrentSenate:
    @pytest.fixture
    def payload(self, load_json_fixture: Any) -> dict[str, Any]:
        return load_json_fixture("api/members/current_senate.json")["members"][0]

    def test_senator_has_no_district(self, payload: dict[str, Any]) -> None:
        _, terms = parse_member(payload)
        senate_terms = [t for t in terms if t.chamber == "senate"]
        assert len(senate_terms) == 3
        for t in senate_terms:
            assert t.district is None

    def test_mixed_chamber_history_preserved(self, payload: dict[str, Any]) -> None:
        _, terms = parse_member(payload)
        chambers = {(t.congress, t.chamber) for t in terms}
        # Sanders served one term in the House (102nd) and three in the Senate.
        assert (102, "house") in chambers
        assert (119, "senate") in chambers

    def test_member_carries_birth_year(self, payload: dict[str, Any]) -> None:
        member, _ = parse_member(payload)
        assert member.birth_year == 1941


class TestParseHistoricalMember:
    @pytest.fixture
    def payload(self, load_json_fixture: Any) -> dict[str, Any]:
        return load_json_fixture("api/members/historical.json")["members"][0]

    def test_death_year_parsed(self, payload: dict[str, Any]) -> None:
        member, _ = parse_member(payload)
        assert member.death_year == 2014

    def test_party_change_preserved_per_term(self, payload: dict[str, Any]) -> None:
        _, terms = parse_member(payload)
        parties = {t.congress: t.party for t in terms}
        # Two terms with different parties.
        assert parties[116] == "Republican"
        assert parties[117] == "Independent"

    def test_end_date_populated_for_ended_term(self, payload: dict[str, Any]) -> None:
        _, terms = parse_member(payload)
        ended = [t for t in terms if t.end_date is not None]
        assert ended, "expected at least one term with an end date"


class TestSnapshotEnvelope:
    def test_round_trip(self) -> None:
        payload = {"bioguideId": "X000001", "name": "Doe, John"}
        envelope = wrap_snapshot(
            payload,
            fetched_at=FIXED_FETCHED_AT,
            key={"bioguide_id": "X000001"},
        )
        snapshot = MemberSnapshot.model_validate(envelope)
        assert snapshot.fetched_at == FIXED_FETCHED_AT
        assert snapshot.key == {"bioguide_id": "X000001"}
        assert snapshot.payload == payload

    def test_envelope_keys_exact(self) -> None:
        envelope = wrap_snapshot(
            {"x": 1},
            fetched_at=FIXED_FETCHED_AT,
            key={"bioguide_id": "Y000001"},
        )
        assert set(envelope.keys()) == {"fetched_at", "key", "payload"}


class TestTermValidation:
    def test_chamber_normalizes_long_form(self) -> None:
        term = Term(
            bioguide_id="X000001",
            congress=119,
            chamber="House of Representatives",  # API's verbose form
            state="VT",
            district=1,
        )
        assert term.chamber == "house"

    def test_state_normalizes_full_name(self) -> None:
        term = Term(
            bioguide_id="X000001",
            congress=119,
            chamber="senate",
            state="Vermont",
        )
        assert term.state == "VT"
