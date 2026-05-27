"""Integration tests for the Votes Stage 0 scraper (Phase 3a)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from concord.api import Client
from concord.scraper.votes import (
    HOUSE_VOTE_POSITIONS_JSONL_NAME,
    HOUSE_VOTES_JSONL_NAME,
    SENATE_ROSTER_JSONL_NAME,
    SENATE_VOTES_JSONL_NAME,
    ScrapeProgressEvent,
    scrape_house,
    scrape_senate,
)
from concord.senate_xml import SenateClient, SenateXmlError

FIXED_FETCHED_AT = datetime(2026, 5, 26, 14, 2, 11, tzinfo=UTC)
VOTES_DIR = Path(__file__).parent / "fixtures" / "api" / "votes"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((VOTES_DIR / name).read_text())


def _two_roll_list_payload() -> dict[str, Any]:
    """Inline list payload pairing the two synthetic detail fixtures.

    Master's real ``list_house_119_1.json`` pairs rolls 240 and 306,
    but no real fixture for roll 306 exists. The scraper test needs a
    list whose stubs match the detail fixtures it has on hand, so we
    synthesize the list payload here.
    """
    return {
        "houseRollCallVotes": [
            {"congress": 119, "sessionNumber": 1, "rollCallNumber": 240},
            {"congress": 119, "sessionNumber": 1, "rollCallNumber": 241},
        ],
        "pagination": {"count": 2},
    }


def _client(
    list_payload: dict[str, Any],
    details_by_roll: dict[int, dict[str, Any]],
    members_by_roll: dict[int, dict[str, Any]],
) -> Client:
    """Serve list / detail / members responses by URL path shape.

    Path shapes:
      /v3/house-vote/{c}/{s}                        (list)        — 5 parts
      /v3/house-vote/{c}/{s}/{roll}                 (detail)      — 6 parts
      /v3/house-vote/{c}/{s}/{roll}/members         (members)     — 7 parts
    """

    def handler(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.rstrip("/").split("/")
        if len(parts) == 5:
            body = list_payload
        elif len(parts) == 6:
            body = details_by_roll[int(parts[-1])]
        elif len(parts) == 7 and parts[-1] == "members":
            body = members_by_roll[int(parts[-2])]
        else:
            return httpx.Response(404)
        return httpx.Response(
            200,
            content=json.dumps(body),
            headers={"content-type": "application/json"},
        )

    return Client(
        api_key="test-key",
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
    )


class TestScrapeHouse:
    def test_writes_paired_detail_and_members(self, tmp_path: Path) -> None:
        client = _client(
            _two_roll_list_payload(),
            {
                240: _fixture("detail_house_119_1_240.json"),
                241: _fixture("detail_house_119_1_241_amendment.json"),
            },
            {
                240: _fixture("members_house_119_1_240.json"),
                241: _fixture("members_house_119_1_241.json"),
            },
        )
        with client:
            stats = scrape_house(
                client=client,
                congresses=[119],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
                sessions=(1,),
            )

        assert stats.votes_written == 2
        assert stats.positions_written == 2
        detail_lines = (tmp_path / HOUSE_VOTES_JSONL_NAME).read_text().splitlines()
        members_lines = (tmp_path / HOUSE_VOTE_POSITIONS_JSONL_NAME).read_text().splitlines()
        assert len(detail_lines) == 2
        assert len(members_lines) == 2

    def test_envelope_shape_matches_adr_0006(self, tmp_path: Path) -> None:
        client = _client(
            _two_roll_list_payload(),
            {
                240: _fixture("detail_house_119_1_240.json"),
                241: _fixture("detail_house_119_1_241_amendment.json"),
            },
            {
                240: _fixture("members_house_119_1_240.json"),
                241: _fixture("members_house_119_1_241.json"),
            },
        )
        with client:
            scrape_house(
                client=client,
                congresses=[119],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
                sessions=(1,),
            )
        env = json.loads((tmp_path / HOUSE_VOTES_JSONL_NAME).read_text().splitlines()[0])
        assert env["fetched_at"] == FIXED_FETCHED_AT.isoformat()
        assert env["key"] == {
            "chamber": "house",
            "congress": 119,
            "session": 1,
            "roll_number": 240,
        }
        assert "payload" in env

    def test_only_writes_house_files(self, tmp_path: Path) -> None:
        client = _client(
            _two_roll_list_payload(),
            {
                240: _fixture("detail_house_119_1_240.json"),
                241: _fixture("detail_house_119_1_241_amendment.json"),
            },
            {
                240: _fixture("members_house_119_1_240.json"),
                241: _fixture("members_house_119_1_241.json"),
            },
        )
        with client:
            scrape_house(
                client=client,
                congresses=[119],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
                sessions=(1,),
            )
        files = sorted(p.name for p in tmp_path.iterdir())
        assert files == sorted([HOUSE_VOTES_JSONL_NAME, HOUSE_VOTE_POSITIONS_JSONL_NAME])

    def test_limit_caps_writes(self, tmp_path: Path) -> None:
        client = _client(
            _two_roll_list_payload(),
            {
                240: _fixture("detail_house_119_1_240.json"),
                241: _fixture("detail_house_119_1_241_amendment.json"),
            },
            {
                240: _fixture("members_house_119_1_240.json"),
                241: _fixture("members_house_119_1_241.json"),
            },
        )
        with client:
            stats = scrape_house(
                client=client,
                congresses=[119],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
                sessions=(1,),
                limit=1,
            )
        assert stats.votes_written == 1
        assert stats.positions_written == 1

    def test_progress_emitted_per_pair(self, tmp_path: Path) -> None:
        events: list[ScrapeProgressEvent] = []
        client = _client(
            _two_roll_list_payload(),
            {
                240: _fixture("detail_house_119_1_240.json"),
                241: _fixture("detail_house_119_1_241_amendment.json"),
            },
            {
                240: _fixture("members_house_119_1_240.json"),
                241: _fixture("members_house_119_1_241.json"),
            },
        )
        with client:
            scrape_house(
                client=client,
                congresses=[119],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
                sessions=(1,),
                progress=events.append,
            )
        assert len(events) == 1
        assert events[0].chamber == "house"
        assert events[0].congress == 119
        assert events[0].session == 1
        assert events[0].votes_written == 2


SENATE_FIXTURES = Path(__file__).parent / "fixtures" / "senate"


def _senate_xml_response(body: bytes) -> httpx.Response:
    return httpx.Response(200, content=body, headers={"content-type": "application/xml"})


def _senate_client(
    roster_xml: bytes,
    menu_by_session: dict[tuple[int, int], bytes],
    detail_by_roll: dict[tuple[int, int, int], bytes],
    *,
    html_for_rolls: set[tuple[int, int, int]] | None = None,
) -> SenateClient:
    """Mock the three senate.gov endpoints by URL shape."""
    html_for_rolls = html_for_rolls or set()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("senators_cfm.xml"):
            return _senate_xml_response(roster_xml)
        if "vote_menu_" in url:
            # vote_menu_{c}_{s}.xml
            stem = url.rsplit("/", 1)[-1].removesuffix(".xml")
            _, _, c, s = stem.split("_")
            return _senate_xml_response(menu_by_session[(int(c), int(s))])
        if "/roll_call_votes/" in url:
            # vote_{c}_{s}_{r5}.xml
            stem = url.rsplit("/", 1)[-1].removesuffix(".xml")
            _, c, s, r5 = stem.split("_")
            key = (int(c), int(s), int(r5))
            if key in html_for_rolls:
                return httpx.Response(
                    200,
                    content=b"<html><body>missing</body></html>",
                    headers={"content-type": "text/html"},
                )
            return _senate_xml_response(detail_by_roll[key])
        return httpx.Response(404)

    return SenateClient(
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
    )


def _menu_xml(roll_numbers: list[int]) -> bytes:
    """Synthesize a minimal vote_menu XML listing the given roll numbers."""
    rows = "".join(f"<vote><vote_number>{n:05d}</vote_number></vote>" for n in roll_numbers)
    return (
        b"<?xml version='1.0'?><vote_summary><votes>"
        + rows.encode("ascii")
        + b"</votes></vote_summary>"
    )


class TestScrapeSenate:
    def test_writes_roster_and_detail_envelopes(self, tmp_path: Path) -> None:
        roster_xml = (SENATE_FIXTURES / "senators_cfm.xml").read_bytes()
        detail_xml = (SENATE_FIXTURES / "detail_119_1_00007_bill.xml").read_bytes()
        another_xml = (SENATE_FIXTURES / "detail_119_1_00003_amendment.xml").read_bytes()
        client = _senate_client(
            roster_xml,
            {(119, 1): _menu_xml([3, 7])},
            {(119, 1, 3): another_xml, (119, 1, 7): detail_xml},
        )

        with client:
            stats = scrape_senate(
                client_xml=client,
                congresses=[119],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
                sessions=(1,),
                sleep=lambda _s: None,
            )

        assert stats.votes_written == 2
        assert stats.votes_seen == 2

        detail_lines = (tmp_path / SENATE_VOTES_JSONL_NAME).read_text().splitlines()
        roster_lines = (tmp_path / SENATE_ROSTER_JSONL_NAME).read_text().splitlines()
        assert len(detail_lines) == 2
        assert len(roster_lines) == 1

        envelopes = [json.loads(line) for line in detail_lines]
        for env in envelopes:
            assert env["key"]["chamber"] == "senate"
            assert env["key"]["congress"] == 119
            assert env["key"]["session"] == 1
            assert isinstance(env["payload"], str)
            assert env["payload"].startswith("<?xml")
        rolls = {env["key"]["roll_number"] for env in envelopes}
        assert rolls == {3, 7}

        roster_env = json.loads(roster_lines[0])
        assert roster_env["key"] == {"source": "senators_cfm"}
        assert roster_env["payload"].startswith("<?xml")

    def test_limit_caps_detail_but_roster_always_written(self, tmp_path: Path) -> None:
        roster_xml = (SENATE_FIXTURES / "senators_cfm.xml").read_bytes()
        detail_xml = (SENATE_FIXTURES / "detail_119_1_00007_bill.xml").read_bytes()
        another_xml = (SENATE_FIXTURES / "detail_119_1_00003_amendment.xml").read_bytes()
        client = _senate_client(
            roster_xml,
            {(119, 1): _menu_xml([3, 7])},
            {(119, 1, 3): another_xml, (119, 1, 7): detail_xml},
        )

        with client:
            stats = scrape_senate(
                client_xml=client,
                congresses=[119],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
                sessions=(1,),
                limit=1,
                sleep=lambda _s: None,
            )

        assert stats.votes_written == 1
        assert (tmp_path / SENATE_VOTES_JSONL_NAME).read_text().count("\n") == 1
        assert (tmp_path / SENATE_ROSTER_JSONL_NAME).read_text().count("\n") == 1

    def test_progress_emitted_per_pair(self, tmp_path: Path) -> None:
        roster_xml = (SENATE_FIXTURES / "senators_cfm.xml").read_bytes()
        detail_xml = (SENATE_FIXTURES / "detail_119_1_00007_bill.xml").read_bytes()
        client = _senate_client(
            roster_xml,
            {(119, 1): _menu_xml([7])},
            {(119, 1, 7): detail_xml},
        )
        events: list[ScrapeProgressEvent] = []
        with client:
            scrape_senate(
                client_xml=client,
                congresses=[119],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
                sessions=(1,),
                progress=events.append,
                sleep=lambda _s: None,
            )
        done_events = [e for e in events if e.is_pair_done]
        assert len(done_events) == 1
        assert done_events[0].chamber == "senate"
        assert done_events[0].votes_written == 1

    def test_html_404_trap_raises_without_corrupting_file(self, tmp_path: Path) -> None:
        roster_xml = (SENATE_FIXTURES / "senators_cfm.xml").read_bytes()
        good_xml = (SENATE_FIXTURES / "detail_119_1_00007_bill.xml").read_bytes()
        client = _senate_client(
            roster_xml,
            {(119, 1): _menu_xml([7, 8])},
            {(119, 1, 7): good_xml, (119, 1, 8): good_xml},
            html_for_rolls={(119, 1, 8)},
        )

        with client, pytest.raises(SenateXmlError, match="HTML"):
            scrape_senate(
                client_xml=client,
                congresses=[119],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
                sessions=(1,),
                sleep=lambda _s: None,
            )

        detail_lines = (tmp_path / SENATE_VOTES_JSONL_NAME).read_text().splitlines()
        # Roll 7 wrote successfully; roll 8 raised before its append.
        assert len(detail_lines) == 1
        # File ends in newline — no half-written partial line.
        assert (tmp_path / SENATE_VOTES_JSONL_NAME).read_text().endswith("\n")
