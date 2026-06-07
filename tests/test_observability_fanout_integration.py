"""End-to-end fan-out: the text/senate/api clients record into a live
``scrape_run`` that flushes to SQLite, and a normal run produces the expected
source buckets with **no** ``*:unmatched`` bucket (ADR 0021, PR 2 acceptance).

These mirror the cross-source shapes the Proceedings (api + text) and Votes
(api + senate) scrapers exercise, driving the real chokepoints through mocked
transports rather than re-running a whole CLI pipeline.
"""

import json
from collections.abc import Callable
from pathlib import Path

import httpx

from concord.api import Client
from concord.observability import current_run_id, scrape_run
from concord.senate_xml import SenateClient
from concord.storage.sqlite import SqliteStorage
from concord.text import AdaptiveThrottle, fetch_text

ARTICLE_URL = (
    "https://www.congress.gov/119/crec/2026/05/22/172/88/modified/CREC-2026-05-22-pt1-PgD551-6.htm"
)


def _read_run(db_path: Path, run_id: str) -> dict[str, object]:
    storage = SqliteStorage(db_path, load_vec=False)
    try:
        row = storage.get_run(run_id)
        assert row is not None
        return dict(row)
    finally:
        storage.close()


def _api_client(handler: Callable[[httpx.Request], httpx.Response]) -> Client:
    return Client(api_key="test-key", transport=httpx.MockTransport(handler), sleep=lambda _s: None)


def _ok_json() -> httpx.Response:
    return httpx.Response(
        200,
        content=b'{"dailyCongressionalRecord": [], "pagination": {"count": 0}}',
        headers={"content-type": "application/json"},
    )


def _text_client(body: str) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200, text=body)))


def _senate_client(xml: bytes) -> SenateClient:
    handler = lambda _r: httpx.Response(  # noqa: E731 - terse mock handler
        200, content=xml, headers={"content-type": "application/xml"}
    )
    return SenateClient(transport=httpx.MockTransport(handler), sleep=lambda _s: None)


def test_proceedings_run_carries_both_api_and_text_buckets(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    with scrape_run(entity="proceedings", command="scrape proceedings", db_path=db_path):
        run_id = current_run_id()
        assert run_id is not None
        with _api_client(lambda _r: _ok_json()) as api:
            api.list_issues()
        with _text_client("<pre>hello world</pre>") as http:
            fetch_text(ARTICLE_URL, http, throttle=AdaptiveThrottle(sleep=lambda _s: None))

    row = _read_run(db_path, run_id)
    counts = json.loads(row["success_counts"])
    assert counts == {"api:daily-record/list": 1, "text:article": 1}
    # A clean run never falls back to the unmatched sentinel.
    assert row["unmatched_sample"] is None  # empty sample stored as NULL
    assert row["status"] == "ok"


def test_votes_run_carries_both_api_and_senate_buckets(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    with scrape_run(entity="votes", command="scrape votes", db_path=db_path):
        run_id = current_run_id()
        assert run_id is not None
        with _api_client(lambda _r: _ok_json()) as api:
            api.list_issues()
        with _senate_client(b"<doc></doc>") as senate:
            senate.get_current_senators_xml()

    row = _read_run(db_path, run_id)
    counts = json.loads(row["success_counts"])
    assert counts == {"api:daily-record/list": 1, "senate:roster": 1}
    assert row["unmatched_sample"] is None  # empty sample stored as NULL
    assert row["status"] == "ok"
