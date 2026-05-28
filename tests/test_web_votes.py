"""Integration tests for the Votes web routes (Phase 3a)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from concord.embedding import EMBEDDING_DIM, Embedder
from concord.models import Bill, Member, Term, Vote, VotePosition
from concord.pipeline.index_votes import index as index_votes
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


HOUSE_BIOGUIDE = "H000001"
SENATE_BIOGUIDE = "S000033"
INDEP_BIOGUIDE = "I000001"


def _vote(
    *,
    roll_number: int,
    bill_id: str | None = "119-hr-3424",
    amendment_id: str | None = None,
    vote_kind: str = "standard",
    result: str = "Passed",
    yea: int | None = 220,
    nay: int | None = 210,
) -> Vote:
    return Vote(
        vote_id=f"house-119-1-{roll_number}",
        chamber="house",
        congress=119,
        session=1,
        roll_number=roll_number,
        vote_kind=vote_kind,  # type: ignore[arg-type]
        start_date=f"2026-04-{roll_number:02d}T18:00:00Z",
        vote_question="On Passage of the Bill",
        vote_type="Yea-and-Nay",
        threshold="simple_majority",
        result=result,
        yea_count=yea,
        nay_count=nay,
        present_count=0,
        not_voting_count=0,
        bill_id=bill_id,
        amendment_id=amendment_id,
        is_party_unity=False,
        update_date="2026-04-30",
    )


def _seed(storage: SqliteStorage) -> None:
    # Bill the vote points at.
    storage.upsert_bill(
        Bill(
            bill_id="119-hr-3424",
            congress=119,
            bill_type="hr",
            bill_number=3424,
            origin_chamber="House",
            title="Sample Bill Act",
            introduced_date="2025-05-01",
            policy_area="Energy",
            sponsor_bioguide_id=HOUSE_BIOGUIDE,
            latest_action_date="2026-04-01",
            latest_action_text="Passed.",
            update_date="2026-04-01",
        ),
        fetched_at="2026-05-25T00:00:00+00:00",
    )

    # Members. The two majority-party Members exist so the Recent-votes
    # and Party-unity score sections have material to render.
    storage.upsert_member(
        Member(
            bioguide_id=HOUSE_BIOGUIDE,
            first_name="House",
            last_name="Memb",
            display_name="House Member",
        ),
        [
            Term(
                bioguide_id=HOUSE_BIOGUIDE,
                congress=119,
                chamber="house",
                state="LA",
                district=1,
                party="Republican",
                start_date="2025-01-03",
            ),
        ],
        fetched_at="2026-05-25T00:00:00+00:00",
    )
    storage.upsert_member(
        Member(
            bioguide_id=SENATE_BIOGUIDE,
            first_name="Bernard",
            last_name="Sanders",
            display_name="Bernard Sanders",
        ),
        [
            Term(
                bioguide_id=SENATE_BIOGUIDE,
                congress=119,
                chamber="senate",
                state="VT",
                party="Independent",
                start_date="2025-01-03",
            ),
        ],
        fetched_at="2026-05-25T00:00:00+00:00",
    )
    storage.upsert_member(
        Member(
            bioguide_id=INDEP_BIOGUIDE,
            first_name="In",
            last_name="Dep",
            display_name="In Dep",
        ),
        [
            Term(
                bioguide_id=INDEP_BIOGUIDE,
                congress=119,
                chamber="house",
                state="ME",
                district=2,
                party="Independent",
                start_date="2025-01-03",
            ),
        ],
        fetched_at="2026-05-25T00:00:00+00:00",
    )

    def _pos(bg: str, pos: str, party: str, state: str) -> VotePosition:
        return VotePosition(bioguide_id=bg, position=pos, vote_party=party, vote_state=state)

    # 12 standard party-unity votes (denominator > 10) plus 1 election vote.
    for roll in range(240, 252):
        storage.upsert_vote(_vote(roll_number=roll), fetched_at="t")
        storage.upsert_vote_positions(
            f"house-119-1-{roll}",
            [
                _pos(HOUSE_BIOGUIDE, "Yea", "R", "LA"),
                # Add a D Member so the vote splits party majorities.
                _pos("DXX0001", "Nay", "D", "CA"),
                _pos(INDEP_BIOGUIDE, "Yea", "I", "ME"),
            ],
        )

    storage.upsert_vote(
        _vote(
            roll_number=2,
            vote_kind="election",
            result="Johnson",
            yea=None,
            nay=None,
            bill_id=None,
        ),
        fetched_at="t",
    )
    storage.upsert_vote_positions(
        "house-119-1-2",
        [_pos(HOUSE_BIOGUIDE, "Johnson", "R", "LA")],
    )

    # Seed Senate party-unity votes so the Senate Member profile renders
    # live data instead of a placeholder.
    storage.upsert_member(
        Member(
            bioguide_id="S000044",
            first_name="Senate",
            last_name="Demo",
            display_name="Senate Demo",
        ),
        [
            Term(
                bioguide_id="S000044",
                congress=119,
                chamber="senate",
                state="OR",
                party="Democratic",
                start_date="2025-01-03",
            ),
        ],
        fetched_at="2026-05-25T00:00:00+00:00",
    )
    for roll in range(1, 14):
        storage.upsert_vote(
            Vote(
                vote_id=f"senate-119-1-{roll}",
                chamber="senate",
                congress=119,
                session=1,
                roll_number=roll,
                vote_kind="standard",
                start_date=f"2026-05-{roll:02d}T18:00:00Z",
                vote_question="On Passage of the Bill",
                vote_type="On Passage of the Bill",
                threshold="simple_majority",
                result="Passed",
                yea_count=60,
                nay_count=40,
                present_count=0,
                not_voting_count=0,
                bill_id="119-hr-3424",
                amendment_id=None,
                is_party_unity=False,
                update_date="2026-05-30",
            ),
            fetched_at="t",
        )
        storage.upsert_vote_positions(
            f"senate-119-1-{roll}",
            [
                _pos("S000044", "Nay", "D", "OR"),
                _pos("SXX0001", "Yea", "R", "TX"),
            ],
        )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    db_path = tmp_path / "votes_web.db"
    storage = SqliteStorage(db_path)
    try:
        _seed(storage)
    finally:
        storage.close()

    index_votes(db_path=db_path)

    app = create_app(db_path, embedder=Embedder(_StubOpenAI()))
    return TestClient(app)


class TestVotesIndex:
    def test_renders(self, client: TestClient) -> None:
        resp = client.get("/votes")
        assert resp.status_code == 200
        assert "Roll-call votes" in resp.text
        # At least one row identifier should appear.
        assert "H 240" in resp.text

    def test_chamber_filter(self, client: TestClient) -> None:
        resp = client.get("/votes?chamber=senate")
        assert resp.status_code == 200
        # Senate votes are seeded — list should now render rather than
        # an empty-state message.
        assert "S 1" in resp.text or "Senate" in resp.text

    def test_vote_kind_filter(self, client: TestClient) -> None:
        resp = client.get("/votes?vote_kind=election")
        assert resp.status_code == 200
        # Only the election vote (roll 2) should render.
        assert "H 2" in resp.text
        assert "H 240" not in resp.text


class TestVoteProfile:
    def test_house_vote_renders(self, client: TestClient) -> None:
        resp = client.get("/votes/house/119/1/240")
        assert resp.status_code == 200
        body = resp.text
        assert "On Passage of the Bill" in body
        # Underlying bill linked.
        assert "/bills/119/hr/3424" in body
        # Totals shown.
        assert "Yea 220" in body
        # Position roster.
        assert "House Member" in body

    def test_election_vote_no_totals(self, client: TestClient) -> None:
        resp = client.get("/votes/house/119/1/2")
        assert resp.status_code == 200
        body = resp.text
        # Election votes leave counts NULL; the totals line is suppressed.
        assert "Yea " not in body or "Johnson" in body

    def test_senate_vote_renders(self, client: TestClient) -> None:
        resp = client.get("/votes/senate/119/1/1")
        assert resp.status_code == 200
        body = resp.text
        assert "Phase 3b" not in body
        # Header and position roster both appear.
        assert "Senate" in body
        assert "S000044" in body

    def test_unknown_roll_404(self, client: TestClient) -> None:
        resp = client.get("/votes/house/119/1/999999")
        assert resp.status_code == 404

    def test_invalid_chamber_404(self, client: TestClient) -> None:
        resp = client.get("/votes/committee/119/1/1")
        assert resp.status_code == 404

    def test_invalid_session_404(self, client: TestClient) -> None:
        resp = client.get("/votes/house/119/3/1")
        assert resp.status_code == 404


class TestBillProfileVoteHistory:
    def test_renders_vote_rows(self, client: TestClient) -> None:
        resp = client.get("/bills/119/hr/3424")
        assert resp.status_code == 200
        body = resp.text
        assert "Vote history" in body
        # One of the seeded votes should appear.
        assert "/votes/house/119/1/240" in body


class TestMemberProfileVotes:
    def test_house_member_recent_votes_and_party_unity(self, client: TestClient) -> None:
        resp = client.get(f"/members/{HOUSE_BIOGUIDE}")
        assert resp.status_code == 200
        body = resp.text
        assert "Recent votes" in body
        assert "Party Unity Score" in body
        # 12 party-unity votes — should render the percentage, not the
        # "not enough votes yet" treatment.
        assert "of party-unity votes" in body
        assert "not enough" not in body
        # Methodology link.
        assert "/about/methodology#party-unity" in body

    def test_senate_member_renders_live(self, client: TestClient) -> None:
        # The Senate Member seeded with party-unity positions ("S000044")
        # should render live Recent votes + a Party Unity Score section.
        resp = client.get("/members/S000044")
        assert resp.status_code == 200
        body = resp.text
        assert "Phase 3b" not in body
        assert "Recent votes" in body
        assert "Party Unity Score" in body
        assert "of party-unity votes" in body
        assert "Senate:" in body

    def test_senate_independent_member_shows_no_score(self, client: TestClient) -> None:
        # The Sanders-style I Senator has no party-unity positions seeded
        # — the section still renders without the old Phase 3b placeholder.
        resp = client.get(f"/members/{SENATE_BIOGUIDE}")
        assert resp.status_code == 200
        body = resp.text
        assert "Phase 3b" not in body
        assert "Recent votes" in body
        assert "Party Unity Score" in body


class TestMethodologyPage:
    def test_renders_party_unity_section(self, client: TestClient) -> None:
        resp = client.get("/about/methodology")
        assert resp.status_code == 200
        body = resp.text
        assert 'id="party-unity"' in body
        assert "Party Unity Score" in body
        assert "Denominator" in body
        assert "Numerator" in body
        # Chamber-scoping wording from the Phase 3b refinement.
        assert "Chamber scope" in body
        assert "independently" in body
