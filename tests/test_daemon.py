"""Tests for the unsupervised scraping daemon (ADR 0026).

Covers the pure planner (:mod:`concord.daemon.plan`), the JSON watermark
(:mod:`concord.daemon.state`), the Tick loop with injected clock + executor
(:mod:`concord.daemon.loop`), and the CLI option parsing helpers. No real
subprocesses, clock, signals, or network.
"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import typer

from concord.cli.daemon import _parse_csv_lower, _parse_interval
from concord.daemon import loop, runner
from concord.daemon.plan import (
    DaemonConfig,
    Job,
    ProceedingsCursor,
    StateView,
    build_tick,
    next_proceedings_window,
)
from concord.daemon.state import DaemonState, load_state, save_state, state_path

TODAY = date(2026, 6, 21)


def _config(tmp_path: Path, **overrides: object) -> DaemonConfig:
    defaults: dict[str, object] = {
        "data_dir": tmp_path,
        "db_path": tmp_path / "proceedings.db",
        "congresses": (117, 118, 119),
        "bill_types": ("hr", "s"),
        "chambers": ("house",),
        "proceedings_since": date(2021, 1, 3),
        "proceedings_window_days": 30,
        "proceedings_forward_days": 7,
        "backfill_per_tick": 1,
        "enrich_bills": False,
        "index_proceedings": True,
    }
    defaults.update(overrides)
    return DaemonConfig(**defaults)  # type: ignore[arg-type]


def _descs(jobs: list[Job]) -> list[str]:
    return [j.description for j in jobs]


# ---------------------------------------------------------------------------
# DaemonConfig
# ---------------------------------------------------------------------------


def test_current_congress_is_newest(tmp_path: Path) -> None:
    assert _config(tmp_path).current_congress == 119


def test_backfill_congresses_excludes_current_newest_first(tmp_path: Path) -> None:
    assert _config(tmp_path).backfill_congresses == (118, 117)


# ---------------------------------------------------------------------------
# build_tick — structure & gating
# ---------------------------------------------------------------------------


def test_tick_drives_all_four_entities(tmp_path: Path) -> None:
    jobs = build_tick(_config(tmp_path), StateView(), TODAY)
    descs = " ".join(_descs(jobs))
    for entity in ("proceedings", "members", "bills", "votes"):
        assert f"{entity} forward" in descs


def test_forward_scrapes_use_skip_unchanged_backfill_does_not(tmp_path: Path) -> None:
    jobs = build_tick(_config(tmp_path), StateView(), TODAY)
    members_forward = next(j for j in jobs if j.description == "members forward congress 119")
    members_backfill = next(j for j in jobs if j.description.startswith("members backfill"))
    assert "--skip-unchanged" in members_forward.argv
    assert "--skip-unchanged" not in members_backfill.argv


def test_backfill_picks_oldest_not_done(tmp_path: Path) -> None:
    # 118 already done -> the next congress-backfill for members is 117.
    state = StateView(congress_done=frozenset({("members", 118)}))
    jobs = build_tick(_config(tmp_path), state, TODAY)
    assert "members backfill congress 117" in _descs(jobs)
    assert "members backfill congress 118" not in _descs(jobs)


def test_backfill_per_tick_caps_chunks(tmp_path: Path) -> None:
    jobs = build_tick(_config(tmp_path, backfill_per_tick=2), StateView(), TODAY)
    members_backfills = [d for d in _descs(jobs) if d.startswith("members backfill")]
    assert members_backfills == ["members backfill congress 118", "members backfill congress 117"]


def test_no_backfill_when_all_congresses_done(tmp_path: Path) -> None:
    done = frozenset({("members", 117), ("members", 118)})
    jobs = build_tick(
        _config(tmp_path, entities=("members",)), StateView(congress_done=done), TODAY
    )
    assert not any(d.startswith("members backfill") for d in _descs(jobs))
    assert "members forward congress 119" in _descs(jobs)


def test_entities_subset_is_honoured(tmp_path: Path) -> None:
    jobs = build_tick(_config(tmp_path, entities=("votes",)), StateView(), TODAY)
    descs = " ".join(_descs(jobs))
    assert "votes forward" in descs
    assert "members" not in descs
    assert "bills" not in descs
    assert "proceedings" not in descs


def test_index_proceedings_skipped_without_openai(tmp_path: Path) -> None:
    with_index = build_tick(_config(tmp_path, index_proceedings=True), StateView(), TODAY)
    without = build_tick(_config(tmp_path, index_proceedings=False), StateView(), TODAY)
    assert "proceedings index" in _descs(with_index)
    assert "proceedings index" not in _descs(without)
    # Scrape + load still happen either way.
    assert "proceedings load" in _descs(without)


def test_enrich_bills_only_when_enabled(tmp_path: Path) -> None:
    off = build_tick(_config(tmp_path, enrich_bills=False), StateView(), TODAY)
    on = build_tick(_config(tmp_path, enrich_bills=True), StateView(), TODAY)
    assert not any("enrich" in d for d in _descs(off))
    assert any("enrich" in d for d in _descs(on))


def test_load_and_index_run_after_scrapes_per_entity(tmp_path: Path) -> None:
    jobs = _descs(build_tick(_config(tmp_path, entities=("members",)), StateView(), TODAY))
    assert jobs.index("members forward congress 119") < jobs.index("members load")
    assert jobs.index("members load") < jobs.index("members index")


# ---------------------------------------------------------------------------
# next_proceedings_window — date-window walk
# ---------------------------------------------------------------------------


def test_first_window_starts_below_forward_floor(tmp_path: Path) -> None:
    # forward covers [today-7, today]; first backfill window ends today-8.
    window = next_proceedings_window(_config(tmp_path), None, TODAY)
    assert window is not None
    start, end = window
    assert end == date(2026, 6, 13)  # today - 8 days
    assert (end - start).days == 29  # 30-day inclusive window


def test_window_walks_backward_from_cursor(tmp_path: Path) -> None:
    cursor = date(2026, 5, 1)
    window = next_proceedings_window(_config(tmp_path), cursor, TODAY)
    assert window == (date(2026, 4, 1), date(2026, 4, 30))


def test_window_clamps_to_since_floor(tmp_path: Path) -> None:
    cfg = _config(tmp_path, proceedings_since=date(2026, 4, 20))
    window = next_proceedings_window(cfg, date(2026, 5, 1), TODAY)
    assert window == (date(2026, 4, 20), date(2026, 4, 30))


def test_window_none_when_floor_reached(tmp_path: Path) -> None:
    cfg = _config(tmp_path, proceedings_since=date(2026, 5, 1))
    assert next_proceedings_window(cfg, date(2026, 5, 1), TODAY) is None


def test_backfill_chunks_chain_within_one_tick(tmp_path: Path) -> None:
    # Two chunks in one tick must not target the same window.
    jobs = build_tick(_config(tmp_path, backfill_per_tick=2), StateView(), TODAY)
    windows = [j.marker for j in jobs if isinstance(j.marker, ProceedingsCursor)]
    assert windows[0].oldest > windows[1].oldest


# ---------------------------------------------------------------------------
# DaemonState
# ---------------------------------------------------------------------------


def test_load_state_defaults_when_missing(tmp_path: Path) -> None:
    state = load_state(tmp_path)
    assert state.congress_backfilled == {}
    assert state.proceedings_oldest_scraped is None


def test_state_roundtrip(tmp_path: Path) -> None:
    state = DaemonState()
    state.mark_congress_done("bills", 118)
    state.proceedings_oldest_scraped = date(2025, 1, 1)
    save_state(tmp_path, state)
    reloaded = load_state(tmp_path)
    assert reloaded.is_congress_done("bills", 118)
    assert reloaded.proceedings_oldest_scraped == date(2025, 1, 1)


def test_mark_congress_done_is_sorted_and_idempotent(tmp_path: Path) -> None:
    state = DaemonState()
    state.mark_congress_done("votes", 119)
    state.mark_congress_done("votes", 117)
    state.mark_congress_done("votes", 119)
    assert state.congress_backfilled["votes"] == [117, 119]


def test_load_state_tolerates_corrupt_file(tmp_path: Path) -> None:
    state_path(tmp_path).write_text("{ not json", encoding="utf-8")
    assert load_state(tmp_path).congress_backfilled == {}


# ---------------------------------------------------------------------------
# loop.run_tick / serve
# ---------------------------------------------------------------------------


def test_run_tick_marks_backfill_on_success(tmp_path: Path) -> None:
    cfg = _config(tmp_path, entities=("members",))
    loop.run_tick(cfg, run=lambda _job: 0, now=lambda: _dt(TODAY))
    state = load_state(tmp_path)
    assert state.is_congress_done("members", 118)


def test_run_tick_does_not_mark_on_failure(tmp_path: Path) -> None:
    cfg = _config(tmp_path, entities=("members",))

    def _run(job: Job) -> int:
        return 1 if "backfill" in job.description else 0

    result = loop.run_tick(cfg, run=_run, now=lambda: _dt(TODAY))
    assert not load_state(tmp_path).is_congress_done("members", 118)
    assert result.failed == 1


def test_run_tick_proceedings_cursor_advances(tmp_path: Path) -> None:
    cfg = _config(tmp_path, entities=("proceedings",))
    loop.run_tick(cfg, run=lambda _job: 0, now=lambda: _dt(TODAY))
    assert load_state(tmp_path).proceedings_oldest_scraped == date(2026, 6, 13) - _days(29)


def test_run_tick_stop_mid_tick_skips_remaining(tmp_path: Path) -> None:
    cfg = _config(tmp_path, entities=("members", "bills", "votes"))
    seen: list[str] = []

    def _run(job: Job) -> int:
        seen.append(job.description)
        return 0

    # Stop after the first job has run.
    calls = {"n": 0}

    def _should_stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    loop.run_tick(cfg, run=_run, now=lambda: _dt(TODAY), should_stop=_should_stop)
    assert len(seen) == 1


def test_serve_once_runs_single_tick(tmp_path: Path) -> None:
    cfg = _config(tmp_path, entities=("members",))
    ticks = {"n": 0}

    def _run(job: Job) -> int:
        if job.description.endswith("forward congress 119"):
            ticks["n"] += 1
        return 0

    slept: list[float] = []
    loop.serve(
        cfg,
        interval_seconds=10.0,
        run=_run,
        once=True,
        now=lambda: _dt(TODAY),
        sleep=slept.append,
    )
    assert ticks["n"] == 1
    assert slept == []  # once => never sleeps


def test_serve_loops_until_stop(tmp_path: Path) -> None:
    cfg = _config(tmp_path, entities=("members",))
    state = {"stop_after": 2, "ticks": 0}

    def _run(_job: Job) -> int:
        return 0

    def _should_stop() -> bool:
        return state["ticks"] >= state["stop_after"]

    def _sleep(_secs: float) -> None:
        state["ticks"] += 1

    loop.serve(
        cfg,
        interval_seconds=10.0,
        run=_run,
        once=False,
        now=lambda: _dt(TODAY),
        sleep=_sleep,
        should_stop=_should_stop,
    )
    assert state["ticks"] == 2


# ---------------------------------------------------------------------------
# runner.run_job
# ---------------------------------------------------------------------------


def test_run_job_returns_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, list[str]] = {}

    class _Completed:
        returncode = 0

    def _fake_run(cmd: list[str], check: bool) -> _Completed:
        captured["cmd"] = cmd
        return _Completed()

    monkeypatch.setattr(runner.subprocess, "run", _fake_run)
    code = runner.run_job(Job(argv=("scrape", "members"), description="x"))
    assert code == 0
    assert captured["cmd"][1:] == ["-m", "concord", "scrape", "members"]


def test_run_job_survives_spawn_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(cmd: list[str], check: bool) -> object:
        raise OSError("no exec")

    monkeypatch.setattr(runner.subprocess, "run", _boom)
    assert runner.run_job(Job(argv=("scrape", "members"), description="x")) == 1


# ---------------------------------------------------------------------------
# CLI option parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("24h", 86400.0), ("90m", 5400.0), ("3600s", 3600.0), ("1d", 86400.0), ("45", 45.0)],
)
def test_parse_interval(raw: str, expected: float) -> None:
    assert _parse_interval(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "0", "-5", "10x"])
def test_parse_interval_rejects_bad(raw: str) -> None:
    with pytest.raises(typer.BadParameter):
        _parse_interval(raw)


def test_parse_csv_lower() -> None:
    assert _parse_csv_lower("House, Senate ", name="chambers") == ("house", "senate")


def _dt(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def _days(n: int) -> timedelta:
    return timedelta(days=n)
