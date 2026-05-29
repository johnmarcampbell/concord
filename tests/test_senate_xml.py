"""Tests for the senate.gov LIS XML client and parsers.

All HTTP tests inject an ``httpx.MockTransport`` so nothing leaves the
process. XML responses are real captures from the live feeds, stored
under ``tests/fixtures/senate/``.
"""

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from concord.models import SenateVoteDetail
from concord.senate_xml import (
    DETAIL_URL,
    MENU_URL,
    ROSTER_URL,
    SenateClient,
    SenateXmlError,
    parse_senate_roster,
    parse_vote_menu,
)

FIXTURES = Path(__file__).parent / "fixtures" / "senate"


def _xml_response(body: bytes, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=body, headers={"content-type": "application/xml"})


def _mock(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# parse_vote_menu
# ---------------------------------------------------------------------------


class TestParseVoteMenu:
    def test_returns_all_roll_numbers_ascending(self) -> None:
        xml_bytes = (FIXTURES / "vote_menu_119_1.xml").read_bytes()
        numbers = parse_vote_menu(xml_bytes)

        assert len(numbers) == 659
        assert numbers[0] == 1
        assert numbers[-1] == 659
        assert numbers == sorted(numbers)

    def test_skips_blank_and_non_integer_entries(self) -> None:
        xml_bytes = (
            b"<?xml version='1.0'?>"
            b"<vote_summary>"
            b"<votes>"
            b"<vote><vote_number>00007</vote_number></vote>"
            b"<vote><vote_number></vote_number></vote>"
            b"<vote><vote_number>notanumber</vote_number></vote>"
            b"<vote><vote_number>00003</vote_number></vote>"
            b"</votes>"
            b"</vote_summary>"
        )
        assert parse_vote_menu(xml_bytes) == [3, 7]


# ---------------------------------------------------------------------------
# parse_senate_roster
# ---------------------------------------------------------------------------


class TestParseSenateRoster:
    def test_returns_member_full_to_bioguide_dict(self) -> None:
        xml_bytes = (FIXTURES / "senators_cfm.xml").read_bytes()
        bridge = parse_senate_roster(xml_bytes)

        assert len(bridge) > 90
        assert bridge["Alsobrooks (D-MD)"] == "A000382"
        # Sanity-check the format on a couple more rows.
        assert "Baldwin (D-WI)" in bridge
        assert "Barrasso (R-WY)" in bridge


# ---------------------------------------------------------------------------
# SenateVoteDetail.from_senate_xml (end-to-end parsing of real fixtures)
# ---------------------------------------------------------------------------


class TestSenateVoteDetailFromSenateXml:
    def test_bill_vote(self) -> None:
        xml_bytes = (FIXTURES / "detail_119_1_00007_bill.xml").read_bytes()
        detail = SenateVoteDetail.from_senate_xml(xml_bytes)

        assert detail.bill_id == "119-s-5"
        assert detail.amendment_id is None
        assert detail.threshold == "simple_majority"
        assert detail.yea_count == 64
        assert detail.nay_count == 35
        assert detail.vote_kind == "standard"
        assert detail.vote_id == "senate-119-1-7"
        assert detail.chamber == "senate"
        assert detail.start_date == "2025-01-20T18:12:00-05:00"
        assert detail.update_date == "2025-01-20T18:44:00-05:00"
        assert len(detail.positions) == 99

        alsobrooks = next(p for p in detail.positions if p.last_name == "Alsobrooks")
        assert alsobrooks.member_full == "Alsobrooks (D-MD)"
        assert alsobrooks.lis_member_id == "S428"
        assert alsobrooks.party == "D"
        assert alsobrooks.state == "MD"
        assert alsobrooks.vote_cast == "Nay"

    def test_amendment_vote(self) -> None:
        xml_bytes = (FIXTURES / "detail_119_1_00003_amendment.xml").read_bytes()
        detail = SenateVoteDetail.from_senate_xml(xml_bytes)

        assert detail.amendment_id == "119-samdt-14"
        assert detail.bill_id == "119-s-5"
        assert detail.threshold == "simple_majority"
        assert detail.yea_count == 70
        assert detail.nay_count == 25
        assert detail.not_voting_count == 4

    def test_nomination_vote(self) -> None:
        xml_bytes = (FIXTURES / "detail_119_1_00008_nomination.xml").read_bytes()
        detail = SenateVoteDetail.from_senate_xml(xml_bytes)

        assert detail.bill_id is None
        assert detail.amendment_id is None
        # vote_title is preferred for subjectless votes (nominee identity
        # would otherwise be lost — the short vote_question_text is
        # "On the Nomination PN11-13", which is opaque to readers).
        assert "Marco Rubio" in detail.vote_question
        assert "Secretary of State" in detail.vote_question
        assert (
            detail.vote_title == "Confirmation: Marco Rubio, of Florida, to be Secretary of State"
        )

    def test_cloture_vote(self) -> None:
        xml_bytes = (FIXTURES / "detail_119_1_00001_cloture.xml").read_bytes()
        detail = SenateVoteDetail.from_senate_xml(xml_bytes)

        assert detail.threshold == "three_fifths"
        assert detail.bill_id == "119-s-5"
        assert detail.amendment_id is None
        assert detail.vote_type == "On Cloture on the Motion to Proceed"

    def test_motion_vote(self) -> None:
        xml_bytes = (FIXTURES / "detail_119_1_00002_motion.xml").read_bytes()
        detail = SenateVoteDetail.from_senate_xml(xml_bytes)

        assert detail.bill_id == "119-s-5"
        assert detail.amendment_id is None
        assert detail.threshold == "simple_majority"


# ---------------------------------------------------------------------------
# SenateClient
# ---------------------------------------------------------------------------


class TestSenateClient:
    def test_get_current_senators_xml(self) -> None:
        body = (FIXTURES / "senators_cfm.xml").read_bytes()

        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == ROSTER_URL
            return _xml_response(body)

        with SenateClient(transport=_mock(handler)) as client:
            result = client.get_current_senators_xml()
        assert result == body

    def test_list_roll_call_numbers_returns_sorted(self) -> None:
        menu = (FIXTURES / "vote_menu_119_1.xml").read_bytes()
        expected_url = MENU_URL.format(congress=119, session=1)

        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == expected_url
            return _xml_response(menu)

        with SenateClient(transport=_mock(handler)) as client:
            numbers = client.list_roll_call_numbers(119, 1)
        assert numbers[0] == 1
        assert numbers[-1] == 659

    def test_get_roll_call_xml_zero_pads_roll(self) -> None:
        detail = (FIXTURES / "detail_119_1_00007_bill.xml").read_bytes()
        expected_url = DETAIL_URL.format(congress=119, session=1, roll5="00007")

        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == expected_url
            return _xml_response(detail)

        with SenateClient(transport=_mock(handler)) as client:
            result = client.get_roll_call_xml(119, 1, 7)
        assert result == detail

    def test_html_response_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=b"<html><body>Not found</body></html>",
                headers={"content-type": "text/html; charset=utf-8"},
            )

        with (
            SenateClient(transport=_mock(handler)) as client,
            pytest.raises(SenateXmlError, match="HTML response"),
        ):
            client.get_roll_call_xml(119, 1, 9999)

    def test_404_raises_without_retry(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(404, content=b"")

        with (
            SenateClient(transport=_mock(handler), sleep=lambda _: None) as client,
            pytest.raises(SenateXmlError, match="404"),
        ):
            client.get_roll_call_xml(119, 1, 1)
        assert attempts == 1

    def test_transient_5xx_then_success(self) -> None:
        responses = iter(
            [
                httpx.Response(503, content=b""),
                _xml_response(b"<?xml version='1.0'?><vote_summary><votes/></vote_summary>"),
            ]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return next(responses)

        with SenateClient(transport=_mock(handler), sleep=lambda _: None) as client:
            numbers = client.list_roll_call_numbers(119, 1)
        assert numbers == []

    def test_persistent_5xx_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=b"")

        with (
            SenateClient(transport=_mock(handler), sleep=lambda _: None) as client,
            pytest.raises(SenateXmlError, match="failed after"),
        ):
            client.list_roll_call_numbers(119, 1)
