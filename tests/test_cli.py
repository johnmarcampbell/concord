"""Tests for the ``concord`` CLI.

The CLI's responsibilities are: parse flags, surface clear errors on missing
API key, wire the pipeline, and format the success summary. The orchestrator
itself is covered by tests/test_pipeline.py — here we stub it out and verify
the CLI does the right thing around it.
"""

import io
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest
import uvicorn
from typer.testing import CliRunner

import concord.cli as cli_module
import concord.cli.proceedings as cli_proceedings_module
import concord.scraper.proceedings as scraper_proceedings_module
from concord.api import ENV_API_KEY, ApiError, Client
from concord.models import Article, Issue, Proceeding
from concord.pipeline.index_proceedings import IndexResult
from concord.pipeline.load_proceedings import PullResult
from concord.scraper.votes import SENATE_ROSTER_JSONL_NAME, SENATE_VOTES_JSONL_NAME
from concord.senate_xml import SenateClient
from concord.storage import SqliteStorage

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _strip(text: str) -> str:
    """Drop ANSI color/style escapes so assertions don't depend on the terminal.

    Locally, CliRunner produces plain text; on CI runners Rich detects color
    support and injects escapes that break naive substring checks.
    """
    return _ANSI.sub("", text)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def with_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_API_KEY, "test-key")


@pytest.fixture
def stub_pull(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace ``concord.scraper.proceedings.pull`` with a recorder; return the call log."""
    calls: list[dict[str, Any]] = []

    def fake_pull(start: date, end: date, **kwargs: Any) -> PullResult:
        calls.append({"start": start, "end": end, **kwargs})
        return PullResult(written=3, skipped=1)

    monkeypatch.setattr(scraper_proceedings_module, "pull", fake_pull)
    return calls


# -- help & arg parsing -------------------------------------------------------


class TestHelp:
    def test_help_lists_all_flags(self, runner: CliRunner) -> None:
        result = runner.invoke(cli_module.app, ["scrape", "proceedings", "--help"])
        assert result.exit_code == 0
        # All four flags must appear in --help.
        plain = _strip(result.output)
        for flag in ["--from", "--to", "--storage", "--limit"]:
            assert flag in plain


class TestArgParsing:
    def test_pull_parses_dates(
        self,
        runner: CliRunner,
        with_api_key: None,
        stub_pull: list[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        result = runner.invoke(
            cli_module.app,
            [
                "scrape",
                "proceedings",
                "--from",
                "2026-05-22",
                "--to",
                "2026-05-22",
                "--storage",
                str(tmp_path / "out.jsonl"),
            ],
        )
        assert result.exit_code == 0, result.output
        assert len(stub_pull) == 1
        call = stub_pull[0]
        assert call["start"] == date(2026, 5, 22)
        assert call["end"] == date(2026, 5, 22)

    def test_pull_passes_limit(
        self,
        runner: CliRunner,
        with_api_key: None,
        stub_pull: list[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        result = runner.invoke(
            cli_module.app,
            [
                "scrape",
                "proceedings",
                "--from",
                "2026-05-01",
                "--to",
                "2026-05-22",
                "--storage",
                str(tmp_path / "out.jsonl"),
                "--limit",
                "5",
            ],
        )
        assert result.exit_code == 0, result.output
        assert stub_pull[0]["limit"] == 5

    def test_pull_progress_flag_passes_callback(
        self,
        runner: CliRunner,
        with_api_key: None,
        stub_pull: list[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        result = runner.invoke(
            cli_module.app,
            [
                "scrape",
                "proceedings",
                "--from",
                "2026-05-22",
                "--to",
                "2026-05-22",
                "--storage",
                str(tmp_path / "out.jsonl"),
                "--progress",
            ],
        )
        assert result.exit_code == 0, result.output
        # With --progress, a callback is wired up.
        assert stub_pull[0]["progress"] is not None

    def test_pull_progress_on_by_default(
        self,
        runner: CliRunner,
        with_api_key: None,
        stub_pull: list[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        # Long-running pulls benefit from feedback; --no-progress opts out.
        result = runner.invoke(
            cli_module.app,
            [
                "scrape",
                "proceedings",
                "--from",
                "2026-05-22",
                "--to",
                "2026-05-22",
                "--storage",
                str(tmp_path / "out.jsonl"),
            ],
        )
        assert result.exit_code == 0, result.output
        assert stub_pull[0]["progress"] is not None

    def test_pull_no_progress_when_disabled(
        self,
        runner: CliRunner,
        with_api_key: None,
        stub_pull: list[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        result = runner.invoke(
            cli_module.app,
            [
                "scrape",
                "proceedings",
                "--from",
                "2026-05-22",
                "--to",
                "2026-05-22",
                "--storage",
                str(tmp_path / "out.jsonl"),
                "--no-progress",
            ],
        )
        assert result.exit_code == 0, result.output
        assert stub_pull[0]["progress"] is None

    def test_pull_default_storage_path(
        self,
        runner: CliRunner,
        with_api_key: None,
        stub_pull: list[dict[str, Any]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Run with no --storage flag; default should land in CWD.
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            cli_module.app,
            ["scrape", "proceedings", "--from", "2026-05-22", "--to", "2026-05-22"],
        )
        assert result.exit_code == 0, result.output
        # Default path is ./proceedings.jsonl
        assert "proceedings.jsonl" in _strip(result.output)

    @pytest.mark.parametrize("bad_date", ["yesterday", "05/22/2026", "2026-13-01"])
    def test_pull_rejects_bad_date(
        self,
        runner: CliRunner,
        with_api_key: None,
        stub_pull: list[dict[str, Any]],
        bad_date: str,
    ) -> None:
        result = runner.invoke(
            cli_module.app,
            ["scrape", "proceedings", "--from", bad_date, "--to", "2026-05-22"],
        )
        assert result.exit_code != 0
        # stub_pull should not have been called.
        assert len(stub_pull) == 0


# -- success summary ---------------------------------------------------------


class TestSuccessOutput:
    def test_prints_written_and_skipped_counts(
        self,
        runner: CliRunner,
        with_api_key: None,
        stub_pull: list[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        out_path = tmp_path / "out.jsonl"
        result = runner.invoke(
            cli_module.app,
            [
                "scrape",
                "proceedings",
                "--from",
                "2026-05-22",
                "--to",
                "2026-05-22",
                "--storage",
                str(out_path),
            ],
        )
        assert result.exit_code == 0, result.output
        # PullResult(written=3, skipped=1) from the stub.
        plain = _strip(result.output)
        assert "Wrote 3 new proceedings" in plain
        assert str(out_path) in plain
        assert "skipped 1" in plain


# -- missing API key ---------------------------------------------------------


class TestMissingApiKey:
    def test_missing_key_exits_cleanly(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Ensure no key in env.
        monkeypatch.delenv(ENV_API_KEY, raising=False)

        result = runner.invoke(
            cli_module.app,
            [
                "scrape",
                "proceedings",
                "--from",
                "2026-05-22",
                "--to",
                "2026-05-22",
                "--storage",
                str(tmp_path / "out.jsonl"),
            ],
        )
        # Non-zero exit, error message on stderr, no traceback.
        assert result.exit_code == 2
        plain = _strip(result.output) + _strip(result.stderr or "")
        # The error message mentions the env var so the user knows what to set.
        assert ENV_API_KEY in plain
        # Ensure we don't accidentally let an unhandled exception through.
        assert "Traceback" not in plain

    def test_apierror_during_client_init_surfaces_as_exit_code_2(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Even if Client() raises ApiError for some other reason, exit 2."""
        monkeypatch.setenv(ENV_API_KEY, "test")

        def boom(**_: Any) -> Any:
            raise ApiError("simulated init failure")

        monkeypatch.setattr(scraper_proceedings_module, "Client", boom)
        result = runner.invoke(
            cli_module.app,
            [
                "scrape",
                "proceedings",
                "--from",
                "2026-05-22",
                "--to",
                "2026-05-22",
                "--storage",
                str(tmp_path / "out.jsonl"),
            ],
        )
        assert result.exit_code == 2
        assert "simulated init failure" in _strip(result.output)


# -- `concord load` ----------------------------------------------------------


_PROCEEDING_LINE_TEMPLATE = (
    '{{"granule_id":"{gid}",'
    '"issue_date":"2026-05-22","congress":119,"session":2,"volume":172,'
    '"issue_number":88,"update_date":"2026-05-23T06:44:22Z",'
    '"section":"Daily Digest","title":"Sample {gid}",'
    '"start_page":"D551","end_page":"D552",'
    '"text_url":"https://www.congress.gov/119/crec/2026/05/22/172/88/modified/{gid}.htm",'
    '"pdf_url":"https://www.congress.gov/119/crec/2026/05/22/172/88/{gid}.pdf",'
    '"text":"body for {gid}",'
    '"fetched_at":"2026-05-24T00:00:00Z"}}'
)


def _write_jsonl(path: Path, granule_ids: list[str]) -> None:
    """Write a JSONL fixture with one valid Proceeding per granule_id."""
    lines = [_PROCEEDING_LINE_TEMPLATE.format(gid=gid) for gid in granule_ids]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestLoadCommand:
    def test_help_lists_all_flags(self, runner: CliRunner) -> None:
        result = runner.invoke(cli_module.app, ["load", "proceedings", "--help"])
        assert result.exit_code == 0
        plain = _strip(result.output)
        for flag in ["--jsonl", "--db", "--limit"]:
            assert flag in plain

    def test_loads_jsonl_into_sqlite(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        jsonl = tmp_path / "in.jsonl"
        db = tmp_path / "out.db"
        _write_jsonl(jsonl, ["CREC-2026-05-22-pt1-PgD551-1", "CREC-2026-05-22-pt1-PgD551-2"])

        result = runner.invoke(
            cli_module.app,
            ["load", "proceedings", "--jsonl", str(jsonl), "--db", str(db)],
        )
        assert result.exit_code == 0, result.output
        plain = _strip(result.output)
        assert "Loaded 2 new proceedings" in plain
        assert str(db) in plain

        # Verify the rows actually landed.
        with SqliteStorage(db) as storage:
            assert len(storage) == 2

    def test_idempotent_when_jsonl_unchanged(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        jsonl = tmp_path / "in.jsonl"
        db = tmp_path / "out.db"
        _write_jsonl(jsonl, ["CREC-2026-05-22-pt1-PgD551-1", "CREC-2026-05-22-pt1-PgD551-2"])

        args = ["load", "proceedings", "--jsonl", str(jsonl), "--db", str(db)]
        first = runner.invoke(cli_module.app, args)
        assert first.exit_code == 0
        second = runner.invoke(cli_module.app, args)
        assert second.exit_code == 0, second.output
        plain = _strip(second.output)
        assert "Loaded 0 new proceedings" in plain
        assert "skipped 2 already present" in plain

    def test_limit_caps_writes(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        jsonl = tmp_path / "in.jsonl"
        db = tmp_path / "out.db"
        _write_jsonl(
            jsonl,
            [
                "CREC-2026-05-22-pt1-PgD551-1",
                "CREC-2026-05-22-pt1-PgD551-2",
                "CREC-2026-05-22-pt1-PgD551-3",
                "CREC-2026-05-22-pt1-PgD551-4",
                "CREC-2026-05-22-pt1-PgD551-5",
            ],
        )
        result = runner.invoke(
            cli_module.app,
            ["load", "proceedings", "--jsonl", str(jsonl), "--db", str(db), "--limit", "2"],
        )
        assert result.exit_code == 0, result.output
        plain = _strip(result.output)
        assert "Loaded 2 new proceedings" in plain

        with SqliteStorage(db) as storage:
            assert len(storage) == 2

    def test_malformed_line_skipped(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        jsonl = tmp_path / "in.jsonl"
        db = tmp_path / "out.db"
        good_1 = _PROCEEDING_LINE_TEMPLATE.format(gid="CREC-2026-05-22-pt1-PgD551-1")
        good_2 = _PROCEEDING_LINE_TEMPLATE.format(gid="CREC-2026-05-22-pt1-PgD551-2")
        # Insert one truncated-JSON line and one missing-field line between
        # two valid lines.
        broken_json = '{"granule_id": "CREC-truncated", "text": "no closing'
        missing_fields = '{"granule_id": "CREC-missing-fields-only"}'
        jsonl.write_text(
            "\n".join([good_1, broken_json, missing_fields, good_2]) + "\n",
            encoding="utf-8",
        )

        result = runner.invoke(
            cli_module.app,
            ["load", "proceedings", "--jsonl", str(jsonl), "--db", str(db)],
        )
        assert result.exit_code == 0, result.output
        plain = _strip(result.output)
        # Two valid lines made it through.
        assert "Loaded 2 new proceedings" in plain
        # Two bad lines were called out.
        assert "2 malformed lines skipped" in plain

    def test_blank_lines_are_skipped(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        jsonl = tmp_path / "in.jsonl"
        db = tmp_path / "out.db"
        good = _PROCEEDING_LINE_TEMPLATE.format(gid="CREC-2026-05-22-pt1-PgD551-1")
        jsonl.write_text("\n\n" + good + "\n\n", encoding="utf-8")

        result = runner.invoke(
            cli_module.app,
            ["load", "proceedings", "--jsonl", str(jsonl), "--db", str(db)],
        )
        assert result.exit_code == 0, result.output
        # Blank lines aren't malformed — they're just skipped silently.
        assert "malformed" not in _strip(result.output)
        assert "Loaded 1 new proceedings" in _strip(result.output)

    def test_missing_jsonl_file_exits_cleanly(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        result = runner.invoke(
            cli_module.app,
            [
                "load",
                "proceedings",
                "--jsonl",
                str(tmp_path / "does-not-exist.jsonl"),
                "--db",
                str(tmp_path / "out.db"),
            ],
        )
        assert result.exit_code == 2
        plain = _strip(result.output) + _strip(result.stderr or "")
        assert "not found" in plain
        assert "Traceback" not in plain


# -- `concord index` ---------------------------------------------------------


def _seed_db_for_index(db_path: Path, granule_ids: list[str]) -> None:
    """Pre-populate a SQLite DB with proceedings so `concord index` has work to do."""
    with SqliteStorage(db_path) as storage:
        for gid in granule_ids:
            text_url = f"https://www.congress.gov/119/crec/2026/05/22/172/88/modified/{gid}.htm"
            pdf_url = f"https://www.congress.gov/119/crec/2026/05/22/172/88/{gid}.pdf"
            issue = Issue(
                issue_date="2026-05-22",
                congress=119,
                session=2,
                volume=172,
                issue_number=88,
                update_date="2026-05-23T06:44:22Z",
            )
            article = Article(
                section="Daily Digest",
                title=f"Sample {gid}",
                start_page="D1",
                end_page="D2",
                text_url=text_url,
                pdf_url=pdf_url,
                granule_id=gid,
            )
            storage.write(
                Proceeding.build(
                    issue=issue,
                    article=article,
                    text=f"Body of {gid} mentioning the senate floor today.",
                    fetched_at=datetime(2026, 5, 24, tzinfo=UTC),
                )
            )


class _FakeOpenAIData:
    def __init__(self, vec: list[float]) -> None:
        self.embedding = vec


class _FakeOpenAIResponse:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.data = [_FakeOpenAIData(v) for v in vectors]


class _FakeOpenAIEmbeddings:
    def create(self, *, model: str, input: list[str]) -> _FakeOpenAIResponse:
        return _FakeOpenAIResponse([[float(i)] * 1536 for i in range(len(input))])


class _FakeOpenAIClient:
    embeddings = _FakeOpenAIEmbeddings()


class TestIndexCommand:
    def test_help_lists_all_flags(self, runner: CliRunner) -> None:
        result = runner.invoke(cli_module.app, ["index", "proceedings", "--help"])
        assert result.exit_code == 0
        plain = _strip(result.output)
        for flag in ["--db", "--limit"]:
            assert flag in plain

    def test_missing_db_file_exits_cleanly(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(cli_module.ENV_OPENAI_API_KEY, "sk-test")
        result = runner.invoke(
            cli_module.app,
            ["index", "proceedings", "--db", str(tmp_path / "missing.db")],
        )
        assert result.exit_code == 2
        plain = _strip(result.output) + _strip(result.stderr or "")
        assert "not found" in plain
        assert "Traceback" not in plain

    def test_missing_openai_api_key_exits_cleanly(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(cli_module.ENV_OPENAI_API_KEY, raising=False)
        db = tmp_path / "out.db"
        _seed_db_for_index(db, ["CREC-2026-05-22-pt1-PgD551-1"])
        result = runner.invoke(cli_module.app, ["index", "proceedings", "--db", str(db)])
        assert result.exit_code == 2
        plain = _strip(result.output) + _strip(result.stderr or "")
        assert cli_module.ENV_OPENAI_API_KEY in plain
        assert "Traceback" not in plain

    def test_indexes_end_to_end(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(cli_module.ENV_OPENAI_API_KEY, "sk-test")
        db = tmp_path / "out.db"
        _seed_db_for_index(db, ["CREC-2026-05-22-pt1-PgD551-1", "CREC-2026-05-22-pt1-PgD551-2"])

        # Patch openai.OpenAI inside the CLI to return our fake.
        monkeypatch.setattr(openai, "OpenAI", lambda *a, **kw: _FakeOpenAIClient())

        result = runner.invoke(cli_module.app, ["index", "proceedings", "--db", str(db)])
        assert result.exit_code == 0, result.output
        plain = _strip(result.output)
        assert "Indexed:" in plain
        assert "chunked 2 new proceedings" in plain
        assert "embedded" in plain

        # Verify state on disk: chunks_vec has rows.
        with SqliteStorage(db) as storage:
            count = storage.connection.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0]
            assert count > 0


# -- `concord serve` ----------------------------------------------------------


class TestServeCommand:
    def test_help_lists_all_flags(self, runner: CliRunner) -> None:
        result = runner.invoke(cli_module.app, ["serve", "--help"])
        assert result.exit_code == 0
        plain = _strip(result.output)
        for flag in ["--db", "--host", "--port", "--reload"]:
            assert flag in plain

    def test_missing_db_bootstraps_empty_schema(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Serve against a missing DB creates it on the fly (ADR 0012)."""
        monkeypatch.setenv(cli_module.ENV_OPENAI_API_KEY, "sk-test")
        db = tmp_path / "missing.db"

        # Stub uvicorn so we don't actually bind a port; stub openai so the
        # embedder constructor doesn't need a real key.
        monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: None)

        class _Fake:
            class embeddings:  # noqa: N801 — mirrors openai SDK attribute name
                @staticmethod
                def create(*, model: str, input: list[str]) -> Any:
                    raise AssertionError("should not be called during serve startup")

        monkeypatch.setattr(openai, "OpenAI", lambda *a, **kw: _Fake())

        result = runner.invoke(cli_module.app, ["serve", "--db", str(db)])
        assert result.exit_code == 0, result.output
        assert db.exists()

    def test_missing_openai_api_key_exits_cleanly(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(cli_module.ENV_OPENAI_API_KEY, raising=False)
        db = tmp_path / "out.db"
        SqliteStorage(db).close()
        result = runner.invoke(cli_module.app, ["serve", "--db", str(db)])
        assert result.exit_code == 2
        plain = _strip(result.output) + _strip(result.stderr or "")
        assert cli_module.ENV_OPENAI_API_KEY in plain
        assert "Traceback" not in plain

    def test_wires_uvicorn_with_expected_args(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify the CLI's wiring up to ``uvicorn.run`` without actually starting a server."""
        monkeypatch.setenv(cli_module.ENV_OPENAI_API_KEY, "sk-test")
        db = tmp_path / "out.db"
        SqliteStorage(db).close()

        captured: dict[str, Any] = {}

        # Stub uvicorn so the test doesn't actually bind a port.
        def fake_run(app: Any, **kwargs: Any) -> None:
            captured["app"] = app
            captured["kwargs"] = kwargs

        monkeypatch.setattr(uvicorn, "run", fake_run)

        # Stub openai.OpenAI so the embedder construction inside create_app
        # doesn't need a real API key.
        class _Fake:
            class embeddings:  # noqa: N801 — mirrors openai SDK attribute name
                @staticmethod
                def create(*, model: str, input: list[str]) -> Any:
                    raise AssertionError("should not be called during serve startup")

        monkeypatch.setattr(openai, "OpenAI", lambda *a, **kw: _Fake())

        result = runner.invoke(
            cli_module.app,
            ["serve", "--db", str(db), "--host", "0.0.0.0", "--port", "8123"],  # noqa: S104 — verifying CLI passes --host through
        )
        assert result.exit_code == 0, result.output
        assert captured["kwargs"]["host"] == "0.0.0.0"  # noqa: S104 — asserting wiring, not deploying
        assert captured["kwargs"]["port"] == 8123
        assert captured["kwargs"]["reload"] is False


# -- defaults & ergonomics --------------------------------------------------


class TestDefaults:
    def test_pull_to_defaults_to_today(
        self,
        runner: CliRunner,
        with_api_key: None,
        stub_pull: list[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        result = runner.invoke(
            cli_module.app,
            [
                "scrape",
                "proceedings",
                "--from",
                "2026-05-22",
                "--storage",
                str(tmp_path / "out.jsonl"),
                "--no-progress",
            ],
        )
        assert result.exit_code == 0, result.output
        # When --to is omitted, end should be today (UTC).
        assert stub_pull[0]["end"] == datetime.now(UTC).date()


# -- `concord run` ----------------------------------------------------------


class TestRunCommand:
    def test_help_lists_all_flags(self, runner: CliRunner) -> None:
        result = runner.invoke(cli_module.app, ["run", "proceedings", "--help"])
        assert result.exit_code == 0
        plain = _strip(result.output)
        for flag in ["--from", "--to", "--storage", "--db", "--limit"]:
            assert flag in plain

    def test_run_dispatches_all_three_stages(
        self,
        runner: CliRunner,
        with_api_key: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """`concord run` should call _run_pull, _run_load, _run_index in order."""
        monkeypatch.setenv(cli_module.ENV_OPENAI_API_KEY, "sk-test")
        calls: list[str] = []

        def fake_pull(**_kw: Any) -> PullResult:
            calls.append("pull")
            return PullResult(written=0, skipped=0, failed=0)

        def fake_load(**_kw: Any) -> tuple[int, int, int]:
            calls.append("load")
            return (0, 0, 0)

        def fake_index(**_kw: Any) -> IndexResult:
            calls.append("index")
            return IndexResult(
                chunked_proceedings=0,
                chunks_written=0,
                embedded_chunks=0,
                skipped_chunked=0,
                skipped_embedded=0,
            )

        monkeypatch.setattr(cli_proceedings_module, "_run_pull", fake_pull)
        monkeypatch.setattr(cli_proceedings_module, "_run_load", fake_load)
        monkeypatch.setattr(cli_proceedings_module, "_run_index", fake_index)

        result = runner.invoke(
            cli_module.app,
            [
                "run",
                "proceedings",
                "--from",
                "2026-05-22",
                "--to",
                "2026-05-22",
                "--storage",
                str(tmp_path / "out.jsonl"),
                "--db",
                str(tmp_path / "out.db"),
            ],
        )
        assert result.exit_code == 0, result.output
        assert calls == ["pull", "load", "index"]
        plain = _strip(result.output)
        assert "Stage 0" in plain
        assert "Stage 1" in plain
        assert "Stage 2" in plain

    def test_run_fails_fast_on_missing_congress_key(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.delenv(ENV_API_KEY, raising=False)
        monkeypatch.setenv(cli_module.ENV_OPENAI_API_KEY, "sk-test")
        result = runner.invoke(
            cli_module.app,
            [
                "run",
                "proceedings",
                "--from",
                "2026-05-22",
                "--storage",
                str(tmp_path / "out.jsonl"),
            ],
        )
        assert result.exit_code == 2
        plain = _strip(result.output) + _strip(result.stderr or "")
        assert ENV_API_KEY in plain

    def test_run_fails_fast_on_missing_openai_key(
        self,
        runner: CliRunner,
        with_api_key: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.delenv(cli_module.ENV_OPENAI_API_KEY, raising=False)
        result = runner.invoke(
            cli_module.app,
            [
                "run",
                "proceedings",
                "--from",
                "2026-05-22",
                "--storage",
                str(tmp_path / "out.jsonl"),
            ],
        )
        assert result.exit_code == 2
        plain = _strip(result.output) + _strip(result.stderr or "")
        assert cli_module.ENV_OPENAI_API_KEY in plain


# -- Progress helper ---------------------------------------------------------


class _FakeTTY:
    """StringIO that pretends to be a TTY for isatty()."""

    def __init__(self) -> None:
        self._buf = io.StringIO()
        self.write = self._buf.write
        self.flush = self._buf.flush

    def isatty(self) -> bool:
        return True

    def getvalue(self) -> str:
        return self._buf.getvalue()


class _FakeNonTTY:
    """StringIO that pretends to be a pipe / file."""

    def __init__(self) -> None:
        self._buf = io.StringIO()
        self.write = self._buf.write
        self.flush = self._buf.flush

    def isatty(self) -> bool:
        return False

    def getvalue(self) -> str:
        return self._buf.getvalue()


class TestProgress:
    def test_tty_overwrites_with_carriage_return(self) -> None:
        s = _FakeTTY()
        p = cli_module.Progress(enabled=True, stream=s)
        p.update("first")
        p.update("second")
        p.commit()
        # Both updates start with \r and clear-to-EOL; commit writes a newline.
        out = s.getvalue()
        assert out == "\r\x1b[Kfirst\r\x1b[Ksecond\n"

    def test_non_tty_falls_back_to_per_line(self) -> None:
        s = _FakeNonTTY()
        p = cli_module.Progress(enabled=True, stream=s)
        p.update("first")
        p.update("second")
        p.commit()
        # No carriage returns; one line per update; commit is a no-op
        # because there was no in-place line open.
        assert s.getvalue() == "first\nsecond\n"

    def test_disabled_writes_nothing(self) -> None:
        s = _FakeTTY()
        p = cli_module.Progress(enabled=False, stream=s)
        p.update("first")
        p.update("second")
        p.commit()
        assert s.getvalue() == ""

    def test_commit_only_emits_newline_when_inplace_line_open(self) -> None:
        # On a non-TTY, commit shouldn't emit a trailing newline because
        # there's no in-place line that needs ending.
        s = _FakeNonTTY()
        p = cli_module.Progress(enabled=True, stream=s)
        p.commit()  # no prior update — must be a no-op
        assert s.getvalue() == ""

    def test_context_manager_commits_on_exit(self) -> None:
        s = _FakeTTY()
        with cli_module.Progress(enabled=True, stream=s) as p:
            p.update("only update")
        # Exit calls commit -> newline.
        assert s.getvalue().endswith("\n")
        assert "only update" in s.getvalue()


# -- `concord load members` --------------------------------------------------


class TestLoadMembersCommand:
    def test_missing_jsonl_is_a_no_op(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """`load members` with no input file is informational, not an error."""
        result = runner.invoke(
            cli_module.app,
            [
                "load",
                "members",
                "--storage",
                str(tmp_path / "missing.jsonl"),
                "--db",
                str(tmp_path / "out.db"),
            ],
        )
        assert result.exit_code == 0, result.output
        plain = _strip(result.output)
        assert "No input file" in plain
        assert "scrape members" in plain


# -- `concord scrape bills` --------------------------------------------------


def _bills_fixture(name: str) -> dict[str, Any]:
    here = Path(__file__).parent / "fixtures" / "api" / "bills"
    return json.loads((here / name).read_text())


def _bills_handler(call_log: list[str] | None = None):
    """Return an httpx handler that answers list + detail for the bills fixtures."""
    list_payload = _bills_fixture("list_hr_119.json")
    details = {
        1: _bills_fixture("detail_119_hr_1.json"),
        22: _bills_fixture("detail_119_hr_22.json"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if call_log is not None:
            call_log.append(request.url.path)
        parts = request.url.path.rstrip("/").split("/")
        if len(parts) == 5:
            body = list_payload
        elif len(parts) == 6:
            number = int(parts[-1])
            body = details[number]
        else:
            return httpx.Response(404)
        return httpx.Response(
            200,
            content=json.dumps(body),
            headers={"content-type": "application/json"},
        )

    return handler


class TestBillsHelp:
    def test_scrape_bills_help_lists_flags(self, runner: CliRunner) -> None:
        result = runner.invoke(cli_module.app, ["scrape", "bills", "--help"])
        assert result.exit_code == 0
        plain = _strip(result.output)
        for flag in ["--congresses", "--bill-types", "--storage-dir", "--limit"]:
            assert flag in plain

    def test_load_bills_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli_module.app, ["load", "bills", "--help"])
        assert result.exit_code == 0
        plain = _strip(result.output)
        for flag in ["--storage-dir", "--db", "--limit"]:
            assert flag in plain

    def test_index_bills_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli_module.app, ["index", "bills", "--help"])
        assert result.exit_code == 0
        plain = _strip(result.output)
        for flag in ["--db", "--limit"]:
            assert flag in plain

    def test_run_bills_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli_module.app, ["run", "bills", "--help"])
        assert result.exit_code == 0
        plain = _strip(result.output)
        for flag in ["--congresses", "--bill-types", "--storage-dir", "--db", "--limit"]:
            assert flag in plain


class TestScrapeBillsCommand:
    def test_scrapes_with_limit(
        self,
        runner: CliRunner,
        with_api_key: None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handler = _bills_handler()

        # Monkey-patch the Client to inject the mock transport while still
        # exercising the real CLI wiring.
        original_init = Client.__init__

        def patched_init(self, **kwargs):
            kwargs.setdefault("transport", httpx.MockTransport(handler))
            kwargs.setdefault("sleep", lambda _s: None)
            original_init(self, **kwargs)

        monkeypatch.setattr(Client, "__init__", patched_init)

        result = runner.invoke(
            cli_module.app,
            [
                "scrape",
                "bills",
                "--congresses",
                "119",
                "--bill-types",
                "hr",
                "--storage-dir",
                str(tmp_path),
                "--db",
                str(tmp_path / "ledger.db"),
                "--limit",
                "2",
                "--no-progress",
            ],
        )
        assert result.exit_code == 0, result.output
        out = tmp_path / "bills.jsonl"
        assert out.exists()
        lines = out.read_text().splitlines()
        assert len(lines) == 2

    def test_scrape_records_a_scrape_run(
        self,
        runner: CliRunner,
        with_api_key: None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A bills scrape persists a complete Scrape Run: DB row + JSONL line
        with per-bucket success counts (ADR 0021)."""
        handler = _bills_handler()
        original_init = Client.__init__

        def patched_init(self, **kwargs):
            kwargs.setdefault("transport", httpx.MockTransport(handler))
            kwargs.setdefault("sleep", lambda _s: None)
            original_init(self, **kwargs)

        monkeypatch.setattr(Client, "__init__", patched_init)

        db_path = tmp_path / "ledger.db"
        result = runner.invoke(
            cli_module.app,
            [
                "scrape",
                "bills",
                "--congresses",
                "119",
                "--bill-types",
                "hr",
                "--storage-dir",
                str(tmp_path),
                "--db",
                str(db_path),
                "--limit",
                "2",
                "--no-progress",
            ],
        )
        assert result.exit_code == 0, result.output

        storage = SqliteStorage(db_path, load_vec=False)
        try:
            runs = storage._conn.execute("SELECT * FROM runs").fetchall()
        finally:
            storage.close()
        assert len(runs) == 1
        run = dict(runs[0])
        assert run["entity"] == "bills"
        assert run["command"] == "scrape bills"
        assert run["status"] == "ok"
        counts = json.loads(run["success_counts"])
        # One list page + two detail fetches, bucketed by endpoint.
        assert counts["api:bill/list"] == 1
        assert counts["api:bill/detail"] == 2

        backup = json.loads((tmp_path / "runs.jsonl").read_text().splitlines()[0])
        assert backup["run_id"] == run["run_id"]
        assert backup["success_counts"] == counts

    def test_rejects_unknown_bill_type(
        self,
        runner: CliRunner,
        with_api_key: None,
        tmp_path: Path,
    ) -> None:
        result = runner.invoke(
            cli_module.app,
            [
                "scrape",
                "bills",
                "--congresses",
                "119",
                "--bill-types",
                "xxx",
                "--storage-dir",
                str(tmp_path),
            ],
        )
        assert result.exit_code != 0
        plain = _strip(result.output) + _strip(result.stderr or "")
        assert "unknown bill type" in plain.lower()


class TestLoadBillsCommand:
    def test_missing_file_is_a_no_op(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        result = runner.invoke(
            cli_module.app,
            [
                "load",
                "bills",
                "--storage-dir",
                str(tmp_path / "no_such_dir"),
                "--db",
                str(tmp_path / "out.db"),
            ],
        )
        assert result.exit_code == 0, result.output
        plain = _strip(result.output)
        assert "No input file" in plain
        assert "scrape bills" in plain


# -- `concord scrape bills enrich` -------------------------------------------


def _enrichment_handler():
    """Return an httpx handler that answers /cosponsors, /actions, ... from fixtures."""
    fixtures_path = Path(__file__).parent / "fixtures" / "api" / "bills"
    fixtures = {
        "cosponsors": json.loads((fixtures_path / "cosponsors_119_hr_22.json").read_text()),
        "actions": json.loads((fixtures_path / "actions_119_hr_1.json").read_text()),
        "subjects": json.loads((fixtures_path / "subjects_119_hr_1.json").read_text()),
        "titles": json.loads((fixtures_path / "titles_119_hr_1.json").read_text()),
        "summaries": json.loads((fixtures_path / "summaries_119_hr_1.json").read_text()),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.rstrip("/").split("/")
        if len(parts) != 7:
            return httpx.Response(404)
        section = parts[-1]
        return httpx.Response(
            200,
            content=json.dumps(fixtures[section]),
            headers={"content-type": "application/json"},
        )

    return handler


class TestScrapeBillsEnrichCommand:
    def test_help_lists_flags(self, runner: CliRunner) -> None:
        result = runner.invoke(cli_module.app, ["scrape", "bills", "enrich", "--help"])
        assert result.exit_code == 0
        plain = _strip(result.output)
        for flag in ["--bill-ids", "--sections", "--storage-dir", "--db", "--limit"]:
            assert flag in plain

    def test_requires_bill_ids_or_db(
        self,
        runner: CliRunner,
        with_api_key: None,
        tmp_path: Path,
    ) -> None:
        result = runner.invoke(
            cli_module.app,
            [
                "scrape",
                "bills",
                "enrich",
                "--storage-dir",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 2
        plain = _strip(result.output) + _strip(result.stderr or "")
        assert "--bill-ids" in plain
        assert "--db" in plain

    def test_writes_one_envelope_per_section(
        self,
        runner: CliRunner,
        with_api_key: None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handler = _enrichment_handler()
        original_init = Client.__init__

        def patched_init(self, **kwargs):
            kwargs.setdefault("transport", httpx.MockTransport(handler))
            kwargs.setdefault("sleep", lambda _s: None)
            original_init(self, **kwargs)

        monkeypatch.setattr(Client, "__init__", patched_init)

        result = runner.invoke(
            cli_module.app,
            [
                "scrape",
                "bills",
                "enrich",
                "--bill-ids",
                "119-hr-1",
                "--storage-dir",
                str(tmp_path),
                "--no-progress",
            ],
        )
        assert result.exit_code == 0, result.output
        for section in ("cosponsors", "actions", "subjects", "titles", "summaries"):
            path = tmp_path / f"bill_{section}.jsonl"
            assert path.exists(), f"missing {path}"
            assert len(path.read_text().splitlines()) == 1

    def test_sections_subset(
        self,
        runner: CliRunner,
        with_api_key: None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handler = _enrichment_handler()
        original_init = Client.__init__

        def patched_init(self, **kwargs):
            kwargs.setdefault("transport", httpx.MockTransport(handler))
            kwargs.setdefault("sleep", lambda _s: None)
            original_init(self, **kwargs)

        monkeypatch.setattr(Client, "__init__", patched_init)

        result = runner.invoke(
            cli_module.app,
            [
                "scrape",
                "bills",
                "enrich",
                "--bill-ids",
                "119-hr-1",
                "--sections",
                "cosponsors,actions",
                "--storage-dir",
                str(tmp_path),
                "--no-progress",
            ],
        )
        assert result.exit_code == 0, result.output
        files = {p.name for p in tmp_path.iterdir()}
        assert files == {"bill_cosponsors.jsonl", "bill_actions.jsonl"}

    def test_limit_caps_bill_ids_input(
        self,
        runner: CliRunner,
        with_api_key: None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--limit applies to --bill-ids count too (Fix #2 regression)."""
        handler = _enrichment_handler()
        original_init = Client.__init__

        def patched_init(self, **kwargs):
            kwargs.setdefault("transport", httpx.MockTransport(handler))
            kwargs.setdefault("sleep", lambda _s: None)
            original_init(self, **kwargs)

        monkeypatch.setattr(Client, "__init__", patched_init)

        result = runner.invoke(
            cli_module.app,
            [
                "scrape",
                "bills",
                "enrich",
                "--bill-ids",
                "119-hr-1,119-hr-22,119-hr-47",
                "--storage-dir",
                str(tmp_path),
                "--limit",
                "1",
                "--no-progress",
            ],
        )
        assert result.exit_code == 0, result.output
        # Only one bill should have been enriched per section, despite
        # three bill_ids being supplied.
        cosponsors_lines = (tmp_path / "bill_cosponsors.jsonl").read_text().splitlines()
        assert len(cosponsors_lines) == 1

    def test_rejects_unknown_section(
        self,
        runner: CliRunner,
        with_api_key: None,
        tmp_path: Path,
    ) -> None:
        result = runner.invoke(
            cli_module.app,
            [
                "scrape",
                "bills",
                "enrich",
                "--bill-ids",
                "119-hr-1",
                "--sections",
                "wibble",
                "--storage-dir",
                str(tmp_path),
            ],
        )
        assert result.exit_code != 0
        plain = _strip(result.output) + _strip(result.stderr or "")
        assert "unknown section" in plain.lower()

    def test_rejects_bad_bill_id(
        self,
        runner: CliRunner,
        with_api_key: None,
        tmp_path: Path,
    ) -> None:
        result = runner.invoke(
            cli_module.app,
            [
                "scrape",
                "bills",
                "enrich",
                "--bill-ids",
                "bad-token",
                "--storage-dir",
                str(tmp_path),
            ],
        )
        assert result.exit_code != 0

    def test_db_autoselect_no_unenriched(
        self,
        runner: CliRunner,
        with_api_key: None,
        tmp_path: Path,
    ) -> None:
        # Empty DB → empty result; should report "nothing to do".
        db = tmp_path / "test.db"
        SqliteStorage(db, load_vec=False).close()
        result = runner.invoke(
            cli_module.app,
            [
                "scrape",
                "bills",
                "enrich",
                "--db",
                str(db),
                "--storage-dir",
                str(tmp_path),
                "--no-progress",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "nothing to do" in _strip(result.output).lower()


# -- `concord scrape votes` / load / index / run (Phase 3a) -----------------


def _votes_fixture(name: str) -> dict[str, Any]:
    here = Path(__file__).parent / "fixtures" / "api" / "votes"
    return json.loads((here / name).read_text())


def _votes_handler():
    # Inline list payload so the rolls match the per-roll detail
    # fixtures the test has on hand. (Master's real list pairs rolls
    # 240 + 306, but no fixture exists for roll 306.)
    list_payload = {
        "houseRollCallVotes": [
            {"congress": 119, "sessionNumber": 1, "rollCallNumber": 240},
            {"congress": 119, "sessionNumber": 1, "rollCallNumber": 241},
        ],
        "pagination": {"count": 2},
    }
    details = {
        240: _votes_fixture("detail_house_119_1_240.json"),
        241: _votes_fixture("detail_house_119_1_241_amendment.json"),
    }
    members = {
        240: _votes_fixture("members_house_119_1_240.json"),
        241: _votes_fixture("members_house_119_1_241.json"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.rstrip("/").split("/")
        if len(parts) == 5:
            body = list_payload
        elif len(parts) == 6:
            body = details[int(parts[-1])]
        elif len(parts) == 7 and parts[-1] == "members":
            body = members[int(parts[-2])]
        else:
            return httpx.Response(404)
        return httpx.Response(
            200,
            content=json.dumps(body),
            headers={"content-type": "application/json"},
        )

    return handler


class TestVotesHelp:
    def test_scrape_votes_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli_module.app, ["scrape", "votes", "--help"])
        assert result.exit_code == 0
        plain = _strip(result.output)
        for flag in ["--congresses", "--sessions", "--chambers", "--storage-dir", "--limit"]:
            assert flag in plain

    def test_load_votes_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli_module.app, ["load", "votes", "--help"])
        assert result.exit_code == 0
        for flag in ["--storage-dir", "--db", "--limit"]:
            assert flag in _strip(result.output)

    def test_index_votes_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli_module.app, ["index", "votes", "--help"])
        assert result.exit_code == 0
        for flag in ["--db", "--limit"]:
            assert flag in _strip(result.output)

    def test_run_votes_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli_module.app, ["run", "votes", "--help"])
        assert result.exit_code == 0


def _senate_votes_handler():
    """Mock handler for senate.gov LIS XML feeds.

    Serves three responses: senators_cfm.xml (roster), a synthetic
    vote_menu listing two roll numbers, and the spike-captured detail
    XMLs for those rolls.
    """
    fixtures = Path(__file__).parent / "fixtures" / "senate"
    roster = (fixtures / "senators_cfm.xml").read_bytes()
    detail_7 = (fixtures / "detail_119_1_00007_bill.xml").read_bytes()
    detail_3 = (fixtures / "detail_119_1_00003_amendment.xml").read_bytes()
    menu = (
        b"<?xml version='1.0'?><vote_summary><votes>"
        b"<vote><vote_number>00003</vote_number></vote>"
        b"<vote><vote_number>00007</vote_number></vote>"
        b"</votes></vote_summary>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("senators_cfm.xml"):
            body = roster
        elif "vote_menu_" in url:
            body = menu
        elif url.endswith("vote_119_1_00007.xml"):
            body = detail_7
        elif url.endswith("vote_119_1_00003.xml"):
            body = detail_3
        else:
            return httpx.Response(404)
        return httpx.Response(200, content=body, headers={"content-type": "application/xml"})

    return handler


def _patch_senate_client(monkeypatch: pytest.MonkeyPatch) -> None:
    original = SenateClient.__init__

    def patched(self, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(_senate_votes_handler()))
        kwargs.setdefault("sleep", lambda _s: None)
        original(self, **kwargs)

    monkeypatch.setattr(SenateClient, "__init__", patched)


class TestScrapeVotesCommand:
    def test_scrape_house_only(
        self,
        runner: CliRunner,
        with_api_key: None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handler = _votes_handler()

        original_init = Client.__init__

        def patched_init(self, **kwargs):
            kwargs.setdefault("transport", httpx.MockTransport(handler))
            kwargs.setdefault("sleep", lambda _s: None)
            original_init(self, **kwargs)

        monkeypatch.setattr(Client, "__init__", patched_init)

        result = runner.invoke(
            cli_module.app,
            [
                "scrape",
                "votes",
                "--congresses",
                "119",
                "--sessions",
                "1",
                "--chambers",
                "house",
                "--limit",
                "2",
                "--storage-dir",
                str(tmp_path),
                "--no-progress",
            ],
        )
        assert result.exit_code == 0, result.output
        details = (tmp_path / "house_votes.jsonl").read_text().splitlines()
        members = (tmp_path / "house_vote_positions.jsonl").read_text().splitlines()
        assert len(details) == 2
        assert len(members) == 2
        assert not (tmp_path / "senate_votes.jsonl").exists()

    def test_default_runs_both_chambers(
        self,
        runner: CliRunner,
        with_api_key: None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        handler = _votes_handler()
        original_init = Client.__init__

        def patched_init(self, **kwargs):
            kwargs.setdefault("transport", httpx.MockTransport(handler))
            kwargs.setdefault("sleep", lambda _s: None)
            original_init(self, **kwargs)

        monkeypatch.setattr(Client, "__init__", patched_init)
        _patch_senate_client(monkeypatch)

        result = runner.invoke(
            cli_module.app,
            [
                "scrape",
                "votes",
                "--congresses",
                "119",
                "--sessions",
                "1",
                "--limit",
                "2",
                "--storage-dir",
                str(tmp_path),
                "--no-progress",
            ],
        )
        assert result.exit_code == 0, result.output
        assert (tmp_path / "house_votes.jsonl").exists()
        assert (tmp_path / "house_vote_positions.jsonl").exists()
        assert (tmp_path / "senate_votes.jsonl").exists()
        assert (tmp_path / "senate_roster.jsonl").exists()

    def test_senate_only(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # No API key needed — Senate-only.
        monkeypatch.delenv("CONGRESS_API_KEY", raising=False)
        _patch_senate_client(monkeypatch)

        result = runner.invoke(
            cli_module.app,
            [
                "scrape",
                "votes",
                "--congresses",
                "119",
                "--sessions",
                "1",
                "--chambers",
                "senate",
                "--limit",
                "1",
                "--storage-dir",
                str(tmp_path),
                "--no-progress",
            ],
        )
        assert result.exit_code == 0, result.output
        assert (tmp_path / "senate_votes.jsonl").exists()
        assert (tmp_path / "senate_roster.jsonl").exists()
        assert not (tmp_path / "house_votes.jsonl").exists()


class TestLoadVotesCommand:
    def test_missing_file_is_a_no_op(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(
            cli_module.app,
            [
                "load",
                "votes",
                "--storage-dir",
                str(tmp_path / "absent"),
                "--db",
                str(tmp_path / "out.db"),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "No input files" in _strip(result.output)

    def test_senate_only_loads_when_house_jsonl_absent(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """Senate-only scrape → load chain must not be gated on house_votes.jsonl."""
        senate_fixtures = Path(__file__).parent / "fixtures" / "senate"
        detail = (senate_fixtures / "detail_119_1_00007_bill.xml").read_text()
        roster = (senate_fixtures / "senators_cfm.xml").read_text()
        ts = "2026-05-26T14:00:00+00:00"
        (tmp_path / SENATE_VOTES_JSONL_NAME).write_text(
            json.dumps(
                {
                    "fetched_at": ts,
                    "key": {
                        "chamber": "senate",
                        "congress": 119,
                        "session": 1,
                        "roll_number": 7,
                    },
                    "payload": detail,
                }
            )
            + "\n"
        )
        (tmp_path / SENATE_ROSTER_JSONL_NAME).write_text(
            json.dumps(
                {
                    "fetched_at": ts,
                    "key": {"source": "senators_cfm"},
                    "payload": roster,
                }
            )
            + "\n"
        )

        result = runner.invoke(
            cli_module.app,
            [
                "load",
                "votes",
                "--storage-dir",
                str(tmp_path),
                "--db",
                str(tmp_path / "out.db"),
            ],
        )
        assert result.exit_code == 0, result.output
        # The senate vote should have actually loaded, not been skipped.
        assert "No input files" not in _strip(result.output)
        assert "Loaded 1 vote" in _strip(result.output)
