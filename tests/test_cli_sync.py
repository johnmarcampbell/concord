"""Tests for ``concord sync`` (the Sync cycle).

The four entity pipelines are exercised by their own suites; here we stub
them out and verify the *orchestration* contract from ADR 0026:

- best-effort isolation (one entity failing doesn't abort the rest),
- the advisory ``flock`` overlap guard,
- the rolling-window + current-Congress wiring,
- API-key gating, and
- the exit-code contract (0 ok · 1 entity failed · 2 missing key · 75 locked).
"""

import fcntl
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import concord.cli as cli_module
import concord.cli.cycle as cycle_module
from concord.api import ENV_API_KEY
from concord.cli._common import ENV_OPENAI_API_KEY
from concord.cli.cycle import CycleAlreadyRunningError, run_cycle

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

_PIPELINE_ATTRS = (
    "run_proceedings_pipeline",
    "run_members_pipeline",
    "run_bills_pipeline",
    "run_votes_pipeline",
)


def _strip(text: str) -> str:
    return _ANSI.sub("", text)


def _combined(result: Any) -> str:
    """stdout + stderr, ANSI-stripped (CliRunner captures them separately)."""
    return _strip(result.output) + _strip(result.stderr or "")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def with_both_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_API_KEY, "test-key")
    monkeypatch.setenv(ENV_OPENAI_API_KEY, "sk-test")


def _install_pipelines(
    monkeypatch: pytest.MonkeyPatch,
    *,
    captured: dict[str, dict[str, Any]] | None = None,
    order: list[str] | None = None,
    fail: tuple[str, ...] = (),
) -> None:
    """Replace the four ``run_<entity>_pipeline`` callables in ``cycle``.

    Each stub records its call (into ``captured`` and/or ``order``) and, if its
    entity is listed in ``fail``, raises so the best-effort path is exercised.
    """
    entities = ("proceedings", "members", "bills", "votes")
    for entity, attr in zip(entities, _PIPELINE_ATTRS, strict=True):

        def _make(name: str) -> Any:
            def _fake(**kwargs: Any) -> None:
                if order is not None:
                    order.append(name)
                if captured is not None:
                    captured[name] = kwargs
                if name in fail:
                    raise RuntimeError(f"{name} boom")

            return _fake

        monkeypatch.setattr(cycle_module, attr, _make(entity))


# -- help --------------------------------------------------------------------


def test_sync_help_lists_flags(runner: CliRunner) -> None:
    result = runner.invoke(cli_module.app, ["sync", "--help"])
    assert result.exit_code == 0
    plain = _strip(result.output)
    for flag in ["--lookback-days", "--db", "--progress"]:
        assert flag in plain


# -- window + Congress wiring ------------------------------------------------


def test_window_and_congress_wiring(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, dict[str, Any]] = {}
    _install_pipelines(monkeypatch, captured=captured)

    db = tmp_path / "proceedings.db"
    result = run_cycle(
        lookback_days=7,
        db_path=db,
        show_progress=False,
        today=date(2026, 6, 23),
    )
    assert result.ok

    # Proceedings: rolling [today - lookback, today] window.
    proc = captured["proceedings"]
    assert proc["start"] == date(2026, 6, 16)
    assert proc["end"] == date(2026, 6, 23)
    assert proc["command"] == "sync"
    assert proc["storage_path"] == tmp_path / "proceedings.jsonl"

    # Mutable entities: current Congress only, skip_unchanged always on.
    assert captured["members"]["congresses"] == [119]
    assert captured["members"]["skip_unchanged"] is True
    assert captured["members"]["storage_path"] == tmp_path / "members.jsonl"
    assert captured["bills"]["congresses"] == [119]
    assert captured["bills"]["skip_unchanged"] is True
    assert captured["bills"]["storage_dir"] == tmp_path
    assert captured["votes"]["congresses"] == [119]
    assert captured["votes"]["skip_unchanged"] is True
    assert captured["votes"]["storage_dir"] == tmp_path
    for entity in ("members", "bills", "votes"):
        assert captured[entity]["command"] == "sync"


def test_lookback_days_changes_only_the_proceedings_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, dict[str, Any]] = {}
    _install_pipelines(monkeypatch, captured=captured)

    run_cycle(
        lookback_days=1,
        db_path=tmp_path / "p.db",
        show_progress=False,
        today=date(2026, 6, 23),
    )
    assert captured["proceedings"]["start"] == date(2026, 6, 22)
    assert captured["proceedings"]["end"] == date(2026, 6, 23)


# -- best-effort isolation ---------------------------------------------------


def test_best_effort_one_failure_does_not_abort_the_rest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    order: list[str] = []
    _install_pipelines(monkeypatch, order=order, fail=("bills",))

    result = run_cycle(
        lookback_days=7,
        db_path=tmp_path / "p.db",
        show_progress=False,
        today=date(2026, 6, 23),
    )

    # All four ran in order even though bills raised mid-way.
    assert order == ["proceedings", "members", "bills", "votes"]
    assert not result.ok

    by_entity = {r.entity: r for r in result.results}
    assert by_entity["bills"].ok is False
    assert by_entity["bills"].error is not None
    assert "boom" in by_entity["bills"].error
    assert by_entity["proceedings"].ok is True
    assert by_entity["members"].ok is True
    assert by_entity["votes"].ok is True


def test_sync_exits_1_when_an_entity_fails(
    runner: CliRunner,
    with_both_keys: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_pipelines(monkeypatch, fail=("bills",))
    result = runner.invoke(
        cli_module.app,
        ["sync", "--db", str(tmp_path / "p.db"), "--no-progress"],
    )
    assert result.exit_code == 1, result.output
    plain = _combined(result)
    assert "bills" in plain
    assert "failures" in plain.lower()


def test_sync_exits_0_when_all_entities_ok(
    runner: CliRunner,
    with_both_keys: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_pipelines(monkeypatch)
    result = runner.invoke(
        cli_module.app,
        ["sync", "--db", str(tmp_path / "p.db"), "--no-progress"],
    )
    assert result.exit_code == 0, result.output
    assert "sync complete" in _combined(result).lower()


# -- flock overlap guard -----------------------------------------------------


def test_run_cycle_raises_when_lock_already_held(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    order: list[str] = []
    _install_pipelines(monkeypatch, order=order)

    lock_path = tmp_path / ".sync.lock"
    with lock_path.open("a", encoding="utf-8") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(CycleAlreadyRunningError):
            run_cycle(
                lookback_days=7,
                db_path=tmp_path / "p.db",
                show_progress=False,
                today=date(2026, 6, 23),
            )
    # No pipeline ran while the lock was held.
    assert order == []


def test_sync_exits_75_when_another_sync_is_running(
    runner: CliRunner,
    with_both_keys: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    order: list[str] = []
    _install_pipelines(monkeypatch, order=order)

    db = tmp_path / "p.db"
    lock_path = tmp_path / ".sync.lock"  # == db.parent / ".sync.lock"
    with lock_path.open("a", encoding="utf-8") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = runner.invoke(
            cli_module.app,
            ["sync", "--db", str(db), "--no-progress"],
        )
    assert result.exit_code == 75, result.output
    assert "already running" in _combined(result).lower()
    assert order == []


# -- API-key gating ----------------------------------------------------------


def test_sync_exits_2_when_congress_key_missing(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    monkeypatch.setenv(ENV_OPENAI_API_KEY, "sk-test")
    order: list[str] = []
    _install_pipelines(monkeypatch, order=order)

    result = runner.invoke(
        cli_module.app,
        ["sync", "--db", str(tmp_path / "p.db"), "--no-progress"],
    )
    assert result.exit_code == 2
    plain = _combined(result)
    assert ENV_API_KEY in plain
    assert "Traceback" not in plain
    assert order == []


def test_sync_exits_2_when_openai_key_missing(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(ENV_API_KEY, "test-key")
    monkeypatch.delenv(ENV_OPENAI_API_KEY, raising=False)
    order: list[str] = []
    _install_pipelines(monkeypatch, order=order)

    result = runner.invoke(
        cli_module.app,
        ["sync", "--db", str(tmp_path / "p.db"), "--no-progress"],
    )
    assert result.exit_code == 2
    plain = _combined(result)
    assert ENV_OPENAI_API_KEY in plain
    assert "Traceback" not in plain
    assert order == []
