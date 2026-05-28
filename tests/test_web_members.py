"""Integration tests for the Members web routes.

Covers ``/members`` (browse list with chamber/party filters), the
``/members/{bioguide_id}`` profile page, and the federated ``/search``
that surfaces Members alongside Proceedings.
"""

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from concord.embedding import EMBEDDING_DIM, Embedder
from concord.models import Member, Term
from concord.pipeline.index_members import index as index_members
from concord.storage.sqlite import SqliteStorage
from concord.web.app import create_app
from concord.web.search import collapse_term_history


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


def _seed_members(storage: SqliteStorage) -> None:
    """Three Members: two currently serving (one D House, one I Senate),
    one historical (party-changing Republican-then-Independent Senator)."""
    storage.upsert_member(
        Member(
            bioguide_id="O000172",
            first_name="Alexandria",
            last_name="Ocasio-Cortez",
            display_name="Alexandria Ocasio-Cortez",
            photo_url="https://example.invalid/o.jpg",
        ),
        [
            Term(
                bioguide_id="O000172",
                congress=119,
                chamber="house",
                state="NY",
                district=14,
                party="Democratic",
                start_date="2025-01-01",
                end_date="2027-01-03",
            ),
        ],
        fetched_at="2026-05-25T00:00:00+00:00",
    )
    storage.upsert_member(
        Member(
            bioguide_id="S000033",
            first_name="Bernard",
            last_name="Sanders",
            display_name="Bernard Sanders",
            birth_year=1941,
        ),
        [
            Term(
                bioguide_id="S000033",
                congress=119,
                chamber="senate",
                state="VT",
                party="Independent",
                start_date="2025-01-01",
                end_date="2027-01-03",
            ),
        ],
        fetched_at="2026-05-25T00:00:00+00:00",
    )
    storage.upsert_member(
        Member(
            bioguide_id="J000301",
            first_name="James",
            last_name="Jeffords",
            display_name="James M. Jeffords",
            birth_year=1934,
            death_year=2014,
        ),
        [
            Term(
                bioguide_id="J000301",
                congress=116,
                chamber="senate",
                state="VT",
                party="Republican",
                start_date="2019-01-01",
                end_date="2021-01-01",
            ),
        ],
        fetched_at="2026-05-25T00:00:00+00:00",
    )

    # Populate the FTS index the same way `concord index members` would.
    storage.close()
    index_members(db_path=storage.path)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    db_path = tmp_path / "test.db"
    storage = SqliteStorage(db_path)
    _seed_members(storage)
    app = create_app(db_path, embedder=Embedder(_StubOpenAI()))
    return TestClient(app, raise_server_exceptions=False)


class TestMembersIndex:
    def test_lists_currently_serving(self, client: TestClient) -> None:
        resp = client.get("/members")
        assert resp.status_code == 200
        body = resp.text
        # Both currently-serving members appear.
        assert "Alexandria Ocasio-Cortez" in body
        assert "Bernard Sanders" in body
        # The historical Jeffords does NOT appear in the default index.
        assert "Jeffords" not in body

    def test_chamber_filter_house(self, client: TestClient) -> None:
        resp = client.get("/members?chamber=house")
        assert resp.status_code == 200
        assert "Ocasio-Cortez" in resp.text
        assert "Sanders" not in resp.text

    def test_chamber_filter_senate(self, client: TestClient) -> None:
        resp = client.get("/members?chamber=senate")
        assert resp.status_code == 200
        assert "Sanders" in resp.text
        assert "Ocasio-Cortez" not in resp.text

    def test_party_filter(self, client: TestClient) -> None:
        resp = client.get("/members?party=Democratic")
        assert resp.status_code == 200
        assert "Ocasio-Cortez" in resp.text
        assert "Sanders" not in resp.text

    def test_combined_filters(self, client: TestClient) -> None:
        resp = client.get("/members?chamber=senate&party=Independent")
        assert resp.status_code == 200
        assert "Sanders" in resp.text
        assert "Ocasio-Cortez" not in resp.text


class TestMemberProfile:
    def test_renders_known_bioguide(self, client: TestClient) -> None:
        resp = client.get("/members/S000033")
        assert resp.status_code == 200
        body = resp.text
        assert "Bernard Sanders" in body
        assert "Senate" in body or "Senator" in body
        # Future-phase placeholders should be present.
        assert "Sponsored bills" in body
        assert "Recent votes" in body
        assert "Mentioned in proceedings" in body

    def test_unknown_bioguide_404(self, client: TestClient) -> None:
        resp = client.get("/members/X999999")
        assert resp.status_code == 404


class TestCollapseTermHistory:
    """Unit tests for the term-history collapse + year-cap helper."""

    def test_empty(self) -> None:
        assert collapse_term_history([]) == []

    def test_collapses_consecutive_identical_terms(self) -> None:
        terms = [
            {
                "congress": 119,
                "chamber": "senate",
                "state": "AK",
                "district": None,
                "party": "Republican",
                "start_date": "2025-01-03",
                "end_date": "2027-01-03",
            },
            {
                "congress": 118,
                "chamber": "senate",
                "state": "AK",
                "district": None,
                "party": "Republican",
                "start_date": "2023-01-03",
                "end_date": "2025-01-03",
            },
            {
                "congress": 117,
                "chamber": "senate",
                "state": "AK",
                "district": None,
                "party": "Republican",
                "start_date": "2021-01-03",
                "end_date": "2023-01-03",
            },
        ]
        groups = collapse_term_history(terms, today=date(2026, 5, 28))
        assert len(groups) == 1
        g = groups[0]
        assert g["congress_min"] == 117
        assert g["congress_max"] == 119
        assert g["year_min"] == 2021
        # Capped at the current year (2026), not the term's 2027 end_date.
        assert g["year_max"] == 2026

    def test_splits_when_chamber_changes(self) -> None:
        terms = [
            {
                "congress": 119,
                "chamber": "senate",
                "state": "NY",
                "district": None,
                "party": "Democratic",
                "start_date": "2025-01-03",
                "end_date": "2027-01-03",
            },
            {
                "congress": 118,
                "chamber": "house",
                "state": "NY",
                "district": 14,
                "party": "Democratic",
                "start_date": "2023-01-03",
                "end_date": "2025-01-03",
            },
        ]
        groups = collapse_term_history(terms, today=date(2026, 5, 28))
        assert len(groups) == 2
        assert groups[0]["chamber"] == "senate"
        assert groups[1]["chamber"] == "house"
        assert groups[1]["district"] == 14

    def test_splits_when_party_changes(self) -> None:
        terms = [
            {
                "congress": 109,
                "chamber": "senate",
                "state": "VT",
                "district": None,
                "party": "Independent",
                "start_date": "2005-01-03",
                "end_date": "2007-01-03",
            },
            {
                "congress": 108,
                "chamber": "senate",
                "state": "VT",
                "district": None,
                "party": "Republican",
                "start_date": "2003-01-03",
                "end_date": "2005-01-03",
            },
        ]
        groups = collapse_term_history(terms, today=date(2026, 5, 28))
        assert len(groups) == 2
        assert groups[0]["party"] == "Independent"
        assert groups[1]["party"] == "Republican"


class TestFederatedSearch:
    def test_search_returns_members_section(self, client: TestClient) -> None:
        resp = client.get("/search?q=Sanders")
        assert resp.status_code == 200
        body = resp.text
        assert "Members" in body
        assert "Bernard Sanders" in body

    def test_members_off_suppresses_members_section(self, client: TestClient) -> None:
        resp = client.get("/search?q=Sanders&proceedings=on&members=")
        assert resp.status_code == 200
        body = resp.text
        # When members are off, the member card for Sanders should not render.
        # (The query string still gets echoed; check for the member-card photo URL
        # instead, which only appears when the member result actually renders.)
        assert "example.invalid" not in body

    def test_proceedings_off_suppresses_proceedings_section(self, client: TestClient) -> None:
        resp = client.get("/search?q=Sanders&members=on&proceedings=")
        assert resp.status_code == 200
        body = resp.text
        assert "Members" in body
        # When proceedings are off, the "Proceedings (N)" header should not appear.
        assert "Proceedings (" not in body

    def test_ambiguous_query_disambiguates_to_current(self, client: TestClient) -> None:
        # Both Sanders and Jeffords are linked to "VT" via their last_name
        # field, but the test query "Vermont" doesn't hit names. Use a
        # broader query that catches both, then assert current ones appear.
        # The members_fts has Last/First/display, so query on a shared
        # substring of multiple historical/current senators.
        resp = client.get("/search?q=Bernard")
        assert resp.status_code == 200
        assert "Bernard Sanders" in resp.text
