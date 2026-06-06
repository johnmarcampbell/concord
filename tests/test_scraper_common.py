"""Unit tests for :mod:`concord.scraper._common` (envelope read/write helpers)."""

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from concord.models import Snapshot
from concord.scraper._common import (
    append_snapshot,
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


class TestAppendSnapshot:
    """The ADR 0006 envelope *write* helper (ADR 0018 Rule 5).

    Serializes through :class:`Snapshot`, the same model the loaders parse
    with, so these tests assert the write side stays compatible with that
    read side and honours the payload-verbatim contract (ADR 0018 Rule 1).
    """

    def test_round_trips_through_snapshot_model(self, tmp_path: Path) -> None:
        path = tmp_path / "out.jsonl"
        fetched_at = datetime(2026, 5, 25, 14, 2, 11, tzinfo=UTC)
        key: dict[str, str | int] = {"bioguide_id": "O000172", "congress": 119}
        payload = {"bioguideId": "O000172", "name": "Last, First"}
        with path.open("a", encoding="utf-8") as fh:
            append_snapshot(fh, fetched_at=fetched_at, key=key, payload=payload)

        lines = path.read_text().splitlines()
        assert len(lines) == 1
        snap = Snapshot[dict[str, Any]].model_validate_json(lines[0])
        assert snap.fetched_at == fetched_at
        assert snap.key == key
        assert snap.payload == payload

    def test_non_ascii_and_nested_payload_preserved(self, tmp_path: Path) -> None:
        # ADR 0018 Rule 1: the payload is written verbatim, including non-ASCII
        # (unescaped) and arbitrarily nested structures.
        path = tmp_path / "out.jsonl"
        payload = {
            "title": "Café résumé — naïve façade",
            "nested": {"items": [1, 2, {"x": "ü"}], "flag": True},
        }
        with path.open("a", encoding="utf-8") as fh:
            append_snapshot(
                fh,
                fetched_at=datetime(2026, 5, 25, tzinfo=UTC),
                key={"k": 1},
                payload=payload,
            )

        line = path.read_text().splitlines()[0]
        assert "Café résumé" in line  # written unescaped, not \uXXXX
        assert json.loads(line)["payload"] == payload

    def test_str_payload_round_trips(self, tmp_path: Path) -> None:
        # Senate case: the payload is the raw decoded XML string, not a dict.
        path = tmp_path / "out.jsonl"
        payload = "<rollcall>café</rollcall>"
        with path.open("a", encoding="utf-8") as fh:
            append_snapshot(
                fh,
                fetched_at=datetime(2026, 5, 25, tzinfo=UTC),
                key={"source": "senators_cfm"},
                payload=payload,
            )

        snap = Snapshot[str].model_validate_json(path.read_text().splitlines()[0])
        assert snap.payload == payload

    def test_appends_one_line_per_call(self, tmp_path: Path) -> None:
        path = tmp_path / "out.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            append_snapshot(
                fh, fetched_at=datetime(2026, 5, 25, tzinfo=UTC), key={"k": 1}, payload={}
            )
            append_snapshot(
                fh, fetched_at=datetime(2026, 5, 26, tzinfo=UTC), key={"k": 2}, payload={}
            )
        assert len(path.read_text().splitlines()) == 2

    def test_malformed_key_raises_validation_error(self, tmp_path: Path) -> None:
        # ADR 0018 Rule 5 fail-fast: a non-(str | int) key value raises at write
        # time rather than serializing a malformed envelope silently. This never
        # fires on current scraper paths (the skip rule guarantees scalar keys).
        path = tmp_path / "out.jsonl"
        bad_key: dict[str, Any] = {"bad": [1, 2]}
        with path.open("a", encoding="utf-8") as fh, pytest.raises(ValidationError):
            append_snapshot(
                fh,
                fetched_at=datetime(2026, 5, 25, tzinfo=UTC),
                key=bad_key,
                payload={},
            )
        assert path.read_text() == ""  # nothing was written
