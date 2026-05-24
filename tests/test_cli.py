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
