"""Smoke tests proving the project skeleton is wired up correctly."""

import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

import concord
from concord.embedding import EMBEDDING_DIM, Embedder
from concord.models import Member, Term
from concord.pipeline.index_bills import index as index_bills
from concord.pipeline.index_members import index as index_members
from concord.pipeline.index_votes import index as index_votes
from concord.pipeline.load_bills import load as load_bills
from concord.pipeline.load_members import load as load_members
from concord.pipeline.load_votes import load as load_votes
from concord.scraper.bills import BILLS_JSONL_NAME, enrichment_jsonl_name
from concord.scraper.votes import (
    HOUSE_VOTE_POSITIONS_JSONL_NAME,
    HOUSE_VOTES_JSONL_NAME,
)
from concord.storage.sqlite import SqliteStorage
from concord.web.app import create_app


def test_package_importable() -> None:
    assert concord.__version__


def test_fixtures_dir_exists(fixtures_dir: Path) -> None:
    assert fixtures_dir.exists()
    assert fixtures_dir.is_dir()


def test_members_end_to_end(tmp_path: Path) -> None:
    """One smoke test exercises Stage 1 → Stage 2 → web for Members.

    Skips Stage 0 (network) and instead writes a hand-built JSONL of
    three snapshots, then loads + indexes + queries the FastAPI app.
    Asserts the acceptance-criteria HTTP routes all return 200.
    """
    here = Path(__file__).parent / "fixtures" / "api" / "members"
    payloads = [
        json.loads((here / "current_house.json").read_text())["members"][0],
        json.loads((here / "current_senate.json").read_text())["members"][0],
        json.loads((here / "historical.json").read_text())["members"][0],
    ]

    fetched_at = datetime(2026, 5, 25, 14, 2, 11, tzinfo=UTC).isoformat()
    jsonl = tmp_path / "members.jsonl"
    # The two current Members get a 119 snapshot; the historical one
    # gets a 109 snapshot (the last Congress Jeffords was in the Senate).
    snapshots = [
        (payloads[0], 119),
        (payloads[1], 119),
        (payloads[2], 109),
    ]
    jsonl.write_text(
        "\n".join(
            json.dumps(
                {
                    "fetched_at": fetched_at,
                    "key": {"bioguide_id": p["bioguideId"], "congress": congress},
                    "payload": p,
                }
            )
            for p, congress in snapshots
        )
        + "\n",
        encoding="utf-8",
    )

    db = tmp_path / "test.db"
    # Touch the DB with load_vec=True so the chunks_vec virtual table
    # exists. /search would 500 trying to query it otherwise — a real
    # deployment would have run `concord run proceedings` first.
    SqliteStorage(db).close()

    load_stats = load_members(jsonl_path=jsonl, db_path=db)
    assert load_stats.members_written == 3
    index_stats = index_members(db_path=db)
    assert index_stats.indexed_members == 3

    # Minimal embedder stub — search_proceedings is exercised but the
    # underlying chunks table is empty, so the embedder is called with
    # the query but the result set is empty. That's fine for the smoke.
    class _StubResp:
        def __init__(self, vectors: list[list[float]]) -> None:
            self.data = [type("D", (), {"embedding": v})() for v in vectors]

    class _StubEmb:
        def create(self, *, model: str, input: list[str]) -> _StubResp:
            return _StubResp([[0.5] * EMBEDDING_DIM for _ in input])

    class _Stub:
        embeddings = _StubEmb()

    app = create_app(db, embedder=Embedder(_Stub()))
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/members")
    assert resp.status_code == 200
    assert "Ocasio-Cortez" in resp.text

    resp = client.get("/members/S000033")
    assert resp.status_code == 200
    assert "Sanders" in resp.text

    resp = client.get("/search?q=Sanders")
    assert resp.status_code == 200
    assert "Members" in resp.text
    assert "Sanders" in resp.text


def test_bills_end_to_end(tmp_path: Path) -> None:
    """Stage 1 → Stage 2 → web smoke for Bills (Phase 2a).

    Skips Stage 0 (network) — writes a hand-built bills.jsonl, then runs
    load + index and pokes the FastAPI app at the four routes named in
    the plan's verification section.
    """
    storage_dir = tmp_path / "data"
    storage_dir.mkdir()
    fetched_at = datetime(2026, 5, 25, 14, 2, 11, tzinfo=UTC).isoformat()
    bill_payload = json.loads(
        (Path(__file__).parent / "fixtures" / "api" / "bills" / "detail_119_hr_1.json").read_text()
    )["bill"]
    (storage_dir / BILLS_JSONL_NAME).write_text(
        json.dumps(
            {
                "fetched_at": fetched_at,
                "key": {"congress": 119, "bill_type": "hr", "bill_number": 1},
                "payload": bill_payload,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    db = tmp_path / "test.db"
    SqliteStorage(db).close()

    load_stats = load_bills(storage_dir=storage_dir, db_path=db)
    assert load_stats.bills_written == 1
    index_stats = index_bills(db_path=db)
    assert index_stats.indexed_bills == 1

    class _StubResp:
        def __init__(self, vectors: list[list[float]]) -> None:
            self.data = [type("D", (), {"embedding": v})() for v in vectors]

    class _StubEmb:
        def create(self, *, model: str, input: list[str]) -> _StubResp:
            return _StubResp([[0.5] * EMBEDDING_DIM for _ in input])

    class _Stub:
        embeddings = _StubEmb()

    app = create_app(db, embedder=Embedder(_Stub()))
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/bills")
    assert resp.status_code == 200
    assert "Lower Energy Costs Act" in resp.text

    resp = client.get("/bills/119/hr/1")
    assert resp.status_code == 200
    # Bill is tier-1 only — the five tier-2 sections render their
    # "not yet fetched" empty states.
    assert "Cosponsors not yet fetched" in resp.text

    # Bare-identifier query: with only one Bill in scope, this redirects.
    resp = client.get("/search?q=HR+1", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/bills/119/hr/1"


def test_bills_tier2_end_to_end(tmp_path: Path) -> None:
    """Phase 2b smoke: write tier-1 + tier-2 JSONL for one bill, load + index,
    poke routes for both the enriched bill and a tier-1-only bill.
    """
    fixtures = Path(__file__).parent / "fixtures" / "api" / "bills"
    storage_dir = tmp_path / "data"
    storage_dir.mkdir()
    fetched_at = datetime(2026, 5, 25, 14, 2, 11, tzinfo=UTC).isoformat()

    def _envelope(payload: dict, key: dict) -> str:
        return json.dumps({"fetched_at": fetched_at, "key": key, "payload": payload}) + "\n"

    bill_hr_1 = json.loads((fixtures / "detail_119_hr_1.json").read_text())["bill"]
    bill_hr_22 = json.loads((fixtures / "detail_119_hr_22.json").read_text())["bill"]
    (storage_dir / BILLS_JSONL_NAME).write_text(
        _envelope(bill_hr_1, {"congress": 119, "bill_type": "hr", "bill_number": 1})
        + _envelope(bill_hr_22, {"congress": 119, "bill_type": "hr", "bill_number": 22}),
        encoding="utf-8",
    )
    # Enrichment only for 119-hr-1.
    section_fixtures = {
        "cosponsors": "cosponsors_119_hr_22.json",
        "actions": "actions_119_hr_1.json",
        "subjects": "subjects_119_hr_1.json",
        "titles": "titles_119_hr_1.json",
        "summaries": "summaries_119_hr_1.json",
    }
    for section, name in section_fixtures.items():
        payload = json.loads((fixtures / name).read_text())
        (storage_dir / enrichment_jsonl_name(section)).write_text(
            _envelope(payload, {"congress": 119, "bill_type": "hr", "bill_number": 1}),
            encoding="utf-8",
        )

    db = tmp_path / "test.db"
    SqliteStorage(db).close()
    load_bills(storage_dir=storage_dir, db_path=db)
    index_bills(db_path=db)

    # Seed two Members so the Member-profile cross-link assertions can
    # resolve names — B001302 cosponsored the enriched bill, R000614
    # sponsored the tier-1-only bill.
    with SqliteStorage(db, load_vec=False) as storage:
        storage.upsert_member(
            Member(
                bioguide_id="B001302",
                first_name="Dan",
                last_name="Bishop",
                display_name="Dan Bishop",
            ),
            [
                Term(
                    bioguide_id="B001302",
                    congress=119,
                    chamber="house",
                    state="NC",
                    district=8,
                    party="Republican",
                    start_date="2025-01-03",
                ),
            ],
            fetched_at=fetched_at,
        )
        storage.upsert_member(
            Member(
                bioguide_id="R000614",
                first_name="Chip",
                last_name="Roy",
                display_name="Chip Roy",
            ),
            [
                Term(
                    bioguide_id="R000614",
                    congress=119,
                    chamber="house",
                    state="TX",
                    district=21,
                    party="Republican",
                    start_date="2025-01-03",
                ),
            ],
            fetched_at=fetched_at,
        )

    class _StubResp:
        def __init__(self, vectors: list[list[float]]) -> None:
            self.data = [type("D", (), {"embedding": v})() for v in vectors]

    class _StubEmb:
        def create(self, *, model: str, input: list[str]) -> _StubResp:
            return _StubResp([[0.5] * EMBEDDING_DIM for _ in input])

    class _Stub:
        embeddings = _StubEmb()

    app = create_app(db, embedder=Embedder(_Stub()))
    client = TestClient(app, raise_server_exceptions=False)

    # Enriched bill renders live sections.
    resp = client.get("/bills/119/hr/1")
    assert resp.status_code == 200
    assert "Cosponsors not yet fetched" not in resp.text
    # First cosponsor bioguide id from the fixture should be present.
    assert "B001302" in resp.text
    # First action text from the fixture.
    assert "Became Public Law No: 119-1." in resp.text
    # Subject chip.
    assert "Pipelines" in resp.text

    # Tier-1-only bill shows the empty-state for every tier-2 section.
    resp = client.get("/bills/119/hr/22")
    assert resp.status_code == 200
    assert "Cosponsors not yet fetched" in resp.text
    assert "Action history not yet fetched" in resp.text
    assert "Subjects not yet fetched" in resp.text
    assert "Titles not yet fetched" in resp.text
    assert "Summaries not yet fetched" in resp.text

    # Cosponsor of the enriched bill: Cosponsored bills section lists it.
    resp = client.get("/members/B001302")
    assert resp.status_code == 200
    body = resp.text
    assert "Cosponsored bills" in body
    assert "Lower Energy Costs Act" in body
    assert "No cosponsored bills indexed" not in body

    # Sponsor of the tier-1-only bill: Cosponsored bills section is empty.
    resp = client.get("/members/R000614")
    assert resp.status_code == 200
    body = resp.text
    assert "Cosponsored bills" in body
    assert "No cosponsored bills indexed" in body


def test_votes_end_to_end(tmp_path: Path) -> None:
    """Phase 3a smoke: write synthetic votes JSONL, load + index, poke routes."""
    fixtures = Path(__file__).parent / "fixtures" / "api" / "votes"
    storage_dir = tmp_path / "data"
    storage_dir.mkdir()
    fetched_at = datetime(2026, 5, 26, 14, 0, 0, tzinfo=UTC).isoformat()

    def _env(payload: dict, roll: int) -> str:
        return (
            json.dumps(
                {
                    "fetched_at": fetched_at,
                    "key": {
                        "chamber": "house",
                        "congress": 119,
                        "session": 1,
                        "roll_number": roll,
                    },
                    "payload": payload,
                }
            )
            + "\n"
        )

    detail = json.loads((fixtures / "detail_house_119_1_240.json").read_text())["houseRollCallVote"]
    members = json.loads((fixtures / "members_house_119_1_240.json").read_text())[
        "houseRollCallVoteMemberVotes"
    ]
    (storage_dir / HOUSE_VOTES_JSONL_NAME).write_text(_env(detail, 240), encoding="utf-8")
    (storage_dir / HOUSE_VOTE_POSITIONS_JSONL_NAME).write_text(_env(members, 240), encoding="utf-8")

    # Phase 3b: also seed a Senate vote + roster snapshot so the loader
    # exercises the Senate branch and the web layer renders live data.
    senate_fixtures = Path(__file__).parent / "fixtures" / "senate"
    senate_detail = (senate_fixtures / "detail_119_1_00007_bill.xml").read_text()
    senate_roster = (senate_fixtures / "senators_cfm.xml").read_text()

    def _senate_env(payload: str, roll: int) -> str:
        return (
            json.dumps(
                {
                    "fetched_at": fetched_at,
                    "key": {
                        "chamber": "senate",
                        "congress": 119,
                        "session": 1,
                        "roll_number": roll,
                    },
                    "payload": payload,
                }
            )
            + "\n"
        )

    def _roster_env(payload: str) -> str:
        return (
            json.dumps(
                {
                    "fetched_at": fetched_at,
                    "key": {"source": "senators_cfm"},
                    "payload": payload,
                }
            )
            + "\n"
        )

    (storage_dir / "senate_votes.jsonl").write_text(_senate_env(senate_detail, 7), encoding="utf-8")
    (storage_dir / "senate_roster.jsonl").write_text(_roster_env(senate_roster), encoding="utf-8")

    db = tmp_path / "test.db"
    SqliteStorage(db).close()
    load_stats = load_votes(storage_dir=storage_dir, db_path=db)
    assert load_stats.votes_written == 2  # one House + one Senate
    index_votes(db_path=db)

    class _StubResp:
        def __init__(self, vectors: list[list[float]]) -> None:
            self.data = [type("D", (), {"embedding": v})() for v in vectors]

    class _StubEmb:
        def create(self, *, model: str, input: list[str]) -> _StubResp:
            return _StubResp([[0.5] * EMBEDDING_DIM for _ in input])

    class _Stub:
        embeddings = _StubEmb()

    app = create_app(db, embedder=Embedder(_Stub()))
    client = TestClient(app, raise_server_exceptions=False)

    for path in (
        "/votes",
        "/votes/house/119/1/240",
        "/votes/senate/119/1/7",
        "/about/methodology",
    ):
        resp = client.get(path)
        assert resp.status_code == 200, path

    # Phase-3b placeholder strings are gone from the Senate Vote profile.
    resp = client.get("/votes/senate/119/1/7")
    assert "Phase 3b" not in resp.text
