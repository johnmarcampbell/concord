"""Tests for the ``concord`` CLI.

The CLI's responsibilities are: parse flags, surface clear errors on missing
API key, wire the pipeline, and format the success summary. The orchestrator
itself is covered by tests/test_pipeline.py — here we stub it out and verify
the CLI does the right thing around it.
"""

import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import concord.cli as cli_module
from concord.api import ENV_API_KEY, ApiError
from concord.pipeline import PullResult

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
    """Replace ``concord.cli.pull`` with a recorder; return the call log."""
    calls: list[dict[str, Any]] = []

    def fake_pull(start: date, end: date, **kwargs: Any) -> PullResult:
        calls.append({"start": start, "end": end, **kwargs})
        return PullResult(written=3, skipped=1)

    monkeypatch.setattr(cli_module, "pull", fake_pull)
    return calls


# -- help & arg parsing -------------------------------------------------------


class TestHelp:
    def test_help_lists_all_flags(self, runner: CliRunner) -> None:
        result = runner.invoke(cli_module.app, ["pull", "--help"])
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
                "pull",
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
                "pull",
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
                "pull",
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

    def test_pull_no_progress_by_default(
        self,
        runner: CliRunner,
        with_api_key: None,
        stub_pull: list[dict[str, Any]],
        tmp_path: Path,
    ) -> None:
        result = runner.invoke(
            cli_module.app,
            [
                "pull",
                "--from",
                "2026-05-22",
                "--to",
                "2026-05-22",
                "--storage",
                str(tmp_path / "out.jsonl"),
            ],
        )
        assert result.exit_code == 0, result.output
        # Without --progress, callback is None.
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
            ["pull", "--from", "2026-05-22", "--to", "2026-05-22"],
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
            ["pull", "--from", bad_date, "--to", "2026-05-22"],
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
                "pull",
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
                "pull",
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

        monkeypatch.setattr(cli_module, "Client", boom)
        result = runner.invoke(
            cli_module.app,
            [
                "pull",
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


# -- Mongo backend wiring ----------------------------------------------------


class TestMongoBackend:
    def test_mongo_uri_routes_to_mongo_storage(
        self,
        runner: CliRunner,
        with_api_key: None,
        stub_pull: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With --mongo-uri, the CLI calls MongoStorage.from_uri instead of JsonlStorage."""
        from concord.storage import MongoStorage

        calls: list[dict[str, Any]] = []

        def fake_from_uri(uri: str, *, db: str, collection: str) -> MongoStorage:
            calls.append({"uri": uri, "db": db, "collection": collection})
            # Return a stand-in that satisfies the Storage protocol.
            import mongomock

            return MongoStorage(collection=mongomock.MongoClient()[db][collection])

        monkeypatch.setattr(cli_module.MongoStorage, "from_uri", staticmethod(fake_from_uri))
        result = runner.invoke(
            cli_module.app,
            [
                "pull",
                "--from",
                "2026-05-22",
                "--to",
                "2026-05-22",
                "--mongo-uri",
                "mongodb://example.invalid:27017",
                "--mongo-db",
                "my_db",
                "--mongo-collection",
                "my_coll",
            ],
        )
        assert result.exit_code == 0, result.output
        assert calls == [
            {"uri": "mongodb://example.invalid:27017", "db": "my_db", "collection": "my_coll"}
        ]
        # Success summary names the mongo target, not a file path.
        assert "mongodb://my_db.my_coll" in _strip(result.output)

    def test_pymongo_missing_exits_cleanly(
        self,
        runner: CliRunner,
        with_api_key: None,
        stub_pull: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Helpful error if MongoStorage.from_uri raises ImportError (no [mongo])."""

        def fake_from_uri(*_args: Any, **_kwargs: Any) -> Any:
            raise ImportError("pymongo not installed; install concord[mongo]")

        monkeypatch.setattr(cli_module.MongoStorage, "from_uri", staticmethod(fake_from_uri))
        result = runner.invoke(
            cli_module.app,
            [
                "pull",
                "--from",
                "2026-05-22",
                "--to",
                "2026-05-22",
                "--mongo-uri",
                "mongodb://example.invalid:27017",
            ],
        )
        assert result.exit_code == 2
        assert "pymongo not installed" in _strip(result.output)


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
        result = runner.invoke(cli_module.app, ["load", "--help"])
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
            ["load", "--jsonl", str(jsonl), "--db", str(db)],
        )
        assert result.exit_code == 0, result.output
        plain = _strip(result.output)
        assert "Loaded 2 new proceedings" in plain
        assert str(db) in plain

        # Verify the rows actually landed.
        from concord.storage import SqliteStorage

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

        first = runner.invoke(cli_module.app, ["load", "--jsonl", str(jsonl), "--db", str(db)])
        assert first.exit_code == 0
        second = runner.invoke(cli_module.app, ["load", "--jsonl", str(jsonl), "--db", str(db)])
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
            ["load", "--jsonl", str(jsonl), "--db", str(db), "--limit", "2"],
        )
        assert result.exit_code == 0, result.output
        plain = _strip(result.output)
        assert "Loaded 2 new proceedings" in plain

        from concord.storage import SqliteStorage

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
            ["load", "--jsonl", str(jsonl), "--db", str(db)],
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
            ["load", "--jsonl", str(jsonl), "--db", str(db)],
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
    from datetime import UTC, datetime

    from concord.models import Article, Issue, Proceeding
    from concord.storage import SqliteStorage

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
        result = runner.invoke(cli_module.app, ["index", "--help"])
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
        result = runner.invoke(cli_module.app, ["index", "--db", str(tmp_path / "missing.db")])
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
        result = runner.invoke(cli_module.app, ["index", "--db", str(db)])
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
        import openai

        monkeypatch.setattr(openai, "OpenAI", lambda *a, **kw: _FakeOpenAIClient())

        result = runner.invoke(cli_module.app, ["index", "--db", str(db)])
        assert result.exit_code == 0, result.output
        plain = _strip(result.output)
        assert "Indexed:" in plain
        assert "chunked 2 new proceedings" in plain
        assert "embedded" in plain

        # Verify state on disk: chunks_vec has rows.
        from concord.storage import SqliteStorage

        with SqliteStorage(db) as storage:
            count = storage.connection.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0]
            assert count > 0
