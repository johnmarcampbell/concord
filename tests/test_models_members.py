"""Tests for Member / Term / MemberSnapshot models.

Covers (1) parsing the identity half of a real API payload, (2) the
per-Congress Term projection that takes the queried Congress as input,
(3) the ADR 0006 snapshot envelope round-trip, and (4) the
state/chamber normalization the projection layer performs at the
boundary.

The list endpoint ``/v3/member/congress/{n}`` returns the **same**
payload regardless of which Congress you queried, so a Term row's
``congress`` can only come from the scraper's queried-Congress
context — see :class:`TestParseMemberTerm`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from concord.models import (
    MemberSnapshot,
    Term,
    normalize_state,
    parse_member,
    parse_member_identity,
    parse_member_term,
)

from ._snapshots import wrap_snapshot

FIXED_FETCHED_AT = datetime(2026, 5, 25, 14, 2, 11, tzinfo=UTC)


class TestNormalization:
    @pytest.mark.parametrize(
        ("input_", "expected"),
        [
            ("Vermont", "VT"),
            ("New York", "NY"),
            ("vermont", "VT"),  # case-insensitive
            ("VT", "VT"),  # pass-through for codes
            ("Puerto Rico", "PR"),
            (None, None),
        ],
    )
    def test_normalize_state(self, input_: str | None, expected: str | None) -> None:
        assert normalize_state(input_) == expected


# -- Identity (Member-row) parsing ------------------------------------------


class TestParseMemberIdentity:
    """Identity fields don't depend on the queried Congress."""

    def test_parses_house_member(self, load_json_fixture: Any) -> None:
        payload = load_json_fixture("api/members/current_house.json")["members"][0]
        member = parse_member_identity(payload)
        assert member.bioguide_id == "O000172"
        assert member.first_name == "Alexandria"
        assert member.last_name == "Ocasio-Cortez"
        assert member.display_name == "Alexandria Ocasio-Cortez"
        assert member.photo_url == "https://www.congress.gov/img/member/o000172.jpg"

    def test_parses_senate_member_with_birth_year(self, load_json_fixture: Any) -> None:
        payload = load_json_fixture("api/members/current_senate.json")["members"][0]
        member = parse_member_identity(payload)
        assert member.bioguide_id == "S000033"
        assert member.birth_year == 1941
        assert member.death_year is None

    def test_parses_historical_member_with_death_year(self, load_json_fixture: Any) -> None:
        payload = load_json_fixture("api/members/historical.json")["members"][0]
        member = parse_member_identity(payload)
        assert member.bioguide_id == "J000301"
        assert member.middle_name == "M."
        assert member.death_year == 2014


# -- Per-Congress Term parsing ----------------------------------------------


class TestParseMemberTerm:
    """The Term row depends on the queried Congress.

    Each test calls :func:`parse_member_term` with a specific Congress
    and verifies the returned row reflects (a) the right chamber for
    that Congress's year range, (b) Congress-clipped start/end dates.
    """

    def test_current_house_member_in_119th(self, load_json_fixture: Any) -> None:
        payload = load_json_fixture("api/members/current_house.json")["members"][0]
        term = parse_member_term(payload, congress=119)
        assert term is not None
        assert term.bioguide_id == "O000172"
        assert term.congress == 119
        assert term.chamber == "house"
        assert term.state == "NY"
        assert term.district == 14
        assert term.party == "Democratic"
        # Congress 119 spans 2025 (Jan 3) - 2027 (Jan 3).
        assert term.start_date == "2025-01-03"
        assert term.end_date == "2027-01-03"

    def test_same_payload_three_congresses_three_terms(self, load_json_fixture: Any) -> None:
        """Heart of the bug — identical payloads for different Congresses
        must produce different Term rows."""
        payload = load_json_fixture("api/members/current_house.json")["members"][0]
        terms = [parse_member_term(payload, congress=c) for c in (117, 118, 119)]
        assert all(t is not None for t in terms)
        assert [t.congress for t in terms] == [117, 118, 119]  # type: ignore[union-attr]
        assert all(t.chamber == "house" for t in terms)  # type: ignore[union-attr]

    def test_senator_in_each_listed_congress(self, load_json_fixture: Any) -> None:
        """Sanders' Senate term started 2007 (110th); query each subsequent
        Congress and confirm the right chamber surfaces."""
        payload = load_json_fixture("api/members/current_senate.json")["members"][0]
        for congress in (110, 115, 118, 119):
            term = parse_member_term(payload, congress=congress)
            assert term is not None
            assert term.chamber == "senate"
            assert term.district is None
            assert term.congress == congress

    def test_chamber_switch_resolved_by_year_range(self, load_json_fixture: Any) -> None:
        """Sanders served in the House before the Senate — querying a
        Congress in his House-tenure window resolves to ``house``."""
        payload = load_json_fixture("api/members/current_senate.json")["members"][0]
        house_term = parse_member_term(payload, congress=102)  # 1991-1993
        assert house_term is not None
        assert house_term.chamber == "house"

    def test_returns_none_for_congress_outside_terms(self, load_json_fixture: Any) -> None:
        """If no terms.item covers the queried Congress, return None."""
        payload = load_json_fixture("api/members/historical.json")["members"][0]
        # Jeffords left the Senate in 2007; querying Congress 116 (2019-2021)
        # finds no matching term.
        term = parse_member_term(payload, congress=116)
        assert term is None

    def test_left_mid_congress_carries_end_year(self, load_json_fixture: Any) -> None:
        """Jeffords' last Senate Congress was the 109th (2005-2007). His
        endYear=2007 falls inside that Congress's window, so end_date
        reflects the actual departure year."""
        payload = load_json_fixture("api/members/historical.json")["members"][0]
        term = parse_member_term(payload, congress=109)
        assert term is not None
        assert term.end_date == "2007-01-03"

    def test_top_level_state_falls_back_when_term_has_none(self, load_json_fixture: Any) -> None:
        """The list endpoint puts ``state`` at the top level, not in each
        terms.item — the parser uses the top-level fallback."""
        payload = load_json_fixture("api/members/current_senate.json")["members"][0]
        term = parse_member_term(payload, congress=119)
        assert term is not None
        assert term.state == "VT"


# -- Combined parse_member helper -------------------------------------------


class TestParseMemberWrapper:
    def test_returns_identity_and_one_term(self, load_json_fixture: Any) -> None:
        payload = load_json_fixture("api/members/current_house.json")["members"][0]
        member, term = parse_member(payload, congress=119)
        assert member.bioguide_id == "O000172"
        assert term is not None
        assert term.congress == 119

    def test_term_is_none_when_congress_outside_range(self, load_json_fixture: Any) -> None:
        payload = load_json_fixture("api/members/historical.json")["members"][0]
        member, term = parse_member(payload, congress=119)
        assert member.bioguide_id == "J000301"
        assert term is None


# -- Snapshot envelope shape -------------------------------------------------


class TestSnapshotEnvelope:
    def test_round_trip(self) -> None:
        payload = {"bioguideId": "X000001", "name": "Doe, John"}
        envelope = wrap_snapshot(
            payload,
            fetched_at=FIXED_FETCHED_AT,
            key={"bioguide_id": "X000001", "congress": 119},
        )
        snapshot = MemberSnapshot.model_validate(envelope)
        assert snapshot.fetched_at == FIXED_FETCHED_AT
        assert snapshot.key == {"bioguide_id": "X000001", "congress": 119}
        assert snapshot.payload == payload

    def test_envelope_keys_exact(self) -> None:
        envelope = wrap_snapshot(
            {"x": 1},
            fetched_at=FIXED_FETCHED_AT,
            key={"bioguide_id": "Y000001", "congress": 118},
        )
        assert set(envelope.keys()) == {"fetched_at", "key", "payload"}
        assert envelope["key"] == {"bioguide_id": "Y000001", "congress": 118}


# -- Term model validators ---------------------------------------------------


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
