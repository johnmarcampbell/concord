"""``concord sync`` — one scheduled, best-effort incremental pass over all entities.

A **Sync** (CONTEXT.md → Orchestration) runs Scrape → Load → Index for
proceedings, members, bills, and votes in a single bounded pass, so an
operator's cron job or systemd timer can keep a deployment current with one
tested command. See [ADR 0026](../../docs/adr/0026-sync-command-not-resident-daemon.md)
for why this is a scheduled command rather than a resident daemon.

This module is the cross-entity composition root: it calls the four
``run_<entity>_pipeline`` functions explicitly. Per ADR 0007 there is no base
class or protocol over them — orchestration lives here, at the CLI level.
"""

import fcntl
import logging
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from concord.api import ENV_API_KEY
from concord.cli._common import DEFAULT_DB, _require_openai_key
from concord.cli.bills import DEFAULT_BILL_TYPES, run_bills_pipeline
from concord.cli.members import DEFAULT_MEMBERS_JSONL, run_members_pipeline
from concord.cli.proceedings import DEFAULT_JSONL, run_proceedings_pipeline
from concord.cli.votes import (
    DEFAULT_VOTE_CHAMBERS,
    DEFAULT_VOTE_SESSIONS,
    run_votes_pipeline,
)
from concord.congress import current_congress
from concord.observability import configure_logging

_log = logging.getLogger("concord.cli.cycle")

#: Default rolling-window size for the proceedings leg of a Sync.
DEFAULT_LOOKBACK_DAYS = 7

#: Filename of the advisory overlap lock, placed alongside the SQLite DB.
LOCK_FILENAME = ".sync.lock"


class CycleAlreadyRunningError(Exception):
    """Raised when another Sync already holds the advisory lock."""


@dataclass(frozen=True)
class EntityResult:
    """Outcome of one entity's pipeline within a Sync."""

    entity: str
    ok: bool
    error: str | None


@dataclass(frozen=True)
class CycleResult:
    """Aggregate outcome of one Sync — one :class:`EntityResult` per entity."""

    results: list[EntityResult]

    @property
    def ok(self) -> bool:
        """True iff every entity's pipeline succeeded."""
        return all(r.ok for r in self.results)


@contextmanager
def cycle_lock(lock_path: Path) -> Iterator[None]:
    """Hold an advisory ``flock`` for the duration of one Sync.

    Uses a non-blocking exclusive lock (``LOCK_EX | LOCK_NB``) so a second
    concurrent Sync bows out immediately with :class:`CycleAlreadyRunningError`
    rather than queueing behind the first. The lock is advisory and
    kernel-released when the process (and thus the file descriptor) dies, so a
    crashed Sync leaves no stale lock to clear (ADR 0026).
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CycleAlreadyRunningError(f"another Sync already holds {lock_path}") from exc
        # The lock releases when `handle` closes on context exit.
        yield


def run_cycle(
    *,
    lookback_days: int,
    db_path: Path,
    show_progress: bool,
    today: date | None = None,
) -> CycleResult:
    """Run one Sync and return a per-entity :class:`CycleResult`.

    Proceedings are scraped over a rolling ``[today - lookback_days, today]``
    window; members, bills, and votes over the **current Congress** with
    ``skip_unchanged=True`` (ADR 0015). The pass is best-effort: one entity's
    failure is captured and reported but does not abort the others (ADR 0026).

    ``today`` is injectable so the window/Congress math is deterministically
    testable; it defaults to the current UTC date. Raises
    :class:`CycleAlreadyRunningError` if another Sync already holds the lock. The
    JSONL stores and the lock live alongside ``db_path``.
    """
    resolved_today = today if today is not None else datetime.now(UTC).date()
    start = resolved_today - timedelta(days=lookback_days)
    end = resolved_today
    congress = current_congress(resolved_today)
    storage_dir = db_path.parent

    pipelines: list[tuple[str, Callable[[], None]]] = [
        (
            "proceedings",
            lambda: run_proceedings_pipeline(
                start=start,
                end=end,
                storage_path=storage_dir / DEFAULT_JSONL.name,
                db_path=db_path,
                limit=None,
                show_progress=show_progress,
                command="sync",
            ),
        ),
        (
            "members",
            lambda: run_members_pipeline(
                congresses=[congress],
                storage_path=storage_dir / DEFAULT_MEMBERS_JSONL.name,
                db_path=db_path,
                show_progress=show_progress,
                command="sync",
                skip_unchanged=True,
            ),
        ),
        (
            "bills",
            lambda: run_bills_pipeline(
                congresses=[congress],
                bill_types=list(DEFAULT_BILL_TYPES),
                storage_dir=storage_dir,
                db_path=db_path,
                limit=None,
                show_progress=show_progress,
                command="sync",
                skip_unchanged=True,
            ),
        ),
        (
            "votes",
            lambda: run_votes_pipeline(
                congresses=[congress],
                sessions=list(DEFAULT_VOTE_SESSIONS),
                chambers=list(DEFAULT_VOTE_CHAMBERS),
                storage_dir=storage_dir,
                db_path=db_path,
                limit=None,
                show_progress=show_progress,
                command="sync",
                skip_unchanged=True,
            ),
        ),
    ]

    results: list[EntityResult] = []
    with cycle_lock(storage_dir / LOCK_FILENAME):
        for name, run_pipeline in pipelines:
            typer.echo(f"=== sync: {name} ===", err=True)
            try:
                run_pipeline()
            except Exception as exc:
                # Best-effort across entities (ADR 0026): the four pipelines are
                # independent and idempotent, so a failed entity simply retries
                # next Sync. typer.Exit subclasses Exception and is captured here
                # too — a Stage 1/2 `Exit(2)` becomes a recorded failure, not an
                # aborted Sync. KeyboardInterrupt/SystemExit are BaseException and
                # still propagate.
                _log.exception("sync: %s pipeline failed", name)
                results.append(
                    EntityResult(entity=name, ok=False, error=f"{type(exc).__name__}: {exc}")
                )
            else:
                results.append(EntityResult(entity=name, ok=True, error=None))
    return CycleResult(results=results)


def sync_command(
    lookback_days: Annotated[
        int,
        typer.Option(
            "--lookback-days",
            help=(
                "Rolling window for proceedings: scrape the last N days up to today. "
                "Members, bills, and votes always cover the current Congress."
            ),
        ),
    ] = DEFAULT_LOOKBACK_DAYS,
    db_path: Annotated[
        Path,
        typer.Option(
            "--db",
            help="SQLite derived store. The JSONL stores and the sync lock live alongside it.",
        ),
    ] = DEFAULT_DB,
    show_progress: Annotated[
        bool,
        typer.Option(
            "--progress/--no-progress",
            help="Print per-stage progress to stderr throughout the Sync.",
        ),
    ] = True,
) -> None:
    """Run one Sync: a bounded, best-effort incremental pass over all four entities.

    Scrape → Load → Index for proceedings (rolling ``--lookback-days`` window),
    members, bills, and votes (current Congress, ``--skip-unchanged`` always
    on). Intended for a cron job or systemd timer — see docs/deployment.md.

    Both ``CONGRESS_API_KEY`` and ``OPENAI_API_KEY`` must be set. Exit codes:
    ``0`` all entities ok · ``1`` one or more entities failed · ``2`` a required
    API key is missing · ``75`` another Sync is already running.
    """
    configure_logging()

    if not os.environ.get(ENV_API_KEY):
        typer.echo(
            f"error: {ENV_API_KEY} is not set; required for `concord sync`",
            err=True,
        )
        raise typer.Exit(code=2)
    _require_openai_key()

    try:
        result = run_cycle(
            lookback_days=lookback_days,
            db_path=db_path,
            show_progress=show_progress,
        )
    except CycleAlreadyRunningError as exc:
        typer.echo(f"another Sync is already running; exiting ({exc}).", err=True)
        raise typer.Exit(code=75) from exc

    typer.echo("── sync summary ──", err=True)
    for entity_result in result.results:
        status = "ok" if entity_result.ok else f"FAILED — {entity_result.error}"
        typer.echo(f"  {entity_result.entity:<12} {status}", err=True)

    if not result.ok:
        failed = ", ".join(r.entity for r in result.results if not r.ok)
        typer.echo(f"✗ sync finished with failures: {failed}", err=True)
        raise typer.Exit(code=1)

    typer.echo("✓ sync complete.", err=True)
