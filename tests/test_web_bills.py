"""Integration tests for the Bills web routes (Phase 2a)."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from concord.embedding import EMBEDDING_DIM, Embedder
from concord.models import (
    BillAction,
    BillCosponsor,
    BillDetail,
    BillSubject,
    BillSummary,
    BillTitle,
    Member,
    Term,
)
from concord.pipeline.index_bills import index as index_bills
from concord.scraper.bills import (
    BILL_ENRICHMENT_SECTIONS,
    EnrichStats,
    enrichment_jsonl_name,
)
from concord.storage.sqlite import SqliteStorage
from concord.web import app as web_app
from concord.web.app import create_app, humanize_age


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
) -> BillDetail:
    return BillDetail(
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
                end_date="2027-01-03",
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

    def test_top_bills_section_on_landing(self, client: TestClient) -> None:
        # The seed includes 119-hr-1, which is in CURATED_TOP_BILLS as the
        # "One Big Beautiful Bill Act" — the curated label/blurb come from
        # the constant, not the DB row's title.
        resp = client.get("/bills")
        assert resp.status_code == 200
        body = resp.text
        assert "Top bills" in body
        assert "One Big Beautiful Bill Act" in body
        # Curated entries whose underlying bill isn't seeded are skipped
        # rather than rendered as broken cards.
        assert "CHIPS and Science Act" not in body

    def test_top_bills_hidden_when_filtered(self, client: TestClient) -> None:
        resp = client.get("/bills?chamber=Senate")
        assert resp.status_code == 200
        assert "Top bills" not in resp.text


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
        # Vote history is live (Phase 3a) but empty when no votes loaded.
        assert "Vote history" in body
        assert "No votes recorded for this bill yet" in body

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
                    BillCosponsor(
                        bioguide_id="S001176",
                        sponsorship_date="2025-01-09",
                        is_original_cosponsor=True,
                    ),
                    BillCosponsor(
                        bioguide_id="Z999999",
                        sponsorship_date="2025-02-01",
                        sponsorship_withdrawn_date="2025-03-15",
                        is_original_cosponsor=False,
                    ),
                ],
                fetched_at="2026-05-26T00:00:00Z",
            )
            storage.replace_bill_actions(
                "119-hr-1",
                [
                    BillAction(
                        action_date="2026-03-30",
                        action_text="Became Public Law",
                        action_code="E40000",
                        source_system="Library of Congress",
                    ),
                    BillAction(
                        action_date="2025-01-09",
                        action_text="Introduced in House",
                        action_code="H10000",
                        source_system="House",
                    ),
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
                    BillCosponsor(
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


class TestHumanizeAge:
    """The Jinja filter that renders ISO timestamps as 'N units ago'."""

    _NOW = datetime(2026, 5, 25, 14, 0, 0, tzinfo=UTC)

    def test_none_returns_empty(self) -> None:
        assert humanize_age(None) == ""

    def test_empty_string_returns_empty(self) -> None:
        assert humanize_age("") == ""

    def test_unparseable_returns_empty(self) -> None:
        assert humanize_age("not-a-timestamp") == ""

    def test_just_now_under_30s(self) -> None:
        recent = "2026-05-25T13:59:45+00:00"
        assert humanize_age(recent, now=self._NOW) == "just now"

    def test_seconds_ago(self) -> None:
        assert humanize_age("2026-05-25T13:59:15+00:00", now=self._NOW) == "45 seconds ago"

    def test_minutes_ago_singular(self) -> None:
        assert humanize_age("2026-05-25T13:59:00+00:00", now=self._NOW) == "1 minute ago"

    def test_minutes_ago_plural(self) -> None:
        assert humanize_age("2026-05-25T13:55:00+00:00", now=self._NOW) == "5 minutes ago"

    def test_hours_ago(self) -> None:
        assert humanize_age("2026-05-25T11:00:00+00:00", now=self._NOW) == "3 hours ago"

    def test_days_ago(self) -> None:
        assert humanize_age("2026-05-22T14:00:00+00:00", now=self._NOW) == "3 days ago"

    def test_in_the_future(self) -> None:
        assert humanize_age("2026-05-26T14:00:00+00:00", now=self._NOW) == "in the future"

    def test_naive_timestamp_assumed_utc(self) -> None:
        assert humanize_age("2026-05-25T13:55:00", now=self._NOW) == "5 minutes ago"

    def test_renders_in_bill_profile(self, client: TestClient) -> None:
        """End-to-end: the filter renders something other than the raw ISO."""
        db_path = client.app.state.db_path
        with SqliteStorage(db_path, load_vec=False) as storage:
            storage.replace_bill_cosponsors(
                "119-hr-1",
                [
                    BillCosponsor(
                        bioguide_id="A000001",
                        sponsorship_date="2020-01-01",
                        is_original_cosponsor=True,
                    )
                ],
                fetched_at="2020-01-01T00:00:00+00:00",
            )
        resp = client.get("/bills/119/hr/1")
        assert resp.status_code == 200
        # Raw ISO must not appear in the body.
        assert "2020-01-01T00:00:00+00:00" not in resp.text
        # "Fetched ... ago" should appear (years old, so "year(s) ago").
        assert "year" in resp.text
        assert "ago" in resp.text


# -----------------------------------------------------------------------------
# Web-initiated enrichment (ADR 0016)
# -----------------------------------------------------------------------------


@pytest.fixture
def enrichment_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    """Same seeded DB as ``client``, but with the two enrichment env vars set."""
    monkeypatch.setenv("CONGRESS_API_KEY", "test-key")
    monkeypatch.setenv("CONCORD_ENABLE_WEB_ENRICHMENT", "1")
    db_path = tmp_path / "test.db"
    storage = SqliteStorage(db_path)
    _seed(storage)
    storage.close()
    index_bills(db_path=db_path)
    app = create_app(db_path, embedder=Embedder(_StubOpenAI()), storage_dir=tmp_path)
    return TestClient(app, raise_server_exceptions=False)


class TestEnrichmentGating:
    """Both env vars must be present for the routes to be registered."""

    def test_disabled_by_default(self, client: TestClient) -> None:
        assert client.app.state.enrichment_enabled is False

    def test_enabled_with_both_env_vars(self, enrichment_client: TestClient) -> None:
        assert enrichment_client.app.state.enrichment_enabled is True

    def test_missing_api_key_disables(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("CONGRESS_API_KEY", raising=False)
        monkeypatch.setenv("CONCORD_ENABLE_WEB_ENRICHMENT", "1")
        db_path = tmp_path / "test.db"
        SqliteStorage(db_path).close()
        app = create_app(db_path, embedder=Embedder(_StubOpenAI()), storage_dir=tmp_path)
        assert app.state.enrichment_enabled is False

    def test_missing_flag_disables(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("CONGRESS_API_KEY", "test-key")
        monkeypatch.delenv("CONCORD_ENABLE_WEB_ENRICHMENT", raising=False)
        db_path = tmp_path / "test.db"
        SqliteStorage(db_path).close()
        app = create_app(db_path, embedder=Embedder(_StubOpenAI()), storage_dir=tmp_path)
        assert app.state.enrichment_enabled is False

    def test_unrecognized_flag_disables(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CONGRESS_API_KEY", "test-key")
        monkeypatch.setenv("CONCORD_ENABLE_WEB_ENRICHMENT", "banana")
        db_path = tmp_path / "test.db"
        SqliteStorage(db_path).close()
        app = create_app(db_path, embedder=Embedder(_StubOpenAI()), storage_dir=tmp_path)
        assert app.state.enrichment_enabled is False

    def test_routes_404_when_disabled(self, client: TestClient) -> None:
        # GET enrichment-status returns 405 because the route isn't registered;
        # FastAPI returns 405 for unknown methods on a known path prefix, 404
        # otherwise. The bill path is known; the suffix isn't.
        resp = client.post("/bills/119/hr/1/enrichment")
        assert resp.status_code == 404
        resp = client.get("/bills/119/hr/1/enrichment-status")
        assert resp.status_code == 404


class TestEnrichmentProfileButton:
    def test_button_renders_when_enabled_and_missing_enrichment(
        self, enrichment_client: TestClient
    ) -> None:
        resp = enrichment_client.get("/bills/119/hr/1")
        assert resp.status_code == 200
        body = resp.text
        assert "Request enrichment" in body
        # The five per-section CLI placeholders also remain — they're the
        # fallback for the non-enrichment-enabled deployment.
        assert "Cosponsors not yet fetched" in body

    def test_button_absent_when_disabled(self, client: TestClient) -> None:
        resp = client.get("/bills/119/hr/1")
        assert resp.status_code == 200
        assert "Request enrichment" not in resp.text

    def test_button_absent_when_fully_enriched(self, enrichment_client: TestClient) -> None:
        db_path = enrichment_client.app.state.db_path
        stamp = "2026-05-26T00:00:00Z"
        with SqliteStorage(db_path, load_vec=False) as storage:
            storage.replace_bill_cosponsors("119-hr-1", [], fetched_at=stamp)
            storage.replace_bill_actions("119-hr-1", [], fetched_at=stamp)
            storage.replace_bill_subjects("119-hr-1", [], fetched_at=stamp)
            storage.replace_bill_titles("119-hr-1", [], fetched_at=stamp)
            storage.replace_bill_summaries("119-hr-1", [], fetched_at=stamp)
        resp = enrichment_client.get("/bills/119/hr/1")
        assert resp.status_code == 200
        assert "Request enrichment" not in resp.text

    def test_failed_banner_when_error_recorded(self, enrichment_client: TestClient) -> None:
        db_path = enrichment_client.app.state.db_path
        with SqliteStorage(db_path, load_vec=False) as storage:
            storage.set_bill_enrichment_error("119-hr-1", "rate limited by api.congress.gov")
        resp = enrichment_client.get("/bills/119/hr/1")
        assert resp.status_code == 200
        body = resp.text
        assert "Enrichment failed" in body
        assert "rate limited by api.congress.gov" in body
        assert "Try again" in body

    def test_stale_error_does_not_override_fully_enriched(
        self, enrichment_client: TestClient
    ) -> None:
        """A bill that becomes fully enriched out-of-band (e.g. CLI run) must not stay failed.

        The fetched_at columns are the source of truth; a
        ``last_enrichment_error`` left over from a prior attempt is
        annotation, not state. Profile page must show neither the
        button nor the failed banner.
        """
        db_path = enrichment_client.app.state.db_path
        stamp = "2026-05-26T00:00:00Z"
        with SqliteStorage(db_path, load_vec=False) as storage:
            storage.set_bill_enrichment_error("119-hr-1", "previous failure that should be ignored")
            storage.replace_bill_cosponsors("119-hr-1", [], fetched_at=stamp)
            storage.replace_bill_actions("119-hr-1", [], fetched_at=stamp)
            storage.replace_bill_subjects("119-hr-1", [], fetched_at=stamp)
            storage.replace_bill_titles("119-hr-1", [], fetched_at=stamp)
            storage.replace_bill_summaries("119-hr-1", [], fetched_at=stamp)
        resp = enrichment_client.get("/bills/119/hr/1")
        assert resp.status_code == 200
        body = resp.text
        assert "Enrichment failed" not in body
        assert "previous failure that should be ignored" not in body
        assert "Request enrichment" not in body
        assert "Try again" not in body


class TestEnrichmentPostRoute:
    def test_post_returns_in_flight_fragment_and_registers_bill(
        self, enrichment_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Block the background task from doing any real work — we only
        # care about the request-flow behavior here.
        monkeypatch.setattr(web_app, "_enrich_one_bill", lambda app, bill_id: None)

        resp = enrichment_client.post("/bills/119/hr/1/enrichment")
        assert resp.status_code == 200
        body = resp.text
        # The in-flight fragment includes the status-poll hx-get.
        assert "enrichment-status" in body
        assert "119" in body
        assert "hr" in body
        # The bill is recorded as in-flight; the background task we
        # patched in is a no-op, so the discard doesn't run, but on
        # the real flow it would clear the set.
        # (We don't assert the set state here because BackgroundTasks
        # runs synchronously in TestClient and the lambda is the task.)

    def test_post_404_for_invalid_bill_type(self, enrichment_client: TestClient) -> None:
        resp = enrichment_client.post("/bills/119/xyz/1/enrichment")
        assert resp.status_code == 404

    def test_post_404_for_unknown_bill_number(
        self, enrichment_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POST refuses to enqueue a bill that isn't in the local store.

        Without this gate, a hand-crafted request would pay 5 upstream
        sub-endpoint calls only for ``load_one`` to no-op on an absent
        parent row.
        """
        called: list[str] = []
        monkeypatch.setattr(
            web_app, "_enrich_one_bill", lambda app, bill_id: called.append(bill_id)
        )
        resp = enrichment_client.post("/bills/119/hr/9999/enrichment")
        assert resp.status_code == 404
        assert called == []
        assert "119-hr-9999" not in enrichment_client.app.state.enrichment_in_flight

    def test_repost_while_in_flight_does_not_enqueue_second_task(
        self, enrichment_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The set + lock on app.state must collapse concurrent POSTs to one task.

        Patches ``_enrich_one_bill`` to record its calls *without*
        discarding from the in-flight set, so after the first POST the
        bill stays "in flight" for the second POST to observe. Then
        fires two POSTs; the second must return the in-flight fragment
        and the patched task must have been called exactly once across
        both requests.
        """
        calls: list[str] = []

        def _record_only(app: object, bill_id: str) -> None:
            # Deliberately do NOT discard from enrichment_in_flight —
            # the real task does, but we want to simulate the window
            # where a second POST arrives before the first finishes.
            calls.append(bill_id)

        monkeypatch.setattr(web_app, "_enrich_one_bill", _record_only)

        first = enrichment_client.post("/bills/119/hr/1/enrichment")
        assert first.status_code == 200
        assert "Enriching this bill" in first.text
        assert calls == ["119-hr-1"]
        assert enrichment_client.app.state.enrichment_in_flight == {"119-hr-1"}

        # Second POST while the first "is still running" — must reuse
        # the existing job rather than enqueueing another one.
        second = enrichment_client.post("/bills/119/hr/1/enrichment")
        assert second.status_code == 200
        assert "Enriching this bill" in second.text
        # The task was NOT called a second time.
        assert calls == ["119-hr-1"]
        # The set was not double-incremented and did not gain a stray
        # entry; the bill is still recorded exactly once.
        assert enrichment_client.app.state.enrichment_in_flight == {"119-hr-1"}


class TestEnrichmentStatusRoute:
    def test_status_in_flight(self, enrichment_client: TestClient) -> None:
        enrichment_client.app.state.enrichment_in_flight.add("119-hr-1")
        try:
            resp = enrichment_client.get("/bills/119/hr/1/enrichment-status")
            assert resp.status_code == 200
            assert "Enriching this bill" in resp.text
        finally:
            enrichment_client.app.state.enrichment_in_flight.discard("119-hr-1")

    def test_status_done_when_all_sections_populated(self, enrichment_client: TestClient) -> None:
        db_path = enrichment_client.app.state.db_path
        stamp = "2026-05-26T00:00:00Z"
        with SqliteStorage(db_path, load_vec=False) as storage:
            storage.replace_bill_cosponsors("119-hr-1", [], fetched_at=stamp)
            storage.replace_bill_actions("119-hr-1", [], fetched_at=stamp)
            storage.replace_bill_subjects("119-hr-1", [], fetched_at=stamp)
            storage.replace_bill_titles("119-hr-1", [], fetched_at=stamp)
            storage.replace_bill_summaries("119-hr-1", [], fetched_at=stamp)
        resp = enrichment_client.get("/bills/119/hr/1/enrichment-status")
        assert resp.status_code == 200
        assert "Enrichment complete" in resp.text

    def test_status_partial_returns_button_for_retry(self, enrichment_client: TestClient) -> None:
        """No error, not in-flight, but some sections still NULL → show the button.

        Guards against the regression where status reported "done" while
        the per-section fetched_at columns were all still NULL (the
        unknown-bill / mid-job-restart path).
        """
        # Seeded bill 119-hr-1 has all *_fetched_at NULL out of the box.
        resp = enrichment_client.get("/bills/119/hr/1/enrichment-status")
        assert resp.status_code == 200
        body = resp.text
        assert "Enrichment complete" not in body
        assert "Request enrichment" in body

    def test_status_404_for_unknown_bill(self, enrichment_client: TestClient) -> None:
        resp = enrichment_client.get("/bills/119/hr/9999/enrichment-status")
        assert resp.status_code == 404

    def test_status_stale_error_does_not_override_fully_enriched(
        self, enrichment_client: TestClient
    ) -> None:
        """Same stale-error regression as TestEnrichmentProfileButton, on the status endpoint."""
        db_path = enrichment_client.app.state.db_path
        stamp = "2026-05-26T00:00:00Z"
        with SqliteStorage(db_path, load_vec=False) as storage:
            storage.set_bill_enrichment_error("119-hr-1", "stale failure")
            storage.replace_bill_cosponsors("119-hr-1", [], fetched_at=stamp)
            storage.replace_bill_actions("119-hr-1", [], fetched_at=stamp)
            storage.replace_bill_subjects("119-hr-1", [], fetched_at=stamp)
            storage.replace_bill_titles("119-hr-1", [], fetched_at=stamp)
            storage.replace_bill_summaries("119-hr-1", [], fetched_at=stamp)
        resp = enrichment_client.get("/bills/119/hr/1/enrichment-status")
        assert resp.status_code == 200
        body = resp.text
        assert "Enrichment complete" in body
        assert "Enrichment failed" not in body

    def test_status_failed_when_error_set(self, enrichment_client: TestClient) -> None:
        db_path = enrichment_client.app.state.db_path
        with SqliteStorage(db_path, load_vec=False) as storage:
            storage.set_bill_enrichment_error("119-hr-1", "boom: 500 from upstream")
        resp = enrichment_client.get("/bills/119/hr/1/enrichment-status")
        assert resp.status_code == 200
        body = resp.text
        assert "Enrichment failed" in body
        assert "boom: 500 from upstream" in body

    def test_status_404_for_invalid_bill_type(self, enrichment_client: TestClient) -> None:
        resp = enrichment_client.get("/bills/119/xyz/1/enrichment-status")
        assert resp.status_code == 404


class _StubClient:
    """Stand-in for ``concord.api.Client`` — no network calls made in tests."""

    def __enter__(self) -> "_StubClient":
        return self

    def __exit__(self, *_a: object) -> None:
        pass


class TestEnrichOneBillBackgroundTask:
    """Drive _enrich_one_bill directly with a stubbed Client + scraper."""

    def test_success_path_clears_error_and_records_fetched_at(
        self,
        enrichment_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Pre-record an error to verify it gets cleared at the start.
        db_path = enrichment_client.app.state.db_path
        with SqliteStorage(db_path, load_vec=False) as storage:
            storage.set_bill_enrichment_error("119-hr-1", "previous failure")

        monkeypatch.setattr("concord.api.Client", lambda *a, **kw: _StubClient())

        # Stub scrape_enrichment to write minimal empty payload envelopes
        # for all five sections — load_one then projects them as empty
        # lists and stamps *_fetched_at.
        def _fake_scrape(
            *,
            client: object,
            bill_keys: list[tuple[int, str, int]],
            storage_dir: Path,
            fetched_at: datetime,
        ) -> EnrichStats:
            storage_dir.mkdir(parents=True, exist_ok=True)
            for section in BILL_ENRICHMENT_SECTIONS:
                payload: dict[str, object]
                if section == "subjects":
                    payload = {"subjects": {"legislativeSubjects": []}}
                else:
                    payload = {section: []}
                envelope = {
                    "fetched_at": fetched_at.isoformat(),
                    "key": {"congress": 119, "bill_type": "hr", "bill_number": 1},
                    "payload": payload,
                }
                with (storage_dir / enrichment_jsonl_name(section)).open(
                    "a", encoding="utf-8"
                ) as fh:
                    fh.write(json.dumps(envelope) + "\n")
            return EnrichStats(
                bills_enriched=1,
                snapshots_written=len(BILL_ENRICHMENT_SECTIONS),
                section_failures=0,
            )

        monkeypatch.setattr("concord.scraper.bills.scrape_enrichment", _fake_scrape)

        web_app._enrich_one_bill(enrichment_client.app, "119-hr-1")

        with SqliteStorage(db_path, load_vec=False) as storage:
            row = storage.get_bill("119-hr-1")
            assert row is not None
            assert row["last_enrichment_error"] is None
            assert row["cosponsors_fetched_at"] is not None
            assert row["actions_fetched_at"] is not None
        # In-flight set is cleared in finally.
        assert "119-hr-1" not in enrichment_client.app.state.enrichment_in_flight

    def test_failure_path_records_error_and_clears_inflight(
        self, enrichment_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("concord.api.Client", lambda *a, **kw: _StubClient())

        def _boom(**kw: object) -> object:
            raise RuntimeError("upstream rate limit")

        monkeypatch.setattr("concord.scraper.bills.scrape_enrichment", _boom)

        enrichment_client.app.state.enrichment_in_flight.add("119-hr-1")
        web_app._enrich_one_bill(enrichment_client.app, "119-hr-1")

        db_path = enrichment_client.app.state.db_path
        with SqliteStorage(db_path, load_vec=False) as storage:
            row = storage.get_bill("119-hr-1")
            assert row is not None
            assert row["last_enrichment_error"] is not None
            assert "upstream rate limit" in row["last_enrichment_error"]
        assert "119-hr-1" not in enrichment_client.app.state.enrichment_in_flight

    def test_partial_section_failures_record_error(
        self, enrichment_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 3-of-5 (or 0-of-5) section-failure run must surface as failed, not done.

        The scraper swallows per-section exceptions and reports them via
        ``EnrichStats.section_failures``. Without inspecting that field
        the UI would show "Enrichment complete" on a run where every
        upstream sub-endpoint refused to answer.
        """
        monkeypatch.setattr("concord.api.Client", lambda *a, **kw: _StubClient())

        def _partial(**kw: object) -> EnrichStats:
            # No JSONL written; report 5/5 sections failed.
            return EnrichStats(
                bills_enriched=1,
                snapshots_written=0,
                section_failures=5,
            )

        monkeypatch.setattr("concord.scraper.bills.scrape_enrichment", _partial)

        web_app._enrich_one_bill(enrichment_client.app, "119-hr-1")

        db_path = enrichment_client.app.state.db_path
        with SqliteStorage(db_path, load_vec=False) as storage:
            row = storage.get_bill("119-hr-1")
            assert row is not None
            assert row["last_enrichment_error"] is not None
            assert "5 section(s) failed" in row["last_enrichment_error"]
        # And the status endpoint must report failed, not done.
        resp = enrichment_client.get("/bills/119/hr/1/enrichment-status")
        assert resp.status_code == 200
        assert "Enrichment failed" in resp.text
        assert "Enrichment complete" not in resp.text

    def test_stale_schema_does_not_strand_inflight(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """If clear_bill_enrichment_error raises (e.g. stale column), in-flight clears anyway.

        Guards the regression where a pre-migration DB caused the
        ``UPDATE bills SET last_enrichment_error = …`` to throw before
        the outer try/finally, stranding the bill in
        ``enrichment_in_flight``.
        """
        monkeypatch.setenv("CONGRESS_API_KEY", "test-key")
        monkeypatch.setenv("CONCORD_ENABLE_WEB_ENRICHMENT", "1")
        db_path = tmp_path / "test.db"
        storage = SqliteStorage(db_path)
        _seed(storage)
        storage.close()
        index_bills(db_path=db_path)
        app = create_app(db_path, embedder=Embedder(_StubOpenAI()), storage_dir=tmp_path)
        app.state.enrichment_in_flight.add("119-hr-1")

        # Make the very first storage call raise — simulates the
        # stale-schema "no such column" failure.
        class _BrokenStorage:
            def __init__(self, *a: object, **kw: object) -> None:
                raise sqlite3.OperationalError("no such column: bills.last_enrichment_error")

        monkeypatch.setattr("concord.storage.sqlite.SqliteStorage", _BrokenStorage)

        web_app._enrich_one_bill(app, "119-hr-1")

        # Even though everything blew up, in-flight must be cleared.
        assert "119-hr-1" not in app.state.enrichment_in_flight
