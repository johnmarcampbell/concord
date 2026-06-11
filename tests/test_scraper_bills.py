"""Integration tests for the Bills Stage 0 scraper."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from concord.api import Client
from concord.models.bills import BILL_SECTIONS, BILL_SECTIONS_BY_NAME
from concord.scraper.bills import (
    BILLS_JSONL_NAME,
    EnrichProgressEvent,
    ScrapeProgressEvent,
    scrape_basic,
    scrape_enrichment,
)

FIXED_FETCHED_AT = datetime(2026, 5, 25, 14, 2, 11, tzinfo=UTC)


def _section_jsonl(section: str) -> str:
    """The catalogue's JSONL filename for one Bill section."""
    return BILL_SECTIONS_BY_NAME[section].jsonl_name


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
        assert datetime.fromisoformat(envelopes[0]["fetched_at"]) == FIXED_FETCHED_AT
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
        done_events = [e for e in events if e.is_pair_done]
        assert len(done_events) == 1
        assert done_events[0].congress == 119
        assert done_events[0].bill_type == "hr"
        assert done_events[0].bills_written == 2

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


def _enrichment_client(
    *,
    failing_sections: set[str] | None = None,
) -> Client:
    """Return a Client that answers all five sub-endpoints from local fixtures."""
    fixtures = {
        "cosponsors": _fixture("cosponsors_119_hr_22.json"),
        "actions": _fixture("actions_119_hr_1.json"),
        "subjects": _fixture("subjects_119_hr_1.json"),
        "titles": _fixture("titles_119_hr_1.json"),
        "summaries": _fixture("summaries_119_hr_1.json"),
    }
    failing = failing_sections or set()

    def handler(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.rstrip("/").split("/")
        # /v3/bill/{c}/{t}/{n}/{section}
        if len(parts) != 7:
            return httpx.Response(404)
        section = parts[-1]
        if section in failing:
            return httpx.Response(500)
        body = fixtures[section]
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


class TestScrapeEnrichment:
    def test_writes_one_envelope_per_section(self, tmp_path: Path) -> None:
        client = _enrichment_client()
        with client:
            stats = scrape_enrichment(
                client=client,
                bill_keys=[(119, "hr", 1)],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
            )
        assert stats.bills_enriched == 1
        assert stats.snapshots_written == 5
        for section in BILL_SECTIONS:
            path = tmp_path / section.jsonl_name
            assert path.exists()
            lines = path.read_text().splitlines()
            assert len(lines) == 1
            env = json.loads(lines[0])
            assert env["key"] == {"congress": 119, "bill_type": "hr", "bill_number": 1}
            assert datetime.fromisoformat(env["fetched_at"]) == FIXED_FETCHED_AT

    def test_sections_subset_only_writes_listed_files(self, tmp_path: Path) -> None:
        client = _enrichment_client()
        with client:
            scrape_enrichment(
                client=client,
                bill_keys=[(119, "hr", 1)],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
                sections=["cosponsors", "actions"],
            )
        files = {p.name for p in tmp_path.iterdir()}
        assert files == {
            _section_jsonl("cosponsors"),
            _section_jsonl("actions"),
        }

    def test_partial_failure_leaves_other_sections_intact(self, tmp_path: Path) -> None:
        client = _enrichment_client(failing_sections={"summaries"})
        events: list[EnrichProgressEvent] = []
        with client:
            stats = scrape_enrichment(
                client=client,
                bill_keys=[(119, "hr", 1)],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
                progress=events.append,
            )
        # Four sections written, one failed.
        assert stats.snapshots_written == 4
        assert stats.section_failures == 1
        assert (tmp_path / _section_jsonl("cosponsors")).exists()
        assert (tmp_path / _section_jsonl("summaries")).read_text() == ""
        assert events[0].partial_failures == ("summaries",)

    def test_limit_caps_bills(self, tmp_path: Path) -> None:
        client = _enrichment_client()
        with client:
            stats = scrape_enrichment(
                client=client,
                bill_keys=[(119, "hr", 1), (119, "hr", 22), (119, "hr", 47)],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
                limit=2,
            )
        assert stats.bills_enriched == 2

    def test_canonicalizes_bill_type(self, tmp_path: Path) -> None:
        client = _enrichment_client()
        with client:
            scrape_enrichment(
                client=client,
                bill_keys=[(119, "HR", 1)],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
                sections=["cosponsors"],
            )
        env = json.loads((tmp_path / _section_jsonl("cosponsors")).read_text().splitlines()[0])
        assert env["key"]["bill_type"] == "hr"

    def test_unknown_section_raises(self, tmp_path: Path) -> None:
        client = _enrichment_client()
        with client, pytest.raises(ValueError, match="unknown Bill section"):
            scrape_enrichment(
                client=client,
                bill_keys=[(119, "hr", 1)],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
                sections=["nonsense"],
            )


# ---------------------------------------------------------------------------
# --skip-unchanged (ADR 0015)
# ---------------------------------------------------------------------------


def _seed_bills_jsonl(
    path: Path,
    *,
    fetched_at: str,
    keys: list[tuple[int, str, int]],
    payload: dict[str, Any] | None = None,
) -> None:
    payload = payload or {}
    with path.open("a", encoding="utf-8") as fh:
        for congress, bt, number in keys:
            fh.write(
                json.dumps(
                    {
                        "fetched_at": fetched_at,
                        "key": {
                            "congress": congress,
                            "bill_type": bt,
                            "bill_number": number,
                        },
                        "payload": payload,
                    }
                )
            )
            fh.write("\n")


def _stub_list(*stubs: dict[str, Any]) -> dict[str, Any]:
    return {"bills": list(stubs), "pagination": {"count": len(stubs)}}


class TestScrapeBasicSkipUnchanged:
    def test_flag_off_unchanged_behavior(self, tmp_path: Path) -> None:
        client = _client_serving(
            _stub_list(
                {"number": "1", "type": "HR", "updateDate": "2026-04-01"},
            ),
            {1: _fixture("detail_119_hr_1.json")},
        )
        with client:
            stats = scrape_basic(
                client=client,
                congresses=[119],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
                bill_types=["hr"],
            )
        assert stats.bills_written == 1
        assert stats.bills_skipped == 0

    def test_empty_jsonl_no_skip(self, tmp_path: Path) -> None:
        client = _client_serving(
            _stub_list({"number": "1", "type": "HR", "updateDate": "2026-04-01"}),
            {1: _fixture("detail_119_hr_1.json")},
        )
        with client:
            stats = scrape_basic(
                client=client,
                congresses=[119],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
                bill_types=["hr"],
                skip_unchanged=True,
            )
        assert stats.bills_written == 1
        assert stats.bills_skipped == 0

    def test_stub_older_than_snapshot_skips(self, tmp_path: Path) -> None:
        out = tmp_path / BILLS_JSONL_NAME
        _seed_bills_jsonl(
            out,
            fetched_at="2026-05-01T00:00:00Z",
            keys=[(119, "hr", 1)],
        )
        client = _client_serving(
            _stub_list({"number": "1", "type": "HR", "updateDate": "2026-01-01"}),
            {1: _fixture("detail_119_hr_1.json")},
        )
        with client:
            stats = scrape_basic(
                client=client,
                congresses=[119],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
                bill_types=["hr"],
                skip_unchanged=True,
            )
        assert stats.bills_written == 0
        assert stats.bills_skipped == 1
        # Seed line preserved, no new line.
        assert len(out.read_text().splitlines()) == 1

    def test_stub_newer_than_snapshot_fetches(self, tmp_path: Path) -> None:
        out = tmp_path / BILLS_JSONL_NAME
        _seed_bills_jsonl(
            out,
            fetched_at="2026-01-01T00:00:00Z",
            keys=[(119, "hr", 1)],
        )
        client = _client_serving(
            _stub_list({"number": "1", "type": "HR", "updateDate": "2026-04-01"}),
            {1: _fixture("detail_119_hr_1.json")},
        )
        with client:
            stats = scrape_basic(
                client=client,
                congresses=[119],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
                bill_types=["hr"],
                skip_unchanged=True,
            )
        assert stats.bills_written == 1
        assert stats.bills_skipped == 0

    def test_stub_equal_to_snapshot_skips(self, tmp_path: Path) -> None:
        out = tmp_path / BILLS_JSONL_NAME
        _seed_bills_jsonl(
            out,
            fetched_at="2026-04-01T00:00:00Z",
            keys=[(119, "hr", 1)],
        )
        client = _client_serving(
            _stub_list({"number": "1", "type": "HR", "updateDate": "2026-04-01"}),
            {1: _fixture("detail_119_hr_1.json")},
        )
        with client:
            stats = scrape_basic(
                client=client,
                congresses=[119],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
                bill_types=["hr"],
                skip_unchanged=True,
            )
        assert stats.bills_skipped == 1

    def test_date_only_stub_vs_full_iso_snapshot(self, tmp_path: Path) -> None:
        # Stub "2026-04-01" → midnight UTC. Snapshot fetched_at = 05:00:00Z → after.
        out = tmp_path / BILLS_JSONL_NAME
        _seed_bills_jsonl(
            out,
            fetched_at="2026-04-01T05:00:00Z",
            keys=[(119, "hr", 1)],
        )
        client = _client_serving(
            _stub_list({"number": "1", "type": "HR", "updateDate": "2026-04-01"}),
            {1: _fixture("detail_119_hr_1.json")},
        )
        with client:
            stats = scrape_basic(
                client=client,
                congresses=[119],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
                bill_types=["hr"],
                skip_unchanged=True,
            )
        assert stats.bills_skipped == 1

    def test_missing_update_date_fetches(self, tmp_path: Path) -> None:
        out = tmp_path / BILLS_JSONL_NAME
        _seed_bills_jsonl(
            out,
            fetched_at="2026-05-01T00:00:00Z",
            keys=[(119, "hr", 1)],
        )
        client = _client_serving(
            _stub_list({"number": "1", "type": "HR"}),  # no updateDate
            {1: _fixture("detail_119_hr_1.json")},
        )
        with client:
            stats = scrape_basic(
                client=client,
                congresses=[119],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
                bill_types=["hr"],
                skip_unchanged=True,
            )
        # Fail-safe: unparseable signal → fetch.
        assert stats.bills_written == 1
        assert stats.bills_skipped == 0


class TestScrapeEnrichmentSkipUnchanged:
    def test_per_section_freshness(self, tmp_path: Path) -> None:
        # cosponsors was fetched fresh; the other four are missing → fetch them.
        existing_cosponsors = tmp_path / _section_jsonl("cosponsors")
        existing_cosponsors.write_text(
            json.dumps(
                {
                    "fetched_at": "2026-05-01T00:00:00Z",
                    "key": {"congress": 119, "bill_type": "hr", "bill_number": 1},
                    "payload": {},
                }
            )
            + "\n"
        )

        def _signal_lookup(key: tuple[int, str, int]) -> datetime | None:
            # Bill stub updated 2026-04-01 (older than cosponsors' 2026-05-01)
            return datetime(2026, 4, 1, tzinfo=UTC)

        client = _enrichment_client()
        with client:
            stats = scrape_enrichment(
                client=client,
                bill_keys=[(119, "hr", 1)],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
                skip_unchanged=True,
                bill_signal_lookup=_signal_lookup,
            )
        # 4 fetched (actions, subjects, titles, summaries); 1 skipped (cosponsors).
        assert stats.snapshots_written == 4
        assert stats.sections_skipped == 1
        # cosponsors file unchanged: one line.
        assert len(existing_cosponsors.read_text().splitlines()) == 1

    def test_flag_off_fetches_all(self, tmp_path: Path) -> None:
        existing_cosponsors = tmp_path / _section_jsonl("cosponsors")
        existing_cosponsors.write_text(
            json.dumps(
                {
                    "fetched_at": "2026-05-01T00:00:00Z",
                    "key": {"congress": 119, "bill_type": "hr", "bill_number": 1},
                    "payload": {},
                }
            )
            + "\n"
        )
        client = _enrichment_client()
        with client:
            stats = scrape_enrichment(
                client=client,
                bill_keys=[(119, "hr", 1)],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
            )
        assert stats.snapshots_written == 5
        assert stats.sections_skipped == 0

    def test_no_signal_lookup_falls_through(self, tmp_path: Path) -> None:
        # skip_unchanged set but no signal lookup → can't decide, fetch.
        existing_cosponsors = tmp_path / _section_jsonl("cosponsors")
        existing_cosponsors.write_text(
            json.dumps(
                {
                    "fetched_at": "2026-05-01T00:00:00Z",
                    "key": {"congress": 119, "bill_type": "hr", "bill_number": 1},
                    "payload": {},
                }
            )
            + "\n"
        )
        client = _enrichment_client()
        with client:
            stats = scrape_enrichment(
                client=client,
                bill_keys=[(119, "hr", 1)],
                storage_dir=tmp_path,
                fetched_at=FIXED_FETCHED_AT,
                skip_unchanged=True,
                bill_signal_lookup=None,
            )
        assert stats.snapshots_written == 5
        assert stats.sections_skipped == 0
