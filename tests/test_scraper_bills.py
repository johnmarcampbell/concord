"""Integration tests for the Bills Stage 0 scraper."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from concord.api import Client
from concord.scraper.bills import BILLS_JSONL_NAME, ScrapeProgressEvent, scrape_basic

FIXED_FETCHED_AT = datetime(2026, 5, 25, 14, 2, 11, tzinfo=UTC)


def _fixture(name: str) -> dict[str, Any]:
    here = Path(__file__).parent / "fixtures" / "api" / "bills"
    return json.loads((here / name).read_text())


def _client_serving(
    list_payload: dict[str, Any],
    details_by_number: dict[int, dict[str, Any]],
) -> Client:
    """Return a Client whose handler answers list + detail calls.

    The list response is served for any list-endpoint request; detail
    responses are looked up by the bill number in the path.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.rstrip("/").split("/")
        # path is /v3/bill/{congress}/{type} (list) or
        # /v3/bill/{congress}/{type}/{number} (detail).
        if len(parts) == 5:  # ["", "v3", "bill", "{c}", "{t}"]
            body = list_payload
        elif len(parts) == 6:  # detail
            number = int(parts[-1])
            body = details_by_number[number]
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


class TestScrapeBasic:
    def test_writes_one_envelope_per_detail(self, tmp_path: Path) -> None:
        client = _client_serving(
            _fixture("list_hr_119.json"),
            {
                1: _fixture("detail_119_hr_1.json"),
                22: _fixture("detail_119_hr_22.json"),
            },
        )

        with client:
            stats = scrape_basic(
                client=client,
                congresses=[119],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
                bill_types=["hr"],
            )

        assert stats.bills_written == 2
        out = tmp_path / BILLS_JSONL_NAME
        lines = out.read_text().splitlines()
        assert len(lines) == 2
        envelopes = [json.loads(line) for line in lines]
        assert envelopes[0]["fetched_at"] == FIXED_FETCHED_AT.isoformat()
        # bill_type in key is lowercased.
        assert envelopes[0]["key"] == {"congress": 119, "bill_type": "hr", "bill_number": 1}
        assert envelopes[1]["key"]["bill_number"] == 22
        # Payload is the unwrapped ``bill`` object.
        assert envelopes[0]["payload"]["number"] == "1"

    def test_only_writes_bills_jsonl(self, tmp_path: Path) -> None:
        client = _client_serving(
            _fixture("list_hr_119.json"),
            {
                1: _fixture("detail_119_hr_1.json"),
                22: _fixture("detail_119_hr_22.json"),
            },
        )
        with client:
            scrape_basic(
                client=client,
                congresses=[119],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
                bill_types=["hr"],
            )
        files = {p.name for p in tmp_path.iterdir()}
        assert files == {BILLS_JSONL_NAME}

    def test_limit_caps_writes(self, tmp_path: Path) -> None:
        client = _client_serving(
            _fixture("list_hr_119.json"),
            {
                1: _fixture("detail_119_hr_1.json"),
                22: _fixture("detail_119_hr_22.json"),
            },
        )
        with client:
            stats = scrape_basic(
                client=client,
                congresses=[119],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
                bill_types=["hr"],
                limit=1,
            )
        assert stats.bills_written == 1
        lines = (tmp_path / BILLS_JSONL_NAME).read_text().splitlines()
        assert len(lines) == 1

    def test_canonicalizes_uppercase_bill_type(self, tmp_path: Path) -> None:
        client = _client_serving(
            _fixture("list_hr_119.json"),
            {
                1: _fixture("detail_119_hr_1.json"),
                22: _fixture("detail_119_hr_22.json"),
            },
        )
        with client:
            scrape_basic(
                client=client,
                congresses=[119],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
                bill_types=["HR"],
            )
        envelopes = [
            json.loads(line) for line in (tmp_path / BILLS_JSONL_NAME).read_text().splitlines()
        ]
        assert all(e["key"]["bill_type"] == "hr" for e in envelopes)

    def test_progress_emits_per_pair(self, tmp_path: Path) -> None:
        events: list[ScrapeProgressEvent] = []
        client = _client_serving(
            _fixture("list_hr_119.json"),
            {
                1: _fixture("detail_119_hr_1.json"),
                22: _fixture("detail_119_hr_22.json"),
            },
        )
        with client:
            scrape_basic(
                client=client,
                congresses=[119],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
                bill_types=["hr"],
                progress=events.append,
            )
        assert len(events) == 1
        assert events[0].congress == 119
        assert events[0].bill_type == "hr"
        assert events[0].bills_written == 2

    def test_creates_storage_dir(self, tmp_path: Path) -> None:
        nested = tmp_path / "nested" / "data"
        client = _client_serving(
            _fixture("list_hr_119.json"),
            {
                1: _fixture("detail_119_hr_1.json"),
                22: _fixture("detail_119_hr_22.json"),
            },
        )
        with client:
            scrape_basic(
                client=client,
                congresses=[119],
                storage_dir=nested,
                fetched_at=FIXED_FETCHED_AT,
                bill_types=["hr"],
            )
        assert (nested / BILLS_JSONL_NAME).exists()
