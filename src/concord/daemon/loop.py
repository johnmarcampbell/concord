"""The Tick loop — cadence, execution, state advancement, clean stop (ADR 0026).

:func:`run_tick` executes one Tick: build the job list (pure, from config +
state + today), run each job, and apply a job's :class:`Marker` to the state
the moment its child exits 0, persisting after each advance. :func:`serve` is
the long-lived loop: Tick, then sleep ``interval``, until a stop is requested.

Everything external is injected — ``run`` (job executor), ``now`` (clock),
``sleep``, and ``should_stop`` — matching Concord's explicit-injection style
(``transport=``, ``sleep=``, ``progress=``). That keeps the loop fully testable
with no real subprocesses, no real clock, and no real signals.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from concord.daemon.plan import (
    CongressDone,
    DaemonConfig,
    Job,
    ProceedingsCursor,
    StateView,
    build_tick,
)
from concord.daemon.state import DaemonState, load_state, save_state

_log = logging.getLogger("concord.daemon.loop")


@dataclass
class TickResult:
    """Outcome of one Tick: counts for the summary log line."""

    total: int = 0
    succeeded: int = 0
    failed: int = 0


def _state_view(state: DaemonState) -> StateView:
    """Project the durable state into the planner's read-only view."""
    done = frozenset(
        (entity, c) for entity, congresses in state.congress_backfilled.items() for c in congresses
    )
    return StateView(congress_done=done, proceedings_oldest=state.proceedings_oldest_scraped)


def _apply_marker(state: DaemonState, job: Job) -> None:
    """Advance the durable state to reflect ``job``'s successful completion."""
    marker = job.marker
    if isinstance(marker, CongressDone):
        state.mark_congress_done(marker.entity, marker.congress)
    elif isinstance(marker, ProceedingsCursor):
        # Walk the cursor strictly backward; never let a stale plan move it forward.
        current = state.proceedings_oldest_scraped
        if current is None or marker.oldest < current:
            state.proceedings_oldest_scraped = marker.oldest


def run_tick(
    config: DaemonConfig,
    *,
    run: Callable[[Job], int],
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    should_stop: Callable[[], bool] = lambda: False,
) -> TickResult:
    """Execute one Tick. Loads state, runs the planned jobs, persists progress.

    State is reloaded at the top of every Tick so an operator's hand-edit of
    ``daemon_state.json`` between Ticks is honoured. A successful job's marker
    is applied and the file rewritten immediately, so a crash never loses an
    advance already earned. ``should_stop`` is polled between jobs so a
    graceful stop requested mid-Tick finishes the in-flight child and skips the
    remaining jobs (ADR 0026).
    """
    today = now().date()
    state = load_state(config.data_dir)
    jobs = build_tick(config, _state_view(state), today)
    result = TickResult(total=len(jobs))
    _log.info("tick %s: %d job(s) planned", today.isoformat(), len(jobs))

    for job in jobs:
        if should_stop():
            _log.info("stop requested mid-tick; skipping remaining jobs")
            break
        code = run(job)
        if code == 0:
            result.succeeded += 1
            if job.marker is not None:
                _apply_marker(state, job)
                save_state(config.data_dir, state)
        else:
            result.failed += 1

    _log.info(
        "tick %s done: %d ok, %d failed of %d",
        today.isoformat(),
        result.succeeded,
        result.failed,
        result.total,
    )
    return result


def serve(
    config: DaemonConfig,
    *,
    interval_seconds: float,
    run: Callable[[Job], int],
    once: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None],
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    """Run Ticks forever (or once), sleeping ``interval_seconds`` between them.

    Runs a Tick immediately on entry, then sleeps. ``once=True`` runs exactly
    one Tick and returns. ``should_stop`` is polled before each Tick and is how
    a SIGTERM/SIGINT handler requests a graceful exit — the in-flight job
    finishes (it's a child process the loop is blocked on) and the loop then
    stops without starting another Tick.
    """
    while True:
        if should_stop():
            _log.info("stop requested; exiting before next tick")
            return
        run_tick(config, run=run, now=now, should_stop=should_stop)
        if once:
            return
        if should_stop():
            _log.info("stop requested; exiting after tick")
            return
        _log.info("sleeping %.0fs until next tick", interval_seconds)
        sleep(interval_seconds)
