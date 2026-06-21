"""``concord daemon`` — the unsupervised scraping supervisor (ADR 0026).

Resolves CLI options into a :class:`~concord.daemon.plan.DaemonConfig`,
installs run_id-stamped logging and signal handlers, and hands off to the Tick
loop (:func:`concord.daemon.loop.serve`). Like ``serve``, this is the
composition seam where logging is configured (ADR 0021) — not at import time.

The daemon spawns the rest of the CLI as children; it does not import the
pipeline. See ADR 0026 for the scheduling and auto-backfill design.
"""

import logging
import os
import signal
import threading
from pathlib import Path
from typing import Annotated

import typer

from concord.api import ENV_API_KEY
from concord.cli._common import DEFAULT_DB, ENV_OPENAI_API_KEY, _parse_congresses, _parse_date
from concord.daemon.loop import serve
from concord.daemon.plan import DaemonConfig
from concord.daemon.runner import run_job
from concord.observability import configure_logging

_log = logging.getLogger("concord.cli.daemon")

#: Default backfill floor for Proceedings: the start of the 117th Congress,
#: matching the default ``--congresses`` set (ADR 0026).
DEFAULT_PROCEEDINGS_SINCE = "2021-01-03"

DEFAULT_DAEMON_CONGRESSES = "117,118,119"
DEFAULT_DAEMON_BILL_TYPES = "hr,hres,hjres,hconres,s,sres,sjres,sconres"
#: House only until Phase 3b lands Senate votes (ADR 0010).
DEFAULT_DAEMON_CHAMBERS = "house"
DEFAULT_DAEMON_ENTITIES = "proceedings,members,bills,votes"

_VALID_ENTITIES = ("proceedings", "members", "bills", "votes")

_SECONDS_PER_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_interval(raw: str) -> float:
    """Parse ``24h`` / ``90m`` / ``3600s`` / ``1d`` / a bare integer (seconds)."""
    text = raw.strip().lower()
    if not text:
        raise typer.BadParameter("empty --interval")
    unit = text[-1]
    if unit in _SECONDS_PER_UNIT:
        number, factor = text[:-1], _SECONDS_PER_UNIT[unit]
    else:
        number, factor = text, 1
    try:
        value = float(number)
    except ValueError as exc:
        raise typer.BadParameter(f"bad --interval {raw!r}; use e.g. 24h, 90m, 3600s") from exc
    if value <= 0:
        raise typer.BadParameter("--interval must be positive")
    return value * factor


def _parse_csv_lower(raw: str, *, name: str) -> tuple[str, ...]:
    out = tuple(tok.strip().lower() for tok in raw.split(",") if tok.strip())
    if not out:
        raise typer.BadParameter(f"no values parsed from --{name} {raw!r}")
    return out


def daemon_command(  # noqa: PLR0913 - a Typer command; each option is a distinct user-facing knob
    data_dir: Annotated[
        Path,
        typer.Option("--data-dir", help="Directory holding the JSONL stores and daemon state."),
    ] = Path("./data"),
    db_path: Annotated[
        Path,
        typer.Option("--db", help="SQLite derived store + Scrape Run ledger."),
    ] = DEFAULT_DB,
    congresses: Annotated[
        str,
        typer.Option(
            "--congresses", help="Target Congresses; newest is kept fresh, rest backfilled."
        ),
    ] = DEFAULT_DAEMON_CONGRESSES,
    bill_types: Annotated[
        str,
        typer.Option("--bill-types", help="Bill type codes to scrape."),
    ] = DEFAULT_DAEMON_BILL_TYPES,
    chambers: Annotated[
        str,
        typer.Option("--chambers", help="Vote chambers (house only until Phase 3b)."),
    ] = DEFAULT_DAEMON_CHAMBERS,
    entities: Annotated[
        str,
        typer.Option("--entities", help="Comma-separated subset of entities to drive."),
    ] = DEFAULT_DAEMON_ENTITIES,
    proceedings_since: Annotated[
        str,
        typer.Option("--proceedings-since", help="Backfill floor (YYYY-MM-DD) for Proceedings."),
    ] = DEFAULT_PROCEEDINGS_SINCE,
    proceedings_window: Annotated[
        int,
        typer.Option("--proceedings-window", help="Days per Proceedings backfill chunk."),
    ] = 30,
    proceedings_forward_days: Annotated[
        int,
        typer.Option(
            "--proceedings-forward-days", help="Trailing days the forward pass re-scrapes."
        ),
    ] = 7,
    backfill_per_tick: Annotated[
        int,
        typer.Option("--backfill-per-tick", help="Backfill chunks per entity per Tick."),
    ] = 1,
    enrich_bills: Annotated[
        bool,
        typer.Option("--enrich-bills/--no-enrich-bills", help="Also fetch tier-2 Bill sections."),
    ] = False,
    interval: Annotated[
        str,
        typer.Option("--interval", help="Cadence between Ticks (e.g. 24h, 90m, 3600s)."),
    ] = "24h",
    once: Annotated[
        bool,
        typer.Option("--once", help="Run a single Tick and exit (for testing / external cron)."),
    ] = False,
) -> None:
    """Run the unsupervised scraping daemon.

    Each Tick keeps the newest Congress / recent Proceedings fresh and advances
    one chunk of historical backfill per entity, until history is filled.
    Requires ``CONGRESS_API_KEY``; ``OPENAI_API_KEY`` is needed only to index
    Proceedings (without it, Proceedings are still scraped and loaded, but the
    embedding index step is skipped with a warning). See ADR 0026.
    """
    configure_logging()

    if not os.environ.get(ENV_API_KEY):
        typer.echo(f"error: {ENV_API_KEY} is not set; required for the daemon", err=True)
        raise typer.Exit(code=2)

    parsed_entities = _parse_csv_lower(entities, name="entities")
    unknown = [e for e in parsed_entities if e not in _VALID_ENTITIES]
    if unknown:
        raise typer.BadParameter(f"unknown --entities: {', '.join(unknown)}")

    since = _parse_date(proceedings_since)
    interval_seconds = _parse_interval(interval)

    index_proceedings = bool(os.environ.get(ENV_OPENAI_API_KEY))
    if "proceedings" in parsed_entities and not index_proceedings:
        _log.warning(
            "%s not set; will scrape + load Proceedings but skip the embedding index step",
            ENV_OPENAI_API_KEY,
        )

    config = DaemonConfig(
        data_dir=data_dir,
        db_path=db_path,
        congresses=tuple(_parse_congresses(congresses)),
        bill_types=_parse_csv_lower(bill_types, name="bill-types"),
        chambers=_parse_csv_lower(chambers, name="chambers"),
        proceedings_since=since,
        proceedings_window_days=proceedings_window,
        proceedings_forward_days=proceedings_forward_days,
        backfill_per_tick=backfill_per_tick,
        enrich_bills=enrich_bills,
        index_proceedings=index_proceedings,
        entities=parsed_entities,
    )

    stop_event = _install_signal_handlers()

    def _sleep(seconds: float) -> None:
        # Event.wait returns early (True) when a signal sets the event; we
        # ignore the bool — should_stop reports the flag on the next loop check.
        stop_event.wait(seconds)

    _log.info(
        "daemon starting: entities=%s congresses=%s interval=%.0fs%s",
        ",".join(config.entities),
        ",".join(str(c) for c in config.congresses),
        interval_seconds,
        " (once)" if once else "",
    )
    serve(
        config,
        interval_seconds=interval_seconds,
        run=run_job,
        once=once,
        sleep=_sleep,
        should_stop=stop_event.is_set,
    )
    _log.info("daemon stopped")


def _install_signal_handlers() -> threading.Event:
    """Wire SIGTERM/SIGINT to a stop :class:`~threading.Event`.

    The event doubles as the loop's interruptible sleep (``Event.wait`` returns
    early when set) and its ``should_stop`` predicate, so a signal both wakes a
    sleeping daemon and asks it to exit after the in-flight child (ADR 0026).
    """
    stop_event = threading.Event()

    def _handle(signum: int, _frame: object) -> None:
        _log.info("received signal %s; requesting graceful stop", signal.Signals(signum).name)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    return stop_event
