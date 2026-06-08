"""Integration tests for the Bill Brief web feature (ADR 0020)."""

from pathlib import Path

from fastapi.testclient import TestClient

from concord.brief import Briefer
from concord.embedding import EMBEDDING_DIM, Embedder
from concord.models.bills import BillCosponsor, BillDetail, BillSummary
from concord.models.members import Member, Term
from concord.pipeline.index_bills import index as index_bills
from concord.storage.sqlite import SqliteStorage
from concord.web.app import create_app

_NEUTRAL = '{"executive_summary": "Stub exec summary about energy policy."}'


# -- embedding stub (search needs an embedder; no network) --------------------


class _EmbData:
    def __init__(self, vec: list[float]) -> None:
        self.embedding = vec


class _EmbResponse:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.data = [_EmbData(v) for v in vectors]


class _StubEmbeddings:
    def create(self, *, model: str, input: list[str]) -> _EmbResponse:
        return _EmbResponse([[0.5] * EMBEDDING_DIM for _ in input])


class _StubOpenAI:
    embeddings = _StubEmbeddings()


# -- chat stub for the Briefer ------------------------------------------------


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _ChatResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]


class _Completions:
    def __init__(self, content: str, error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.calls: list[object] = []

    def create(self, *, model: str, messages: object, response_format: object, temperature: float):  # type: ignore[no-untyped-def]
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return _ChatResponse(self.content)


class _Chat:
    def __init__(self, completions: _Completions) -> None:
        self.completions = completions


class _StubChatClient:
    def __init__(self, content: str = _NEUTRAL, error: Exception | None = None) -> None:
        self.chat = _Chat(_Completions(content, error))


def _seed(storage: SqliteStorage) -> None:
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
    storage.upsert_bill(
        BillDetail(
            bill_id="119-hr-1",
            congress=119,
            bill_type="hr",
            bill_number=1,
            origin_chamber="House",
            title="Lower Energy Costs Act",
            introduced_date="2025-01-09",
            policy_area="Energy",
            sponsor_bioguide_id="S001176",
            latest_action_date="2026-03-30",
            latest_action_text="Became Public Law.",
            update_date="2026-04-01",
        ),
        fetched_at="2026-05-25T00:00:00+00:00",
    )
    storage.replace_bill_summaries(
        "119-hr-1",
        [
            BillSummary(
                version_code="00",
                action_date="2025-01-09",
                action_desc="Introduced in House",
                summary_text="<p>Requires steps to lower energy costs.</p>",
            ),
        ],
        fetched_at="2026-05-26T00:00:00Z",
    )
    # One cosponsor (party-indexed) so the fact pack's coalition/party split
    # path is exercised in the rendered brief.
    storage.replace_bill_cosponsors(
        "119-hr-1",
        [
            BillCosponsor(
                bioguide_id="S001176",
                sponsorship_date="2025-01-09",
                is_original_cosponsor=True,
            ),
        ],
        fetched_at="2026-05-26T00:00:00Z",
    )


def _make_client(
    tmp_path: Path,
    *,
    content: str = _NEUTRAL,
    error: Exception | None = None,
    with_briefer: bool = True,
) -> tuple[TestClient, _StubChatClient | None]:
    db_path = tmp_path / "test.db"
    storage = SqliteStorage(db_path)
    _seed(storage)
    storage.close()
    index_bills(db_path=db_path)
    chat: _StubChatClient | None = None
    briefer: Briefer | None = None
    if with_briefer:
        chat = _StubChatClient(content, error)
        briefer = Briefer(chat)
    app = create_app(db_path, embedder=Embedder(_StubOpenAI()), briefer=briefer)
    return TestClient(app, raise_server_exceptions=False), chat


class TestBriefGating:
    def test_enabled_when_briefer_present(self, tmp_path: Path) -> None:
        client, _ = _make_client(tmp_path)
        assert client.app.state.brief_enabled is True

    def test_disabled_without_briefer(self, tmp_path: Path) -> None:
        client, _ = _make_client(tmp_path, with_briefer=False)
        assert client.app.state.brief_enabled is False
        # No brief UI on the profile, and the route isn't registered.
        resp = client.get("/bills/119/hr/1")
        assert "Generate brief" not in resp.text
        assert client.post("/bills/119/hr/1/brief", data={"lens": ""}).status_code == 404


class TestBriefProfile:
    def test_fact_pack_shown_before_generation(self, tmp_path: Path) -> None:
        """The brief is self-contained: the deterministic fact pack renders
        even before any executive summary has been generated (ADR 0020)."""
        client, _ = _make_client(tmp_path)
        resp = client.get("/bills/119/hr/1")
        assert resp.status_code == 200
        body = resp.text
        assert "Generate brief" in body
        # No summary yet, but the fact pack is all there.
        assert "Executive summary" in body
        assert "No executive summary yet" in body
        assert "Coalition" in body
        assert "1 cosponsor" in body
        assert "Became Public Law." in body  # status (latest action)
        assert "recorded vote" in body
        # The latest CRS summary is shown verbatim with its stage label.
        assert "Latest CRS summary — Introduced in House" in body
        assert "Requires steps to lower energy costs." in body

    def test_fact_pack_still_shown_after_generation(self, tmp_path: Path) -> None:
        client, _ = _make_client(tmp_path)
        resp = client.post("/bills/119/hr/1/brief", data={"lens": ""})
        assert resp.status_code == 200
        body = resp.text
        # Both the generated summary AND the fact pack are present.
        assert "Stub exec summary about energy policy." in body
        assert "Coalition" in body
        assert "Latest CRS summary — Introduced in House" in body


class TestBriefGeneration:
    def test_post_generates_caches_and_persists(self, tmp_path: Path) -> None:
        client, chat = _make_client(tmp_path)
        assert chat is not None

        resp = client.post("/bills/119/hr/1/brief", data={"lens": ""})
        assert resp.status_code == 200
        assert "Stub exec summary about energy policy." in resp.text
        assert "Regenerate brief" in resp.text
        assert len(chat.chat.completions.calls) == 1

        # The neutral brief is now cached and shows on a fresh profile load.
        profile = client.get("/bills/119/hr/1")
        assert "Stub exec summary about energy policy." in profile.text
        assert "Regenerate brief" in profile.text

    def test_cache_hit_skips_second_llm_call(self, tmp_path: Path) -> None:
        client, chat = _make_client(tmp_path)
        assert chat is not None
        client.post("/bills/119/hr/1/brief", data={"lens": ""})
        client.post("/bills/119/hr/1/brief", data={"lens": ""})
        # Same fact pack + same lens → second POST is a cache hit, no new call.
        assert len(chat.chat.completions.calls) == 1

    def test_lens_tailors_and_reaches_model(self, tmp_path: Path) -> None:
        client, chat = _make_client(tmp_path)
        assert chat is not None
        resp = client.post(
            "/bills/119/hr/1/brief",
            data={"lens": "Emphasize fiscal impact for a budget audience"},
        )
        assert resp.status_code == 200
        assert "Tailored for" in resp.text
        assert "Emphasize fiscal impact for a budget audience" in resp.text
        sent = " ".join(
            m["content"]
            for m in chat.chat.completions.calls[-1]  # type: ignore[index]
        )
        assert "Emphasize fiscal impact for a budget audience" in sent

    def test_stale_banner_after_data_change(self, tmp_path: Path) -> None:
        client, _ = _make_client(tmp_path)
        client.post("/bills/119/hr/1/brief", data={"lens": ""})
        # Mutate the underlying mirror data so the fact pack hash moves.
        db_path = client.app.state.db_path
        with SqliteStorage(db_path, load_vec=False) as storage:
            storage.replace_bill_cosponsors(
                "119-hr-1",
                [
                    BillCosponsor(
                        bioguide_id="X000001",
                        sponsorship_date="2025-02-01",
                        is_original_cosponsor=False,
                    )
                ],
                fetched_at="2026-05-27T00:00:00Z",
            )
        profile = client.get("/bills/119/hr/1")
        assert "underlying data changed" in profile.text

    def test_generation_error_renders_message(self, tmp_path: Path) -> None:
        client, _ = _make_client(tmp_path, error=RuntimeError("upstream 500"))
        resp = client.post("/bills/119/hr/1/brief", data={"lens": ""})
        assert resp.status_code == 200
        # Apostrophe in "Couldn't" is HTML-escaped; assert an escape-free part.
        assert "generate a brief right now" in resp.text

    def test_failed_regenerate_falls_back_to_cached(self, tmp_path: Path) -> None:
        # A brief already exists, but generation now fails: the user must
        # still see the older (stale) brief rather than an empty box.
        client, _ = _make_client(tmp_path, error=RuntimeError("boom"))
        db_path = client.app.state.db_path
        with SqliteStorage(db_path, load_vec=False) as storage:
            storage.upsert_bill_brief(
                bill_id="119-hr-1",
                lens="",
                executive_summary="Older cached summary.",
                facts_hash="stale-hash",  # won't match current facts → cache miss
                model="gpt-4o-mini",
                prompt_version=1,
                generated_at="2026-05-30T00:00:00+00:00",
            )
        resp = client.post("/bills/119/hr/1/brief", data={"lens": ""})
        assert resp.status_code == 200
        body = resp.text
        assert "Older cached summary." in body  # cached brief still shown
        assert "generate a brief right now" in body  # error surfaced
        assert "underlying data changed" in body  # flagged stale

    def test_post_unknown_bill_404(self, tmp_path: Path) -> None:
        client, _ = _make_client(tmp_path)
        assert client.post("/bills/119/hr/9999/brief", data={"lens": ""}).status_code == 404

    def test_post_invalid_bill_type_404(self, tmp_path: Path) -> None:
        client, _ = _make_client(tmp_path)
        assert client.post("/bills/119/xyz/1/brief", data={"lens": ""}).status_code == 404
