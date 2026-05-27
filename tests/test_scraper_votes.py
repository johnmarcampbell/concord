"""Integration tests for the Votes Stage 0 scraper (Phase 3a)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from concord.api import Client
from concord.scraper.votes import (
    HOUSE_VOTE_POSITIONS_JSONL_NAME,
    HOUSE_VOTES_JSONL_NAME,
    ScrapeProgressEvent,
    scrape_house,
)

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
