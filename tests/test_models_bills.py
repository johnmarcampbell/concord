"""Tests for BillDetail and the tier-2 child models (Phase 2a)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from concord.models import (
    BillAction,
    BillCosponsor,
    BillDetail,
    BillSubject,
    BillSummary,
    BillTitle,
    Snapshot,
    bill_id_from_components,
)

from ._snapshots import wrap_snapshot

FIXED_FETCHED_AT = datetime(2026, 5, 25, 14, 2, 11, tzinfo=UTC)


class TestBillIdFromComponents:
    def test_round_trip(self) -> None:
        assert bill_id_from_components(119, "hr", 1234) == "119-hr-1234"

    def test_lowercases_bill_type(self) -> None:
        assert bill_id_from_components(118, "HRES", 5) == "118-hres-5"


class TestBillDetailModel:
    def test_factory_lowercases_bill_type(self) -> None:
        # The API delivers ``type`` uppercase ("HR"); the factory
        # canonicalizes to lowercase before constructing the model.
        bill = BillDetail.from_congress_api(
            {
                "congress": 119,
                "type": "HR",
                "number": "1",
                "originChamber": "House",
                "title": "Lower Energy Costs Act",
                "updateDate": "2026-04-01",
            }
        )
        assert bill.bill_type == "hr"

    def test_rejects_unknown_bill_type(self) -> None:
        with pytest.raises(ValidationError):
            BillDetail(
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
            BillDetail(
                bill_id="119-hr-1",
                congress=119,
                bill_type="hr",
                bill_number=1,
                origin_chamber="House of Cards",  # type: ignore[arg-type]
                title="x",
                update_date="2026-04-01",
            )


class TestBillDetailFromCongressApi:
    def test_parses_hr_1_fixture(self, fixtures_dir: Path) -> None:
        payload = json.loads((fixtures_dir / "api/bills/detail_119_hr_1.json").read_text())
        bill = BillDetail.from_congress_api(payload["bill"])
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
        bill = BillDetail.from_congress_api(payload["bill"])
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
        bill = BillDetail.from_congress_api(payload)
        assert bill.sponsor_bioguide_id is None
        assert bill.policy_area is None
        assert bill.latest_action_date is None


class TestBillCosponsorFromCongressApi:
    def test_parses_cosponsor_fixture(self, fixtures_dir: Path) -> None:
        payload = json.loads((fixtures_dir / "api/bills/cosponsors_119_hr_22.json").read_text())
        rows = [BillCosponsor.from_congress_api(p) for p in payload["cosponsors"]]
        first = rows[0]
        assert first.bioguide_id == "B001302"
        assert first.is_original_cosponsor is True
        assert first.sponsorship_withdrawn_date is None

    def test_parses_withdrawn_row(self, fixtures_dir: Path) -> None:
        payload = json.loads(
            (fixtures_dir / "api/bills/cosponsors_119_hr_22_withdrawn.json").read_text()
        )
        rows = [BillCosponsor.from_congress_api(p) for p in payload["cosponsors"]]
        withdrawn = next(r for r in rows if r.sponsorship_withdrawn_date)
        assert withdrawn.sponsorship_withdrawn_date == "2025-04-02"
        assert withdrawn.is_original_cosponsor is False

    def test_raises_for_row_without_bioguide(self) -> None:
        with pytest.raises(ValueError, match="bioguideId"):
            BillCosponsor.from_congress_api({"sponsorshipDate": "2025-01-09"})


class TestBillActionFromCongressApi:
    def test_parses_action_fixture(self, fixtures_dir: Path) -> None:
        payload = json.loads((fixtures_dir / "api/bills/actions_119_hr_1.json").read_text())
        rows = [BillAction.from_congress_api(p) for p in payload["actions"]]
        first = rows[0]
        assert first.action_date == "2026-03-30"
        assert first.action_text == "Became Public Law No: 119-1."
        assert first.source_system == "Library of Congress"

    def test_raises_for_action_without_date(self) -> None:
        with pytest.raises(ValueError, match="actionDate"):
            BillAction.from_congress_api({"text": "Some action"})


class TestBillSubjectFromCongressApi:
    def test_parses_subjects_fixture(self, fixtures_dir: Path) -> None:
        payload = json.loads((fixtures_dir / "api/bills/subjects_119_hr_1.json").read_text())
        rows = [
            BillSubject.from_congress_api(p) for p in payload["subjects"]["legislativeSubjects"]
        ]
        names = [r.name for r in rows]
        assert "Energy" in names
        assert "Pipelines" in names

    def test_raises_for_empty_name(self) -> None:
        with pytest.raises(ValueError, match="name"):
            BillSubject.from_congress_api({"name": ""})


class TestBillTitleFromCongressApi:
    def test_parses_titles_fixture(self, fixtures_dir: Path) -> None:
        payload = json.loads((fixtures_dir / "api/bills/titles_119_hr_1.json").read_text())
        rows = [BillTitle.from_congress_api(p) for p in payload["titles"]]
        short = next(r for r in rows if r.title_type.startswith("Short Title"))
        assert short.title_text == "Lower Energy Costs Act"


class TestBillSummaryFromCongressApi:
    def test_parses_summaries_fixture(self, fixtures_dir: Path) -> None:
        payload = json.loads((fixtures_dir / "api/bills/summaries_119_hr_1.json").read_text())
        rows = [BillSummary.from_congress_api(p) for p in payload["summaries"]]
        first = rows[0]
        assert first.version_code == "00"
        assert "<p>" in first.summary_text


class TestSnapshotEnvelope:
    def test_envelope_round_trip(self) -> None:
        env = wrap_snapshot(
            {"congress": 119, "type": "HR", "number": "1"},
            fetched_at=FIXED_FETCHED_AT,
            key={"congress": 119, "bill_type": "hr", "bill_number": 1},
        )
        # Round-trip through JSON to mirror how the loader reads it.
        raw = json.dumps(env)
        snap = Snapshot[dict].model_validate_json(raw)
        assert snap.key == {"congress": 119, "bill_type": "hr", "bill_number": 1}
        assert snap.payload["number"] == "1"
        assert snap.fetched_at == FIXED_FETCHED_AT
