"""Tests for Bill and BillSnapshot models (Phase 2a)."""

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
    parse_bill_action,
    parse_bill_subject,
    parse_bill_summary,
    parse_bill_title,
    parse_cosponsor,
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


class TestParseCosponsor:
    def test_parses_cosponsor_fixture(self, fixtures_dir: Path) -> None:
        payload = json.loads((fixtures_dir / "api/bills/cosponsors_119_hr_22.json").read_text())
        rows = [parse_cosponsor(p) for p in payload["cosponsors"]]
        assert all(r is not None for r in rows)
        first = rows[0]
        assert first is not None
        assert first.bioguide_id == "B001302"
        assert first.is_original_cosponsor is True
        assert first.sponsorship_withdrawn_date is None

    def test_parses_withdrawn_row(self, fixtures_dir: Path) -> None:
        payload = json.loads(
            (fixtures_dir / "api/bills/cosponsors_119_hr_22_withdrawn.json").read_text()
        )
        rows = [parse_cosponsor(p) for p in payload["cosponsors"]]
        withdrawn = next(r for r in rows if r is not None and r.sponsorship_withdrawn_date)
        assert withdrawn.sponsorship_withdrawn_date == "2025-04-02"
        assert withdrawn.is_original_cosponsor is False

    def test_returns_none_for_row_without_bioguide(self) -> None:
        assert parse_cosponsor({"sponsorshipDate": "2025-01-09"}) is None


class TestParseBillAction:
    def test_parses_action_fixture(self, fixtures_dir: Path) -> None:
        payload = json.loads((fixtures_dir / "api/bills/actions_119_hr_1.json").read_text())
        rows = [parse_bill_action(p) for p in payload["actions"]]
        assert all(r is not None for r in rows)
        first = rows[0]
        assert first is not None
        assert first.action_date == "2026-03-30"
        assert first.action_text == "Became Public Law No: 119-1."
        assert first.source_system == "Library of Congress"

    def test_returns_none_for_action_without_date(self) -> None:
        assert parse_bill_action({"text": "Some action"}) is None


class TestParseBillSubject:
    def test_parses_subjects_fixture(self, fixtures_dir: Path) -> None:
        payload = json.loads((fixtures_dir / "api/bills/subjects_119_hr_1.json").read_text())
        rows = [parse_bill_subject(p) for p in payload["subjects"]["legislativeSubjects"]]
        assert all(r is not None for r in rows)
        names = [r.name for r in rows if r is not None]
        assert "Energy" in names
        assert "Pipelines" in names

    def test_returns_none_for_empty_name(self) -> None:
        assert parse_bill_subject({"name": ""}) is None


class TestParseBillTitle:
    def test_parses_titles_fixture(self, fixtures_dir: Path) -> None:
        payload = json.loads((fixtures_dir / "api/bills/titles_119_hr_1.json").read_text())
        rows = [parse_bill_title(p) for p in payload["titles"]]
        assert all(r is not None for r in rows)
        short = next(r for r in rows if r is not None and r.title_type.startswith("Short Title"))
        assert short.title_text == "Lower Energy Costs Act"


class TestParseBillSummary:
    def test_parses_summaries_fixture(self, fixtures_dir: Path) -> None:
        payload = json.loads((fixtures_dir / "api/bills/summaries_119_hr_1.json").read_text())
        rows = [parse_bill_summary(p) for p in payload["summaries"]]
        assert all(r is not None for r in rows)
        first = rows[0]
        assert first is not None
        assert first.version_code == "00"
        assert "<p>" in first.summary_text


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
