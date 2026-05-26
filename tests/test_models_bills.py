"""Tests for Bill and BillSnapshot models (Phase 2a)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from concord.models import (
    Bill,
    BillSnapshot,
    bill_id_from_components,
    parse_bill,
)

from ._snapshots import wrap_snapshot

FIXED_FETCHED_AT = datetime(2026, 5, 25, 14, 2, 11, tzinfo=UTC)


class TestBillIdFromComponents:
    def test_round_trip(self) -> None:
        assert bill_id_from_components(119, "hr", 1234) == "119-hr-1234"

    def test_lowercases_bill_type(self) -> None:
        assert bill_id_from_components(118, "HRES", 5) == "118-hres-5"


class TestBillModel:
    def test_validator_lowercases_bill_type(self) -> None:
        bill = Bill(
            bill_id="119-hr-1",
            congress=119,
            bill_type="HR",  # type: ignore[arg-type]
            bill_number=1,
            origin_chamber="House",
            title="Lower Energy Costs Act",
            update_date="2026-04-01",
        )
        assert bill.bill_type == "hr"

    def test_rejects_unknown_bill_type(self) -> None:
        with pytest.raises(ValidationError):
            Bill(
                bill_id="119-xxx-1",
                congress=119,
                bill_type="xxx",  # type: ignore[arg-type]
                bill_number=1,
                origin_chamber="House",
                title="x",
                update_date="2026-04-01",
            )

    def test_rejects_unknown_origin_chamber(self) -> None:
        with pytest.raises(ValidationError):
            Bill(
                bill_id="119-hr-1",
                congress=119,
                bill_type="hr",
                bill_number=1,
                origin_chamber="House of Cards",  # type: ignore[arg-type]
                title="x",
                update_date="2026-04-01",
            )


class TestParseBill:
    def test_parses_hr_1_fixture(self, fixtures_dir: Path) -> None:
        payload = json.loads((fixtures_dir / "api/bills/detail_119_hr_1.json").read_text())
        bill = parse_bill(payload["bill"])
        assert bill.bill_id == "119-hr-1"
        assert bill.congress == 119
        assert bill.bill_type == "hr"
        assert bill.bill_number == 1
        assert bill.origin_chamber == "House"
        assert bill.title == "Lower Energy Costs Act"
        assert bill.introduced_date == "2025-01-09"
        assert bill.policy_area == "Energy"
        assert bill.sponsor_bioguide_id == "S001176"
        assert bill.latest_action_date == "2026-03-30"
        assert bill.latest_action_text == "Became Public Law No: 119-1."
        # update_date prefers updateDateIncludingText when present.
        assert bill.update_date == "2026-04-01T12:34:56Z"

    def test_parses_hr_22_fixture(self, fixtures_dir: Path) -> None:
        payload = json.loads((fixtures_dir / "api/bills/detail_119_hr_22.json").read_text())
        bill = parse_bill(payload["bill"])
        assert bill.bill_id == "119-hr-22"
        assert bill.sponsor_bioguide_id == "R000614"
        assert bill.policy_area == "Government Operations and Politics"

    def test_missing_sponsor_is_ok(self) -> None:
        payload = {
            "congress": 119,
            "type": "HR",
            "number": "9999",
            "originChamber": "House",
            "title": "Sponsorless Bill Act",
            "updateDate": "2026-04-01",
        }
        bill = parse_bill(payload)
        assert bill.sponsor_bioguide_id is None
        assert bill.policy_area is None
        assert bill.latest_action_date is None


class TestBillSnapshot:
    def test_envelope_round_trip(self) -> None:
        env = wrap_snapshot(
            {"congress": 119, "type": "HR", "number": "1"},
            fetched_at=FIXED_FETCHED_AT,
            key={"congress": 119, "bill_type": "hr", "bill_number": 1},
        )
        # Round-trip through JSON to mirror how the loader reads it.
        raw = json.dumps(env)
        snap = BillSnapshot.model_validate(json.loads(raw))
        assert snap.key == {"congress": 119, "bill_type": "hr", "bill_number": 1}
        assert snap.payload["number"] == "1"
        assert snap.fetched_at == FIXED_FETCHED_AT
