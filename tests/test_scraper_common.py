"""Unit tests for :mod:`concord.scraper._common` (ADR 0015 helpers)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from concord.scraper._common import (
    is_stub_unchanged,
    load_bill_signal_map,
    load_freshness_map,
    parse_signal_timestamp,
)


def _write_envelope(path: Path, *, key: dict, fetched_at: str, payload: dict | None = None) -> None:
    line = json.dumps({"fetched_at": fetched_at, "key": key, "payload": payload or {}})
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


class TestParseSignalTimestamp:
    def test_date_only_coerced_to_midnight_utc(self) -> None:
        dt = parse_signal_timestamp("2026-04-01")
        assert dt == datetime(2026, 4, 1, tzinfo=UTC)

    def test_full_iso_with_z(self) -> None:
        dt = parse_signal_timestamp("2026-04-10T08:00:00Z")
        assert dt == datetime(2026, 4, 10, 8, 0, 0, tzinfo=UTC)

    def test_full_iso_with_offset(self) -> None:
        dt = parse_signal_timestamp("2025-09-09T18:53:19-04:00")
        assert dt is not None
        assert dt.utcoffset() == timedelta(hours=-4)

    def test_none_input(self) -> None:
        assert parse_signal_timestamp(None) is None

    def test_malformed_string(self) -> None:
        assert parse_signal_timestamp("not a date") is None


class TestLoadFreshnessMap:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_freshness_map(tmp_path / "nope.jsonl", ("bioguide_id", "congress")) == {}

    def test_empty_file_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "members.jsonl"
        path.touch()
        assert load_freshness_map(path, ("bioguide_id", "congress")) == {}

    def test_one_envelope_one_entry(self, tmp_path: Path) -> None:
        path = tmp_path / "members.jsonl"
        _write_envelope(
            path,
            key={"bioguide_id": "A000001", "congress": 119},
            fetched_at="2026-04-01T00:00:00Z",
        )
        result = load_freshness_map(path, ("bioguide_id", "congress"))
        assert result == {("A000001", 119): datetime(2026, 4, 1, tzinfo=UTC)}

    def test_duplicate_keys_keeps_max(self, tmp_path: Path) -> None:
        path = tmp_path / "members.jsonl"
        _write_envelope(
            path,
            key={"bioguide_id": "A000001", "congress": 119},
            fetched_at="2026-04-01T00:00:00Z",
        )
        _write_envelope(
            path,
            key={"bioguide_id": "A000001", "congress": 119},
            fetched_at="2026-05-15T00:00:00Z",
        )
        result = load_freshness_map(path, ("bioguide_id", "congress"))
        assert result[("A000001", 119)] == datetime(2026, 5, 15, tzinfo=UTC)

    def test_malformed_line_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "members.jsonl"
        _write_envelope(
            path,
            key={"bioguide_id": "A000001", "congress": 119},
            fetched_at="2026-04-01T00:00:00Z",
        )
        with path.open("a", encoding="utf-8") as fh:
            fh.write("this is not json\n")
        _write_envelope(
            path,
            key={"bioguide_id": "B000002", "congress": 119},
            fetched_at="2026-05-01T00:00:00Z",
        )
        result = load_freshness_map(path, ("bioguide_id", "congress"))
        assert set(result.keys()) == {("A000001", 119), ("B000002", 119)}

    def test_naive_fetched_at_coerced_to_utc(self, tmp_path: Path) -> None:
        path = tmp_path / "members.jsonl"
        _write_envelope(
            path,
            key={"bioguide_id": "A000001", "congress": 119},
            fetched_at="2026-04-01T00:00:00",
        )
        result = load_freshness_map(path, ("bioguide_id", "congress"))
        dt = result[("A000001", 119)]
        assert dt.tzinfo is not None
        assert dt == datetime(2026, 4, 1, tzinfo=UTC)


class TestIsStubUnchanged:
    def _fresh(self) -> dict:
        return {("A", 119): datetime(2026, 4, 1, tzinfo=UTC)}

    def test_key_absent_returns_false(self) -> None:
        assert (
            is_stub_unchanged(freshness={}, key=("A", 119), signal=datetime(2026, 4, 1, tzinfo=UTC))
            is False
        )

    def test_signal_none_returns_false(self) -> None:
        assert is_stub_unchanged(freshness=self._fresh(), key=("A", 119), signal=None) is False

    def test_signal_equal_returns_true(self) -> None:
        assert (
            is_stub_unchanged(
                freshness=self._fresh(), key=("A", 119), signal=datetime(2026, 4, 1, tzinfo=UTC)
            )
            is True
        )

    def test_signal_older_returns_true(self) -> None:
        assert (
            is_stub_unchanged(
                freshness=self._fresh(), key=("A", 119), signal=datetime(2026, 1, 1, tzinfo=UTC)
            )
            is True
        )

    def test_signal_newer_returns_false(self) -> None:
        assert (
            is_stub_unchanged(
                freshness=self._fresh(), key=("A", 119), signal=datetime(2026, 5, 1, tzinfo=UTC)
            )
            is False
        )

    def test_offset_aware_signal_compares_in_utc(self) -> None:
        # 2026-04-01T05:00 in -04:00 == 09:00 UTC == AFTER 2026-04-01T00:00 UTC
        signal = datetime(2026, 4, 1, 5, 0, 0, tzinfo=timezone(timedelta(hours=-4)))
        assert is_stub_unchanged(freshness=self._fresh(), key=("A", 119), signal=signal) is False


class TestLoadBillSignalMap:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_bill_signal_map(tmp_path / "bills.jsonl") == {}

    def test_picks_max_of_update_dates(self, tmp_path: Path) -> None:
        path = tmp_path / "bills.jsonl"
        _write_envelope(
            path,
            key={"congress": 119, "bill_type": "hr", "bill_number": 1},
            fetched_at="2026-04-01T00:00:00Z",
            payload={
                "updateDate": "2026-04-01",
                "updateDateIncludingText": "2026-04-10T08:00:00Z",
            },
        )
        result = load_bill_signal_map(path)
        assert result[(119, "hr", 1)] == datetime(2026, 4, 10, 8, 0, 0, tzinfo=UTC)

    def test_unparseable_payload_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "bills.jsonl"
        _write_envelope(
            path,
            key={"congress": 119, "bill_type": "hr", "bill_number": 1},
            fetched_at="2026-04-01T00:00:00Z",
            payload={"updateDate": None, "updateDateIncludingText": None},
        )
        assert load_bill_signal_map(path) == {}

    def test_keeps_max_across_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "bills.jsonl"
        _write_envelope(
            path,
            key={"congress": 119, "bill_type": "hr", "bill_number": 1},
            fetched_at="2026-04-01T00:00:00Z",
            payload={"updateDate": "2026-04-01"},
        )
        _write_envelope(
            path,
            key={"congress": 119, "bill_type": "hr", "bill_number": 1},
            fetched_at="2026-05-15T00:00:00Z",
            payload={"updateDate": "2026-05-15"},
        )
        result = load_bill_signal_map(path)
        assert result[(119, "hr", 1)] == datetime(2026, 5, 15, tzinfo=UTC)
