"""Build one Tick's job list — pure, no I/O (ADR 0026).

Given a :class:`DaemonConfig` and the current :class:`DaemonState`, produce an
ordered list of :class:`Job`s: each is one ``concord`` CLI invocation the
daemon will spawn (see :mod:`concord.daemon.runner`). Jobs that, on success,
advance backfill progress carry a :class:`Marker` the loop applies to the
state (see :mod:`concord.daemon.loop`).

This module decides *what* to run; it never runs anything, reads no files, and
holds no clock — ``today`` is passed in — so a Tick's whole job list is a pure
function of (config, state, today) and is exhaustively unit-testable.

Per ADR 0026: each entity gets, in order, its forward scrape, up to
``backfill_per_tick`` backfill scrapes, then a single ``load`` and ``index``.
``load`` and ``index`` are entity-global (they ingest the whole JSONL / index
everything outstanding), so one pair per entity converges both the forward and
the backfill data scraped earlier in the same Tick — no per-chunk load/index
needed. Argv is built to match each subcommand's real flags exactly; an
unknown flag would make a child exit non-zero.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path


@dataclass(frozen=True)
class CongressDone:
    """Marker: ``entity``'s backfill of ``congress`` completed."""

    entity: str
    congress: int


@dataclass(frozen=True)
class ProceedingsCursor:
    """Marker: Proceedings backfill has now reached ``oldest`` (inclusive)."""

    oldest: date


#: A job's on-success state mutation, or ``None`` for jobs that don't advance
#: backfill (forward scrapes, loads, indexes).
Marker = CongressDone | ProceedingsCursor | None


@dataclass(frozen=True)
class Job:
    """One ``concord`` CLI invocation in a Tick.

    ``argv`` is the argument vector *after* the executable (e.g.
    ``("scrape", "members", "--congresses", "119", …)``); the runner prepends
    ``sys.executable -m concord``. ``marker`` is applied to the daemon state
    only if the child exits 0.
    """

    argv: tuple[str, ...]
    description: str
    marker: Marker = None


@dataclass(frozen=True)
class DaemonConfig:
    """Resolved daemon configuration for a deployment (ADR 0026)."""

    data_dir: Path
    db_path: Path
    congresses: tuple[int, ...]
    bill_types: tuple[str, ...]
    chambers: tuple[str, ...]
    proceedings_since: date
    proceedings_window_days: int
    proceedings_forward_days: int
    backfill_per_tick: int
    enrich_bills: bool
    #: Whether to run ``index proceedings`` (embeddings) — gated on OPENAI_API_KEY.
    index_proceedings: bool
    entities: tuple[str, ...] = ("proceedings", "members", "bills", "votes")

    @property
    def current_congress(self) -> int:
        """The newest target Congress — kept fresh every Tick by the forward pass."""
        return max(self.congresses)

    @property
    def backfill_congresses(self) -> tuple[int, ...]:
        """Older target Congresses, newest-first (the backfill targets)."""
        return tuple(
            sorted((c for c in self.congresses if c != self.current_congress), reverse=True)
        )

    # Path helpers — explicit so the daemon honours a non-default --data-dir.
    @property
    def _proceedings_jsonl(self) -> str:
        return str(self.data_dir / "proceedings.jsonl")

    @property
    def _members_jsonl(self) -> str:
        return str(self.data_dir / "members.jsonl")

    @property
    def _db(self) -> str:
        return str(self.db_path)

    @property
    def _storage_dir(self) -> str:
        return str(self.data_dir)


def next_proceedings_window(
    config: DaemonConfig, state_oldest: date | None, today: date
) -> tuple[date, date] | None:
    """Return the next ``(start, end)`` backfill window, or ``None`` if caught up.

    Windows walk backward from just before the forward pass's reach (or the
    cursor, once backfill has started) down to ``config.proceedings_since``.
    The returned range is inclusive and at most ``proceedings_window_days``
    wide; ``start`` is clamped to the floor.
    """
    forward_floor = today - timedelta(days=config.proceedings_forward_days)
    top = state_oldest if state_oldest is not None else forward_floor
    end = top - timedelta(days=1)
    if end < config.proceedings_since:
        return None
    start = max(end - timedelta(days=config.proceedings_window_days - 1), config.proceedings_since)
    return start, end


def _proceedings_jobs(config: DaemonConfig, state_oldest: date | None, today: date) -> list[Job]:
    jsonl, db = config._proceedings_jsonl, config._db
    forward_from = (today - timedelta(days=config.proceedings_forward_days)).isoformat()
    jobs: list[Job] = [
        Job(
            argv=(
                "scrape",
                "proceedings",
                "--from",
                forward_from,
                "--to",
                today.isoformat(),
                "--storage",
                jsonl,
                "--db",
                db,
                "--no-progress",
            ),
            description=f"proceedings forward {forward_from}..{today.isoformat()}",
        )
    ]
    cursor = state_oldest
    for _ in range(config.backfill_per_tick):
        window = next_proceedings_window(config, cursor, today)
        if window is None:
            break
        start, end = window
        jobs.append(
            Job(
                argv=(
                    "scrape",
                    "proceedings",
                    "--from",
                    start.isoformat(),
                    "--to",
                    end.isoformat(),
                    "--storage",
                    jsonl,
                    "--db",
                    db,
                    "--no-progress",
                ),
                description=f"proceedings backfill {start.isoformat()}..{end.isoformat()}",
                marker=ProceedingsCursor(start),
            )
        )
        cursor = start  # plan the *next* window as if this one has landed
    jobs.append(
        Job(
            argv=("load", "proceedings", "--jsonl", jsonl, "--db", db, "--no-progress"),
            description="proceedings load",
        )
    )
    if config.index_proceedings:
        jobs.append(
            Job(
                argv=("index", "proceedings", "--db", db, "--no-progress"),
                description="proceedings index",
            )
        )
    return jobs


def _members_jobs(config: DaemonConfig, state: "StateView") -> list[Job]:
    jsonl, db = config._members_jsonl, config._db
    cur = str(config.current_congress)
    jobs: list[Job] = [
        Job(
            argv=(
                "scrape",
                "members",
                "--congresses",
                cur,
                "--storage",
                jsonl,
                "--db",
                db,
                "--skip-unchanged",
                "--no-progress",
            ),
            description=f"members forward congress {cur}",
        )
    ]
    for congress in _pick_backfill(config, state, "members"):
        jobs.append(
            Job(
                argv=(
                    "scrape",
                    "members",
                    "--congresses",
                    str(congress),
                    "--storage",
                    jsonl,
                    "--db",
                    db,
                    "--no-progress",
                ),
                description=f"members backfill congress {congress}",
                marker=CongressDone("members", congress),
            )
        )
    jobs.append(
        Job(argv=("load", "members", "--storage", jsonl, "--db", db), description="members load")
    )
    jobs.append(Job(argv=("index", "members", "--db", db), description="members index"))
    return jobs


def _bills_jobs(config: DaemonConfig, state: "StateView") -> list[Job]:
    storage_dir, db = config._storage_dir, config._db
    cur = str(config.current_congress)
    bill_types = ",".join(config.bill_types)
    jobs: list[Job] = [
        Job(
            argv=(
                "scrape",
                "bills",
                "--congresses",
                cur,
                "--bill-types",
                bill_types,
                "--storage-dir",
                storage_dir,
                "--db",
                db,
                "--skip-unchanged",
                "--no-progress",
            ),
            description=f"bills forward congress {cur}",
        )
    ]
    if config.enrich_bills:
        jobs.append(
            Job(
                argv=(
                    "scrape",
                    "bills",
                    "enrich",
                    "--db",
                    db,
                    "--storage-dir",
                    storage_dir,
                    "--skip-unchanged",
                ),
                description="bills enrich (auto-select stale sections)",
            )
        )
    for congress in _pick_backfill(config, state, "bills"):
        jobs.append(
            Job(
                argv=(
                    "scrape",
                    "bills",
                    "--congresses",
                    str(congress),
                    "--bill-types",
                    bill_types,
                    "--storage-dir",
                    storage_dir,
                    "--db",
                    db,
                    "--no-progress",
                ),
                description=f"bills backfill congress {congress}",
                marker=CongressDone("bills", congress),
            )
        )
    jobs.append(
        Job(
            argv=("load", "bills", "--storage-dir", storage_dir, "--db", db),
            description="bills load",
        )
    )
    jobs.append(Job(argv=("index", "bills", "--db", db), description="bills index"))
    return jobs


def _votes_jobs(config: DaemonConfig, state: "StateView") -> list[Job]:
    storage_dir, db = config._storage_dir, config._db
    cur = str(config.current_congress)
    chambers = ",".join(config.chambers)
    jobs: list[Job] = [
        Job(
            argv=(
                "scrape",
                "votes",
                "--congresses",
                cur,
                "--chambers",
                chambers,
                "--storage-dir",
                storage_dir,
                "--db",
                db,
                "--skip-unchanged",
                "--no-progress",
            ),
            description=f"votes forward congress {cur}",
        )
    ]
    for congress in _pick_backfill(config, state, "votes"):
        jobs.append(
            Job(
                argv=(
                    "scrape",
                    "votes",
                    "--congresses",
                    str(congress),
                    "--chambers",
                    chambers,
                    "--storage-dir",
                    storage_dir,
                    "--db",
                    db,
                    "--no-progress",
                ),
                description=f"votes backfill congress {congress}",
                marker=CongressDone("votes", congress),
            )
        )
    jobs.append(
        Job(
            argv=("load", "votes", "--storage-dir", storage_dir, "--db", db),
            description="votes load",
        )
    )
    jobs.append(Job(argv=("index", "votes", "--db", db), description="votes index"))
    return jobs


@dataclass
class StateView:
    """The slice of :class:`~concord.daemon.state.DaemonState` the planner reads.

    A tiny seam so :func:`build_tick` can be tested without constructing the
    full Pydantic state: it needs only "is this congress done?" and the
    proceedings cursor.
    """

    congress_done: frozenset[tuple[str, int]] = field(default_factory=frozenset)
    proceedings_oldest: date | None = None

    def is_congress_done(self, entity: str, congress: int) -> bool:
        return (entity, congress) in self.congress_done


def _pick_backfill(config: DaemonConfig, state: StateView, entity: str) -> list[int]:
    """The up-to-``backfill_per_tick`` oldest-not-done Congresses for ``entity``."""
    pending = [c for c in config.backfill_congresses if not state.is_congress_done(entity, c)]
    return pending[: config.backfill_per_tick]


def build_tick(config: DaemonConfig, state: StateView, today: date) -> list[Job]:
    """Build the full ordered job list for one Tick (pure)."""
    jobs: list[Job] = []
    if "proceedings" in config.entities:
        jobs += _proceedings_jobs(config, state.proceedings_oldest, today)
    if "members" in config.entities:
        jobs += _members_jobs(config, state)
    if "bills" in config.entities:
        jobs += _bills_jobs(config, state)
    if "votes" in config.entities:
        jobs += _votes_jobs(config, state)
    return jobs
