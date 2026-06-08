"""Tests for the pipeline orchestrator.

A mix of fast unit tests (with stub fetch + in-memory storage) and one
integration test that wires the real API client, real text fetcher, and
JsonlStorage together via httpx.MockTransport, using the JSON and HTML
fixtures captured for #18 and #19.
"""

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from concord.api import Client
from concord.models.proceedings import Article, Issue, Proceeding
from concord.pipeline.load_proceedings import pull
from concord.storage.jsonl import JsonlStorage
from concord.text import TextFetchError, fetch_text

FIXED_NOW = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)


def _now() -> datetime:
    return FIXED_NOW


# ---------------------------------------------------------------------------
# Fakes for the unit-style tests. Keep them dependency-free: an in-memory
# storage and a stubbed Client subclass that returns whatever lists you set.
# ---------------------------------------------------------------------------


class _InMemoryStorage:
    def __init__(self) -> None:
        self.writes: list[Proceeding] = []
        self._seen: set[str] = set()

    def has(self, granule_id: str) -> bool:
        return granule_id in self._seen

    def write(self, proceeding: Proceeding) -> None:
        if proceeding.granule_id in self._seen:
            return
        self.writes.append(proceeding)
        self._seen.add(proceeding.granule_id)


class _StubClient:
    """Minimal Client substitute: hand-feed pages and per-issue article lists."""

    def __init__(
        self,
        *,
        pages: list[list[Issue]],
        articles_by_issue: dict[tuple[int, int], list[Article]],
    ) -> None:
        self._pages = pages
        self._articles = articles_by_issue
        self.list_issues_calls: list[tuple[int, int]] = []
        self.list_articles_calls: list[tuple[int, int]] = []

    def list_issues(self, *, limit: int = 50, offset: int = 0) -> tuple[list[Issue], int | None]:
        self.list_issues_calls.append((limit, offset))
        page_idx = offset // limit if limit else 0
        if page_idx >= len(self._pages):
            return [], None
        next_offset = offset + limit if page_idx + 1 < len(self._pages) else None
        return self._pages[page_idx], next_offset

    def list_articles(self, volume: int, issue_number: int) -> list[Article]:
        self.list_articles_calls.append((volume, issue_number))
        return self._articles.get((volume, issue_number), [])


def _issue(issue_date: str, *, volume: int = 172, issue_number: int = 1) -> Issue:
    return Issue(
        issue_date=issue_date,
        congress=119,
        session=2,
        volume=volume,
        issue_number=issue_number,
        update_date="2026-05-23T06:44:22Z",
    )


def _article(granule_id: str) -> Article:
    text_url = f"https://www.congress.gov/119/crec/2026/05/22/172/88/modified/{granule_id}.htm"
    pdf_url = f"https://www.congress.gov/119/crec/2026/05/22/172/88/{granule_id}.pdf"
    return Article(
        section="Daily Digest",
        title="t",
        start_page="D1",
        end_page="D1",
        text_url=text_url,
        pdf_url=pdf_url,
        granule_id=granule_id,
    )


def _stub_fetch(_url: str) -> str:
    return "fetched body"


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


class TestRangeFiltering:
    def test_empty_range_returns_zero(self) -> None:
        client = _StubClient(pages=[], articles_by_issue={})
        storage = _InMemoryStorage()
        result = pull(
            date(2026, 5, 1),
            date(2026, 4, 30),  # end < start
            client=client,  # type: ignore[arg-type]
            fetch=_stub_fetch,
            storage=storage,
            now=_now,
        )
        assert result.written == 0
        # No API calls were made — the range was empty.
        assert client.list_issues_calls == []

    def test_in_range_issue_is_processed(self) -> None:
        issue = _issue("2026-05-22", volume=172, issue_number=88)
        articles = [_article(f"CREC-2026-05-22-pt1-PgS{n:04d}") for n in range(3)]
        client = _StubClient(pages=[[issue]], articles_by_issue={(172, 88): articles})
        storage = _InMemoryStorage()
        result = pull(
            date(2026, 5, 22),
            date(2026, 5, 22),
            client=client,  # type: ignore[arg-type]
            fetch=_stub_fetch,
            storage=storage,
            now=_now,
        )
        assert result.written == 3
        assert len(storage.writes) == 3
        # fetched_at is stamped from the injected clock.
        assert all(p.fetched_at == FIXED_NOW for p in storage.writes)
        # The issue's article endpoint was hit exactly once.
        assert client.list_articles_calls == [(172, 88)]

    def test_out_of_range_issues_skip_article_fetch(self) -> None:
        # Two issues in the page: one in range, one out. Only the in-range
        # one should trigger list_articles.
        in_range = _issue("2026-05-22", issue_number=88)
        too_old = _issue("2026-05-01", issue_number=80)
        client = _StubClient(
            pages=[[in_range, too_old]],
            articles_by_issue={(172, 88): [_article("CREC-2026-05-22-pt1-PgS0001")]},
        )
        storage = _InMemoryStorage()
        pull(
            date(2026, 5, 15),
            date(2026, 5, 22),
            client=client,  # type: ignore[arg-type]
            fetch=_stub_fetch,
            storage=storage,
            now=_now,
        )
        # Article endpoint hit only for the in-range issue.
        assert client.list_articles_calls == [(172, 88)]


class TestDedup:
    def test_skips_already_stored_articles(self) -> None:
        issue = _issue("2026-05-22", issue_number=88)
        articles = [_article(f"CREC-2026-05-22-pt1-PgS{n:04d}") for n in range(3)]
        client = _StubClient(pages=[[issue]], articles_by_issue={(172, 88): articles})

        storage = _InMemoryStorage()
        # Pre-seed one of the granule IDs as already stored.
        storage._seen.add(articles[1].granule_id)

        fetch_calls: list[str] = []

        def tracking_fetch(url: str) -> str:
            fetch_calls.append(url)
            return "body"

        result = pull(
            date(2026, 5, 22),
            date(2026, 5, 22),
            client=client,  # type: ignore[arg-type]
            fetch=tracking_fetch,
            storage=storage,
            now=_now,
        )
        # Two new writes, one skip.
        assert result.written == 2
        assert result.skipped == 1
        # fetch was NOT called for the already-stored article.
        assert len(fetch_calls) == 2
        assert articles[1].text_url not in [httpx.URL(u) for u in fetch_calls]


class TestFetchFailures:
    def test_single_fetch_failure_does_not_abort_run(self) -> None:
        issue = _issue("2026-05-22", issue_number=88)
        articles = [_article(f"CREC-2026-05-22-pt1-PgS{n:04d}") for n in range(3)]
        client = _StubClient(pages=[[issue]], articles_by_issue={(172, 88): articles})
        storage = _InMemoryStorage()

        def flaky_fetch(url: str) -> str:
            if articles[1].granule_id in url:
                raise TextFetchError("simulated timeout", status_code=None)
            return "body"

        result = pull(
            date(2026, 5, 22),
            date(2026, 5, 22),
            client=client,  # type: ignore[arg-type]
            fetch=flaky_fetch,
            storage=storage,
            now=_now,
        )
        assert result.written == 2  # the two that succeeded
        assert result.skipped == 0
        assert result.failed == 1  # the one that timed out
        # Storage didn't accept the failed article — re-running can retry it.
        assert all(p.granule_id != articles[1].granule_id for p in storage.writes)

    def test_failed_count_in_progress_event(self) -> None:
        issue = _issue("2026-05-22", issue_number=88)
        articles = [_article(f"CREC-2026-05-22-pt1-PgS{n:04d}") for n in range(3)]
        client = _StubClient(pages=[[issue]], articles_by_issue={(172, 88): articles})
        storage = _InMemoryStorage()

        def flaky_fetch(url: str) -> str:
            if articles[0].granule_id in url:
                raise TextFetchError("simulated", status_code=None)
            return "body"

        events: list[Any] = []
        pull(
            date(2026, 5, 22),
            date(2026, 5, 22),
            client=client,  # type: ignore[arg-type]
            fetch=flaky_fetch,
            storage=storage,
            progress=events.append,
            now=_now,
        )
        assert len(events) == 1
        assert events[0].issue_failed == 1
        assert events[0].issue_written == 2
        assert events[0].total_failed == 1


class TestLimit:
    def test_limit_caps_writes(self) -> None:
        issue = _issue("2026-05-22", issue_number=88)
        articles = [_article(f"CREC-2026-05-22-pt1-PgS{n:04d}") for n in range(10)]
        client = _StubClient(pages=[[issue]], articles_by_issue={(172, 88): articles})
        storage = _InMemoryStorage()

        result = pull(
            date(2026, 5, 22),
            date(2026, 5, 22),
            client=client,  # type: ignore[arg-type]
            fetch=_stub_fetch,
            storage=storage,
            limit=4,
            now=_now,
        )
        assert result.written == 4
        assert len(storage.writes) == 4


class TestPagination:
    def test_walks_all_pages(self) -> None:
        """Issues are not strictly date-ordered (recent updates bubble up),
        so the orchestrator must walk every page — early termination on the
        first older date would silently drop later-page in-range issues.
        """
        # Page 1: one in-range. Page 2: another in-range that comes "after"
        # in pagination but is also in the target window.
        page1 = [_issue("2026-05-22", issue_number=88)]
        page2 = [_issue("2026-05-21", issue_number=87)]
        articles_88 = [_article("CREC-2026-05-22-pt1-PgS0001")]
        articles_87 = [_article("CREC-2026-05-21-pt1-PgS0001")]
        client = _StubClient(
            pages=[page1, page2],
            articles_by_issue={(172, 88): articles_88, (172, 87): articles_87},
        )
        storage = _InMemoryStorage()
        result = pull(
            date(2026, 5, 21),
            date(2026, 5, 22),
            client=client,  # type: ignore[arg-type]
            fetch=_stub_fetch,
            storage=storage,
            now=_now,
        )
        assert result.written == 2
        # Both pages were requested.
        assert len(client.list_issues_calls) == 2


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


def _api_mock_handler(fixtures_dir: Path) -> Callable[[httpx.Request], httpx.Response]:
    """Route real api.congress.gov paths to recorded fixtures."""
    list_payload = json.loads((fixtures_dir / "api/list_issues_page1.json").read_text())
    articles_payload = json.loads((fixtures_dir / "api/articles_172_88.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v3/daily-congressional-record":
            # Modify fixture to claim no next page (we only have one page of
            # fixture data; the orchestrator should stop after this).
            modified = {
                **list_payload,
                "pagination": {"count": list_payload["pagination"]["count"]},
            }
            return httpx.Response(200, json=modified)
        if path.endswith("/articles"):
            # list_articles paginates; serve fixture on first page only and
            # an empty terminating page after.
            offset = int(request.url.params.get("offset", "0"))
            if offset == 0:
                return httpx.Response(200, json=articles_payload)
            return httpx.Response(200, json={"articles": [], "pagination": {"count": 20}})
        return httpx.Response(404)

    return handler


def _text_mock_handler() -> Callable[[httpx.Request], httpx.Response]:
    """Serve a generic Congressional Record HTML body for any article URL."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<pre>\n\n[Body]\nIntegration test fixture text.\n\n</pre>",
            headers={"content-type": "text/html"},
        )

    return handler


@pytest.fixture
def wired_components(
    fixtures_dir: Path, tmp_path: Path
) -> tuple[Client, Callable[[str], str], JsonlStorage]:
    """Build real Client + real text fetcher + JsonlStorage, all wired to mocks."""
    api_transport = httpx.MockTransport(_api_mock_handler(fixtures_dir))
    text_transport = httpx.MockTransport(_text_mock_handler())
    text_http = httpx.Client(transport=text_transport)
    client = Client(api_key="test", transport=api_transport)
    storage = JsonlStorage(tmp_path / "out.jsonl")
    return client, lambda url: fetch_text(url, text_http), storage


class TestIntegration:
    def test_end_to_end_one_day(
        self, wired_components: tuple[Client, Callable[[str], str], JsonlStorage]
    ) -> None:
        client, fetch, storage = wired_components
        with client:
            result = pull(
                date(2026, 5, 22),
                date(2026, 5, 22),
                client=client,
                fetch=fetch,
                storage=storage,
                now=_now,
            )

        # The articles fixture has 6 + 3 + 11 = 20 articles.
        assert result.written == 20
        assert result.skipped == 0
        assert len(storage) == 20

        # Every line on disk parses back into a Proceeding.
        lines = storage.path.read_text().strip().splitlines()
        assert len(lines) == 20
        proceedings = [Proceeding.model_validate_json(line) for line in lines]

        # All proceedings carry the issue metadata from issue 172/88.
        assert all(p.volume == 172 for p in proceedings)
        assert all(p.issue_number == 88 for p in proceedings)
        assert all(p.issue_date == date(2026, 5, 22) for p in proceedings)
        # All carry the fetched text body.
        assert all("Integration test fixture text." in p.text for p in proceedings)
        # All carry the injected timestamp.
        assert all(p.fetched_at == FIXED_NOW for p in proceedings)

    def test_re_running_is_idempotent(
        self, wired_components: tuple[Client, Callable[[str], str], JsonlStorage]
    ) -> None:
        client, fetch, storage = wired_components
        with client:
            first = pull(
                date(2026, 5, 22),
                date(2026, 5, 22),
                client=client,
                fetch=fetch,
                storage=storage,
                now=_now,
            )
            second = pull(
                date(2026, 5, 22),
                date(2026, 5, 22),
                client=client,
                fetch=fetch,
                storage=storage,
                now=_now,
            )
        assert first.written == 20
        assert first.skipped == 0
        assert second.written == 0  # Everything already stored.
        assert second.skipped == 20  # ...and all of them counted as skipped.
        # File has exactly 20 lines, not 40.
        assert len(storage.path.read_text().strip().splitlines()) == 20


# ---------------------------------------------------------------------------
# Resume / crash recovery
# ---------------------------------------------------------------------------


class TestResume:
    def test_crash_mid_pull_is_recoverable(
        self,
        fixtures_dir: Path,
        tmp_path: Path,
    ) -> None:
        """Simulate the operator's worst-case: kill -9 partway through.

        Setup: integration components but the fetch callable raises on the
        N-th article. Run pull, catch the exception, count what was written,
        then run pull again with a working fetch and verify no duplicates
        and no missing records.
        """
        # Build two clients (one will be re-created for the resume) sharing
        # the same JSONL path.
        out = tmp_path / "out.jsonl"

        # ---- attempt 1: fetch dies after 7 articles
        api_transport_1 = httpx.MockTransport(_api_mock_handler(fixtures_dir))
        text_transport_1 = httpx.MockTransport(_text_mock_handler())
        text_http_1 = httpx.Client(transport=text_transport_1)
        client_1 = Client(api_key="test", transport=api_transport_1, sleep=lambda _: None)
        storage_1 = JsonlStorage(out)

        call_count = {"n": 0}

        def crashing_fetch(url: str) -> str:
            call_count["n"] += 1
            if call_count["n"] > 7:
                raise RuntimeError("simulated crash")
            return fetch_text(url, text_http_1)

        with client_1, pytest.raises(RuntimeError, match="simulated crash"):
            pull(
                date(2026, 5, 22),
                date(2026, 5, 22),
                client=client_1,
                fetch=crashing_fetch,
                storage=storage_1,
                now=_now,
            )
        text_http_1.close()

        # 7 articles made it onto disk before the crash.
        written_during_crash = len(out.read_text().strip().splitlines())
        assert written_during_crash == 7

        # ---- attempt 2: fresh components, working fetch
        api_transport_2 = httpx.MockTransport(_api_mock_handler(fixtures_dir))
        text_transport_2 = httpx.MockTransport(_text_mock_handler())
        text_http_2 = httpx.Client(transport=text_transport_2)
        client_2 = Client(api_key="test", transport=api_transport_2, sleep=lambda _: None)
        # Re-opening the same JSONL path rebuilds the seen-set from disk.
        storage_2 = JsonlStorage(out)

        with client_2:
            result = pull(
                date(2026, 5, 22),
                date(2026, 5, 22),
                client=client_2,
                fetch=lambda url: fetch_text(url, text_http_2),
                storage=storage_2,
                now=_now,
            )
        text_http_2.close()

        # Resume wrote only the remaining articles, skipped the 7 already there.
        assert result.written == 13  # 20 total - 7 already done
        assert result.skipped == 7

        # Final state: exactly 20 distinct records, no duplicates.
        lines = out.read_text().strip().splitlines()
        assert len(lines) == 20
        granule_ids = {Proceeding.model_validate_json(line).granule_id for line in lines}
        assert len(granule_ids) == 20  # all unique


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------


class TestProgress:
    def test_callback_invoked_once_per_in_range_issue(self) -> None:
        # Two in-range issues, one out-of-range.
        in_1 = _issue("2026-05-22", issue_number=88)
        in_2 = _issue("2026-05-21", issue_number=87)
        out_of_range = _issue("2026-05-01", issue_number=80)
        articles_88 = [_article(f"CREC-2026-05-22-pt1-PgS{n:04d}") for n in range(3)]
        articles_87 = [_article(f"CREC-2026-05-21-pt1-PgS{n:04d}") for n in range(2)]
        client = _StubClient(
            pages=[[in_1, in_2, out_of_range]],
            articles_by_issue={(172, 88): articles_88, (172, 87): articles_87},
        )
        storage = _InMemoryStorage()

        events: list[Any] = []
        pull(
            date(2026, 5, 21),
            date(2026, 5, 22),
            client=client,  # type: ignore[arg-type]
            fetch=_stub_fetch,
            storage=storage,
            progress=events.append,
            now=_now,
        )
        # Two callbacks: one per in-range issue. Out-of-range was skipped
        # without invoking progress.
        assert len(events) == 2
        # First call: issue 88 with 3 writes.
        assert events[0].issue.issue_number == 88
        assert events[0].issue_written == 3
        assert events[0].issue_skipped == 0
        assert events[0].total_written == 3
        # Second call: issue 87 with 2 more writes; totals accumulate.
        assert events[1].issue.issue_number == 87
        assert events[1].issue_written == 2
        assert events[1].total_written == 5

    def test_callback_reports_skipped(self) -> None:
        issue = _issue("2026-05-22", issue_number=88)
        articles = [_article(f"CREC-2026-05-22-pt1-PgS{n:04d}") for n in range(3)]
        client = _StubClient(pages=[[issue]], articles_by_issue={(172, 88): articles})
        storage = _InMemoryStorage()
        # Pre-seed one as already stored.
        storage._seen.add(articles[1].granule_id)

        events: list[Any] = []
        pull(
            date(2026, 5, 22),
            date(2026, 5, 22),
            client=client,  # type: ignore[arg-type]
            fetch=_stub_fetch,
            storage=storage,
            progress=events.append,
            now=_now,
        )
        assert len(events) == 1
        assert events[0].issue_written == 2
        assert events[0].issue_skipped == 1

    def test_progress_optional(self) -> None:
        """No progress kwarg → no events, no errors."""
        issue = _issue("2026-05-22", issue_number=88)
        client = _StubClient(
            pages=[[issue]],
            articles_by_issue={(172, 88): [_article("CREC-2026-05-22-pt1-PgS0001")]},
        )
        storage = _InMemoryStorage()
        result = pull(
            date(2026, 5, 22),
            date(2026, 5, 22),
            client=client,  # type: ignore[arg-type]
            fetch=_stub_fetch,
            storage=storage,
            now=_now,
        )
        assert result.written == 1
