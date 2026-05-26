"""Integration tests for the Bills web routes (Phase 2a)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from concord.embedding import EMBEDDING_DIM, Embedder
from concord.models import (
    Bill,
    BillAction,
    BillSubject,
    BillSummary,
    BillTitle,
    Cosponsor,
    Member,
    Term,
)
from concord.pipeline.index_bills import index as index_bills
from concord.storage.sqlite import SqliteStorage
from concord.web.app import create_app


class _StubData:
    def __init__(self, vec: list[float]) -> None:
        self.embedding = vec


class _StubResponse:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.data = [_StubData(v) for v in vectors]


class _StubEmbeddings:
    def create(self, *, model: str, input: list[str]) -> _StubResponse:
        return _StubResponse([[0.5] * EMBEDDING_DIM for _ in input])


class _StubOpenAI:
    embeddings = _StubEmbeddings()


def _bill(
    bill_id: str,
    *,
    congress: int,
    bill_type: str,
    bill_number: int,
    title: str,
    sponsor_bioguide_id: str | None,
    origin_chamber: str = "House",
    policy_area: str | None = "Energy",
    introduced_date: str | None = "2025-01-09",
    latest_action_date: str | None = "2026-03-30",
    latest_action_text: str | None = "Became Public Law.",
) -> Bill:
    return Bill(
        bill_id=bill_id,
        congress=congress,
        bill_type=bill_type,  # type: ignore[arg-type]
        bill_number=bill_number,
        origin_chamber=origin_chamber,  # type: ignore[arg-type]
        title=title,
        introduced_date=introduced_date,
        policy_area=policy_area,
        sponsor_bioguide_id=sponsor_bioguide_id,
        latest_action_date=latest_action_date,
        latest_action_text=latest_action_text,
        update_date="2026-04-01",
    )


def _seed(storage: SqliteStorage) -> None:
    # Sponsor Member so the join produces a real name on the bill page.
    storage.upsert_member(
        Member(
            bioguide_id="S001176",
            first_name="Steve",
            last_name="Scalise",
            display_name="Steve Scalise",
        ),
        [
            Term(
                bioguide_id="S001176",
                congress=119,
                chamber="house",
                state="LA",
                district=1,
                party="Republican",
                start_date="2025-01-01",
            ),
        ],
        fetched_at="2026-05-25T00:00:00+00:00",
    )

    # Three bills: 119-hr-1 sponsored by Scalise; 119-hr-22 sponsored by
    # a Member not in the table (cross-link should degrade gracefully);
    # 118-s-47 to exercise the chamber filter and a duplicate identifier
    # across Congresses (alongside 119-hr-1, both will match query "hr 1"
    # only — 's 47' will be unique).
    storage.upsert_bill(
        _bill(
            "119-hr-1",
            congress=119,
            bill_type="hr",
            bill_number=1,
            title="Lower Energy Costs Act",
            sponsor_bioguide_id="S001176",
        ),
        fetched_at="2026-05-25T00:00:00+00:00",
    )
    storage.upsert_bill(
        _bill(
            "119-hr-22",
            congress=119,
            bill_type="hr",
            bill_number=22,
            title="Safeguard American Voter Eligibility Act",
            sponsor_bioguide_id="R000614",  # not in members table
            policy_area="Government Operations and Politics",
            latest_action_date="2026-04-10",
        ),
        fetched_at="2026-05-25T00:00:00+00:00",
    )
    storage.upsert_bill(
        _bill(
            "118-s-47",
            congress=118,
            bill_type="s",
            bill_number=47,
            title="An Unrelated Senate Bill",
            sponsor_bioguide_id=None,
            origin_chamber="Senate",
            policy_area="Health",
            latest_action_date="2024-09-01",
        ),
        fetched_at="2026-05-25T00:00:00+00:00",
    )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    db_path = tmp_path / "test.db"
    storage = SqliteStorage(db_path)
    _seed(storage)
    storage.close()
    index_bills(db_path=db_path)
    app = create_app(db_path, embedder=Embedder(_StubOpenAI()))
    return TestClient(app, raise_server_exceptions=False)


class TestBillsIndex:
    def test_lists_all(self, client: TestClient) -> None:
        resp = client.get("/bills")
        assert resp.status_code == 200
        body = resp.text
        assert "Lower Energy Costs Act" in body
        assert "Safeguard American Voter Eligibility Act" in body
        assert "An Unrelated Senate Bill" in body

    def test_chamber_filter(self, client: TestClient) -> None:
        resp = client.get("/bills?chamber=Senate")
        assert resp.status_code == 200
        body = resp.text
        assert "An Unrelated Senate Bill" in body
        assert "Lower Energy Costs Act" not in body

    def test_congress_filter(self, client: TestClient) -> None:
        resp = client.get("/bills?congress=118")
        assert resp.status_code == 200
        body = resp.text
        assert "An Unrelated Senate Bill" in body
        assert "Lower Energy Costs Act" not in body

    def test_policy_area_filter(self, client: TestClient) -> None:
        resp = client.get("/bills?policy_area=Energy")
        assert resp.status_code == 200
        body = resp.text
        assert "Lower Energy Costs Act" in body
        assert "Safeguard American Voter Eligibility Act" not in body

    def test_sponsor_filter(self, client: TestClient) -> None:
        resp = client.get("/bills?sponsor=S001176")
        assert resp.status_code == 200
        body = resp.text
        assert "Lower Energy Costs Act" in body
        assert "Safeguard American Voter Eligibility Act" not in body


class TestBillProfile:
    def test_renders_known_bill(self, client: TestClient) -> None:
        resp = client.get("/bills/119/hr/1")
        assert resp.status_code == 200
        body = resp.text
        assert "Lower Energy Costs Act" in body
        # Sponsor link present (Member in table).
        assert "Steve Scalise" in body
        assert "/members/S001176" in body
        # Tier-2 empty-states are visible — this bill has no enrichment loaded.
        assert "Cosponsors not yet fetched" in body
        assert "Action history not yet fetched" in body
        assert "Subjects not yet fetched" in body
        assert "Titles not yet fetched" in body
        assert "Summaries not yet fetched" in body
        # Phase 3/4/5 placeholders.
        assert "Vote history (Phase 3)" in body
        assert "Committee path (Phase 4)" in body
        assert "Search within this bill (Phase 5)" in body

    def test_unknown_bill_number_404(self, client: TestClient) -> None:
        resp = client.get("/bills/119/hr/9999")
        assert resp.status_code == 404

    def test_invalid_bill_type_404(self, client: TestClient) -> None:
        resp = client.get("/bills/119/xyz/1")
        assert resp.status_code == 404

    def test_sponsor_not_in_members_table_degrades(self, client: TestClient) -> None:
        """The Member isn't indexed; show the bioguide id with a hint, no link."""
        resp = client.get("/bills/119/hr/22")
        assert resp.status_code == 200
        body = resp.text
        assert "R000614" in body
        assert "(not indexed)" in body

    def test_enriched_bill_renders_sections(self, client: TestClient) -> None:
        db_path = client.app.state.db_path
        with SqliteStorage(db_path, load_vec=False) as storage:
            storage.replace_bill_cosponsors(
                "119-hr-1",
                [
                    Cosponsor(
                        bioguide_id="S001176",
                        sponsorship_date="2025-01-09",
                        is_original_cosponsor=True,
                    ),
                    Cosponsor(
                        bioguide_id="Z999999",
                        sponsorship_date="2025-02-01",
                        sponsorship_withdrawn_date="2025-03-15",
                    ),
                ],
                fetched_at="2026-05-26T00:00:00Z",
            )
            storage.replace_bill_actions(
                "119-hr-1",
                [
                    BillAction(action_date="2026-03-30", action_text="Became Public Law"),
                    BillAction(action_date="2025-01-09", action_text="Introduced in House"),
                ],
                fetched_at="2026-05-26T00:00:00Z",
            )
            storage.replace_bill_subjects(
                "119-hr-1",
                [BillSubject(name="Energy"), BillSubject(name="Pipelines")],
                fetched_at="2026-05-26T00:00:00Z",
            )
            storage.replace_bill_titles(
                "119-hr-1",
                [
                    BillTitle(
                        title_type="Short Title(s) as Introduced",
                        title_text="Lower Energy Costs Act",
                    ),
                ],
                fetched_at="2026-05-26T00:00:00Z",
            )
            storage.replace_bill_summaries(
                "119-hr-1",
                [
                    BillSummary(
                        version_code="00",
                        action_date="2025-01-09",
                        action_desc="Introduced",
                        summary_text="<p>Intro summary</p>",
                    ),
                ],
                fetched_at="2026-05-26T00:00:00Z",
            )

        resp = client.get("/bills/119/hr/1")
        assert resp.status_code == 200
        body = resp.text
        # Empty states are gone for this bill.
        assert "Cosponsors not yet fetched" not in body
        # Cosponsor: indexed Member shows up by name.
        assert "Steve Scalise" in body
        # Withdrawn cosponsor renders as <del> with a (withdrawn ...) marker.
        assert "(withdrawn 2025-03-15)" in body
        assert "<del" in body
        # Action history present with reverse-chrono date.
        assert "Became Public Law" in body
        # Subjects rendered as chips.
        assert "Pipelines" in body
        # Summary's HTML body is rendered (safe).
        assert "Intro summary" in body


class TestFederatedBillsSearch:
    def test_search_includes_bills_section(self, client: TestClient) -> None:
        resp = client.get("/search?q=energy")
        assert resp.status_code == 200
        body = resp.text
        assert "Bills" in body
        assert "Lower Energy Costs Act" in body

    def test_bills_off_suppresses_section(self, client: TestClient) -> None:
        resp = client.get("/search?q=energy&members=on&proceedings=on&bills=")
        assert resp.status_code == 200
        body = resp.text
        assert "Lower Energy Costs Act" not in body

    def test_bare_identifier_redirects_when_unique(self, client: TestClient) -> None:
        # Only 118-s-47 exists; bare "S 47" should redirect there.
        resp = client.get("/search?q=S+47", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/bills/118/s/47"

    def test_bare_identifier_with_period_redirects(self, client: TestClient) -> None:
        resp = client.get("/search?q=s.47", follow_redirects=False)
        assert resp.status_code == 307

    def test_bare_identifier_with_only_one_congress_match(self, client: TestClient) -> None:
        # HR 1 only exists in 119 in our seed; should redirect.
        resp = client.get("/search?q=HR+1", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/bills/119/hr/1"


class TestMemberProfileSponsoredCrossLink:
    def test_sponsored_section_lists_bill(self, client: TestClient) -> None:
        resp = client.get("/members/S001176")
        assert resp.status_code == 200
        body = resp.text
        assert "Sponsored bills" in body
        assert "Lower Energy Costs Act" in body
        # Cosponsored section is now live; with no enrichment in the seed,
        # it shows the empty-state CLI hint.
        assert "Cosponsored bills" in body
        assert "No cosponsored bills indexed" in body


class TestMemberProfileCosponsoredEnriched:
    def test_cosponsored_lists_bill_when_enriched(self, client: TestClient, tmp_path: Path) -> None:
        # Reach into the seeded DB and add a cosponsor edge.
        db_path = client.app.state.db_path
        with SqliteStorage(db_path, load_vec=False) as storage:
            storage.replace_bill_cosponsors(
                "119-hr-22",
                [
                    Cosponsor(
                        bioguide_id="S001176",
                        sponsorship_date="2025-02-04",
                        is_original_cosponsor=False,
                    )
                ],
                fetched_at="2026-05-26T00:00:00Z",
            )

        resp = client.get("/members/S001176")
        assert resp.status_code == 200
        body = resp.text
        # Scalise now appears in the Cosponsored bills section as 119-hr-22.
        assert "Safeguard American Voter Eligibility Act" in body
        # The "No cosponsored" empty-state should be gone for this member.
        assert "No cosponsored bills indexed" not in body
