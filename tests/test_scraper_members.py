"""Integration tests for the Members Stage 0 scraper.

Drives :func:`concord.scraper.members.scrape` end-to-end against the
captured ``tests/fixtures/api/members/*.json`` fixtures via
``httpx.MockTransport``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from concord.api import Client
from concord.scraper.members import ScrapeProgressEvent, scrape

FIXED_FETCHED_AT = datetime(2026, 5, 25, 14, 2, 11, tzinfo=UTC)


def _client_serving(payloads_by_congress: dict[int, dict[str, Any]]) -> Client:
    """Build a test Client that returns the right payload per ``congress``.

    Single page per congress, ``pagination`` is left empty so the iterator
    terminates after one call.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        # Path is ``/v3/member/congress/{n}``
        parts = request.url.path.split("/")
        congress = int(parts[-1])
        body = payloads_by_congress.get(congress, {"members": [], "pagination": {}})
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


def _load_fixture(name: str) -> dict[str, Any]:
    here = Path(__file__).parent / "fixtures" / "api" / "members"
    return json.loads((here / name).read_text())


class TestScrape:
    def test_writes_one_envelope_per_member(self, tmp_path: Path) -> None:
        out = tmp_path / "members.jsonl"
        client = _client_serving({119: _load_fixture("current_house.json")})

        with client:
            stats = scrape(
                client=client,
                congresses=[119],
                storage_path=out,
                fetched_at=FIXED_FETCHED_AT,
            )

        assert stats.members_written == 1
        lines = out.read_text().splitlines()
        assert len(lines) == 1
        envelope = json.loads(lines[0])
        assert envelope["fetched_at"] == FIXED_FETCHED_AT.isoformat()
        assert envelope["key"] == {"bioguide_id": "O000172", "congress": 119}
        assert envelope["payload"]["bioguideId"] == "O000172"

    def test_writes_across_multiple_congresses(self, tmp_path: Path) -> None:
        out = tmp_path / "members.jsonl"
        client = _client_serving(
            {
                117: _load_fixture("historical.json"),
                119: _load_fixture("current_senate.json"),
            }
        )

        with client:
            stats = scrape(
                client=client,
                congresses=[117, 119],
                storage_path=out,
                fetched_at=FIXED_FETCHED_AT,
            )

        assert stats.members_written == 2
        envelopes = [json.loads(line) for line in out.read_text().splitlines()]
        keys = [e["key"]["bioguide_id"] for e in envelopes]
        assert keys == ["J000301", "S000033"]

    def test_appends_does_not_truncate(self, tmp_path: Path) -> None:
        out = tmp_path / "members.jsonl"
        out.write_text('{"existing": true}\n', encoding="utf-8")
        client = _client_serving({119: _load_fixture("current_house.json")})

        with client:
            scrape(
                client=client,
                congresses=[119],
                storage_path=out,
                fetched_at=FIXED_FETCHED_AT,
            )

        lines = out.read_text().splitlines()
        assert lines[0] == '{"existing": true}'
        assert len(lines) == 2

    def test_emits_per_congress_progress(self, tmp_path: Path) -> None:
        events: list[ScrapeProgressEvent] = []
        out = tmp_path / "members.jsonl"
        client = _client_serving(
            {
                117: _load_fixture("historical.json"),
                119: _load_fixture("current_senate.json"),
            }
        )

        with client:
            scrape(
                client=client,
                congresses=[117, 119],
                storage_path=out,
                fetched_at=FIXED_FETCHED_AT,
                progress=events.append,
            )

        done_events = [e for e in events if e.is_congress_done]
        assert [e.congress for e in done_events] == [117, 119]
        assert done_events[0].total_written == 1
        assert done_events[1].total_written == 2

    def test_same_member_in_multiple_congresses_writes_distinct_envelopes(
        self, tmp_path: Path
    ) -> None:
        """Regression — the list endpoint returns identical payloads for the
        same Member across every Congress they served in. Without the queried
        Congress in the envelope key, the loader would collapse them and only
        produce one Term row instead of three."""
        out = tmp_path / "members.jsonl"
        fixture = _load_fixture("current_senate.json")
        client = _client_serving({117: fixture, 118: fixture, 119: fixture})

        with client:
            stats = scrape(
                client=client,
                congresses=[117, 118, 119],
                storage_path=out,
                fetched_at=FIXED_FETCHED_AT,
            )

        assert stats.members_written == 3
        envelopes = [json.loads(line) for line in out.read_text().splitlines()]
        # Same bioguide_id, distinct congress, distinct envelopes.
        assert [e["key"]["bioguide_id"] for e in envelopes] == ["S000033"] * 3
        assert [e["key"]["congress"] for e in envelopes] == [117, 118, 119]

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        out = tmp_path / "nested" / "data" / "members.jsonl"
        client = _client_serving({119: _load_fixture("current_house.json")})

        with client:
            scrape(
                client=client,
                congresses=[119],
                storage_path=out,
                fetched_at=FIXED_FETCHED_AT,
            )

        assert out.exists()


# ---------------------------------------------------------------------------
# --skip-unchanged (ADR 0015)
# ---------------------------------------------------------------------------


def _seed_member_snapshot(path: Path, *, bioguide: str, congress: int, fetched_at: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "fetched_at": fetched_at,
                    "key": {"bioguide_id": bioguide, "congress": congress},
                    "payload": {},
                }
            )
        )
        fh.write("\n")


def _members_payload(*, bioguide: str, update_date: str | None) -> dict[str, Any]:
    member = {
        "bioguideId": bioguide,
        "name": "Test, Person",
        "directOrderName": "Person Test",
        "invertedOrderName": "Test, Person",
        "partyName": "Democratic",
        "state": "NY",
        "terms": {"item": [{"chamber": "House of Representatives", "startYear": 2019}]},
    }
    if update_date is not None:
        member["updateDate"] = update_date
    return {"members": [member], "pagination": {"count": 1}}


class TestScrapeSkipUnchanged:
    def test_flag_off_writes_all(self, tmp_path: Path) -> None:
        out = tmp_path / "members.jsonl"
        _seed_member_snapshot(
            out, bioguide="O000172", congress=119, fetched_at="2026-05-01T00:00:00Z"
        )
        client = _client_serving(
            {119: _members_payload(bioguide="O000172", update_date="2026-01-15T00:00:00Z")}
        )
        with client:
            stats = scrape(
                client=client,
                congresses=[119],
                storage_path=out,
                fetched_at=FIXED_FETCHED_AT,
            )
        assert stats.members_written == 1
        assert stats.members_skipped == 0

    def test_empty_jsonl_no_skip(self, tmp_path: Path) -> None:
        out = tmp_path / "members.jsonl"
        client = _client_serving(
            {119: _members_payload(bioguide="O000172", update_date="2026-01-15T00:00:00Z")}
        )
        with client:
            stats = scrape(
                client=client,
                congresses=[119],
                storage_path=out,
                fetched_at=FIXED_FETCHED_AT,
                skip_unchanged=True,
            )
        assert stats.members_written == 1
        assert stats.members_skipped == 0

    def test_signal_older_skips(self, tmp_path: Path) -> None:
        out = tmp_path / "members.jsonl"
        _seed_member_snapshot(
            out, bioguide="O000172", congress=119, fetched_at="2026-05-01T00:00:00Z"
        )
        client = _client_serving(
            {119: _members_payload(bioguide="O000172", update_date="2026-01-15T00:00:00Z")}
        )
        with client:
            stats = scrape(
                client=client,
                congresses=[119],
                storage_path=out,
                fetched_at=FIXED_FETCHED_AT,
                skip_unchanged=True,
            )
        assert stats.members_written == 0
        assert stats.members_skipped == 1
        assert len(out.read_text().splitlines()) == 1  # only seed line

    def test_signal_newer_fetches(self, tmp_path: Path) -> None:
        out = tmp_path / "members.jsonl"
        _seed_member_snapshot(
            out, bioguide="O000172", congress=119, fetched_at="2026-01-01T00:00:00Z"
        )
        client = _client_serving(
            {119: _members_payload(bioguide="O000172", update_date="2026-04-01T00:00:00Z")}
        )
        with client:
            stats = scrape(
                client=client,
                congresses=[119],
                storage_path=out,
                fetched_at=FIXED_FETCHED_AT,
                skip_unchanged=True,
            )
        assert stats.members_written == 1
        assert stats.members_skipped == 0

    def test_missing_update_date_fetches(self, tmp_path: Path) -> None:
        out = tmp_path / "members.jsonl"
        _seed_member_snapshot(
            out, bioguide="O000172", congress=119, fetched_at="2026-05-01T00:00:00Z"
        )
        client = _client_serving({119: _members_payload(bioguide="O000172", update_date=None)})
        with client:
            stats = scrape(
                client=client,
                congresses=[119],
                storage_path=out,
                fetched_at=FIXED_FETCHED_AT,
                skip_unchanged=True,
            )
        assert stats.members_written == 1  # fail-safe
        assert stats.members_skipped == 0
