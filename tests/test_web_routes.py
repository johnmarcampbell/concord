"""Integration tests for the FastAPI app via ``TestClient``.

The fixture builds a small seeded SQLite database, constructs the app with
a stub :class:`Embedder` (no OpenAI key required), and the tests hit real
HTTP endpoints.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from concord.embedding import EMBEDDING_DIM, Embedder
from concord.models import Article, Issue, Proceeding
from concord.storage import SqliteStorage
from concord.web.app import create_app

# -- fixtures -----------------------------------------------------------------


def _make_proceeding(granule_id: str, *, text: str = "Banking regulation discussion") -> Proceeding:
    text_url = f"https://www.congress.gov/119/crec/2026/05/22/172/88/modified/{granule_id}.htm"
    pdf_url = f"https://www.congress.gov/119/crec/2026/05/22/172/88/{granule_id}.pdf"
    issue = Issue(
        issue_date="2026-05-22",
        congress=119,
        session=2,
        volume=172,
        issue_number=88,
        update_date="2026-05-23T06:44:22Z",
    )
    article = Article(
        section="Senate Section",
        title=f"Sample {granule_id}",
        start_page="S1",
        end_page="S2",
        text_url=text_url,
        pdf_url=pdf_url,
        granule_id=granule_id,
    )
    return Proceeding.build(
        issue=issue,
        article=article,
        text=text,
        fetched_at=datetime(2026, 5, 24, tzinfo=UTC),
    )


def _seed(storage: SqliteStorage, granule_ids: list[str]) -> None:
    for gid in granule_ids:
        storage.write(_make_proceeding(gid))
        storage.bulk_insert_chunks(
            gid,
            [(0, f"Banking regulation discussion for {gid}", 0, 40)],
            chunked_at="2026-05-25T00:00:00Z",
        )
        chunk_id = storage.connection.execute(
            "SELECT id FROM chunks WHERE granule_id = ? ORDER BY id DESC LIMIT 1",
            (gid,),
        ).fetchone()[0]
        storage.bulk_insert_embeddings([(chunk_id, [0.5] * EMBEDDING_DIM)])


class _StubData:
    def __init__(self, vec: list[float]) -> None:
        self.embedding = vec


class _StubResponse:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.data = [_StubData(v) for v in vectors]


class _StubEmbeddings:
    def create(self, *, model: str, input: list[str]) -> _StubResponse:
        return _StubResponse([[0.5] * EMBEDDING_DIM for _ in input])


class _StubClient:
    embeddings = _StubEmbeddings()


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """A fully wired TestClient pointed at a seeded SQLite DB."""
    db_path = tmp_path / "test.db"
    storage = SqliteStorage(db_path)
    _seed(
        storage,
        ["CREC-2026-05-22-pt1-PgS001-1", "CREC-2026-05-22-pt1-PgS001-2"],
    )
    storage.close()
    app = create_app(db_path, embedder=Embedder(_StubClient()))
    # raise_server_exceptions=False so we can verify a 429 response body
    # instead of having the exception leak through TestClient.
    return TestClient(app, raise_server_exceptions=False)


# -- basic routes -------------------------------------------------------------


class TestBasicRoutes:
    def test_index_renders(self, client: TestClient) -> None:
        r = client.get("/")
        assert r.status_code == 200
        assert "Concord" in r.text
        # Search box present.
        assert 'name="q"' in r.text

    def test_healthz_returns_ok(self, client: TestClient) -> None:
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"ok": True}

    def test_static_css_served(self, client: TestClient) -> None:
        r = client.get("/static/style.css")
        assert r.status_code == 200
        assert "font-serif" in r.text


# -- search endpoint ----------------------------------------------------------


class TestSearchEndpoint:
    def test_full_page_response(self, client: TestClient) -> None:
        r = client.get("/search?q=banking")
        assert r.status_code == 200
        assert "<html" in r.text  # full page
        assert "Sample" in r.text  # at least one result rendered

    def test_partial_response_for_htmx(self, client: TestClient) -> None:
        r = client.get("/search?q=banking", headers={"HX-Request": "true"})
        assert r.status_code == 200
        # Partial — no full HTML envelope.
        assert "<html" not in r.text
        assert "Sample" in r.text

    def test_empty_query_returns_empty_state(self, client: TestClient) -> None:
        r = client.get("/search?q=")
        assert r.status_code == 200
        # The empty-results message from _results.html.
        assert "Enter a query" in r.text

    def test_invalid_date_returns_400(self, client: TestClient) -> None:
        r = client.get("/search?q=banking&from=not-a-date")
        assert r.status_code == 400

    def test_section_filter_is_passed_through(self, client: TestClient) -> None:
        # All seeded results are in "Senate Section"; filtering for House
        # should yield none.
        r = client.get("/search?q=banking&section=House%20Section")
        assert r.status_code == 200
        assert "No proceedings match" in r.text


# -- proceeding detail --------------------------------------------------------


class TestProceedingRoute:
    def test_known_proceeding_renders(self, client: TestClient) -> None:
        r = client.get("/proceedings/CREC-2026-05-22-pt1-PgS001-1")
        assert r.status_code == 200
        # Sidebar metadata visible.
        assert "Senate Section" in r.text
        # Granule ID echoed in sidebar.
        assert "CREC-2026-05-22-pt1-PgS001-1" in r.text
        # Body text rendered.
        assert "Banking regulation discussion" in r.text

    def test_unknown_proceeding_returns_404(self, client: TestClient) -> None:
        r = client.get("/proceedings/CREC-not-a-real-granule")
        assert r.status_code == 404
        assert "Not found" in r.text


# -- search form behavior on every page -------------------------------------


class TestSearchFormTarget:
    """Regression: the header search form must work from /proceedings/{id}.

    Previously the form had hx-target="#results", which doesn't exist on
    the proceeding detail page, so HTMX silently failed. The fix targets
    `main`, which exists on every page via base.html.
    """

    def test_form_targets_main_on_index(self, client: TestClient) -> None:
        r = client.get("/")
        assert 'hx-target="main"' in r.text
        assert 'hx-target="#results"' not in r.text

    def test_form_targets_main_on_proceeding(self, client: TestClient) -> None:
        r = client.get("/proceedings/CREC-2026-05-22-pt1-PgS001-1")
        assert 'hx-target="main"' in r.text
        assert 'hx-target="#results"' not in r.text

    def test_form_targets_main_on_search(self, client: TestClient) -> None:
        r = client.get("/search?q=banking")
        assert 'hx-target="main"' in r.text

    def test_form_targets_main_on_404(self, client: TestClient) -> None:
        r = client.get("/proceedings/CREC-not-real")
        assert r.status_code == 404
        assert 'hx-target="main"' in r.text


# -- rate limiting ------------------------------------------------------------


class TestRateLimit:
    def test_search_endpoint_rate_limited(self, client: TestClient) -> None:
        """31st search request in a single minute should be rejected.

        SlowAPI uses the configured ``30/minute`` cap on the search endpoint.
        TestClient requests all come from the same fixed remote address.
        """
        # First 30 succeed.
        for _ in range(30):
            r = client.get("/search?q=banking")
            assert r.status_code == 200
        r = client.get("/search?q=banking")
        assert r.status_code == 429

    def test_healthz_not_rate_limited(self, client: TestClient) -> None:
        # Even after a search-endpoint flood, /healthz keeps responding.
        for _ in range(50):
            assert client.get("/healthz").status_code == 200
