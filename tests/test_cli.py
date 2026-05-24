"""Tests for the ``concord`` CLI.

The CLI's responsibilities are: parse flags, surface clear errors on missing
API key, wire the pipeline, and format the success summary. The orchestrator
itself is covered by tests/test_pipeline.py — here we stub it out and verify
the CLI does the right thing around it.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import concord.cli as cli_module
from concord.api import ENV_API_KEY, ApiError
from concord.pipeline import PullResult


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
        for flag in ["--from", "--to", "--storage", "--limit"]:
            assert flag in result.output


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
        assert "proceedings.jsonl" in result.output

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
        assert "Wrote 3 new proceedings" in result.output
        assert str(out_path) in result.output
        assert "skipped 1" in result.output


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
        # The error message mentions the env var so the user knows what to set.
        assert ENV_API_KEY in result.output or ENV_API_KEY in (result.stderr or "")
        # Ensure we don't accidentally let an unhandled exception through.
        assert "Traceback" not in result.output

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
        assert "simulated init failure" in result.output
