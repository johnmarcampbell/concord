"""Smoke tests proving the project skeleton is wired up correctly."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import concord


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
    from fastapi.testclient import TestClient

    from concord.embedding import EMBEDDING_DIM, Embedder
    from concord.pipeline.index_members import index as index_members
    from concord.pipeline.load_members import load as load_members
    from concord.web.app import create_app

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
    from concord.storage.sqlite import SqliteStorage as _SqliteStorage

    _SqliteStorage(db).close()

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
