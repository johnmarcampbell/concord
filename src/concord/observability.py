"""Scrape-run observability — the ledger and its run_id-stamped logging.

This module is the home of Concord's **Scrape Run** ledger (ADR 0021). It
records, per Stage-0 execution for one entity, the count of successful
network requests bucketed by endpoint and a detailed **Run Event** for
every request that hit an error — including whether the error resolved on
retry. See ``CONTEXT.md`` ("Observability") for the term definitions.

The recorder rides ambient :mod:`contextvars` rather than injected
parameters. The :func:`scrape_run` context manager mints a run_id, sets
both contextvars at pull start, and resets + flushes in a ``finally``. Each
HTTP client's network chokepoint does a two-line
``rec = active_recorder(); if rec is not None: …`` — so the clients are a
no-op when no scrape is active (tests, the web layer). This is a deliberate
departure from Concord's otherwise-explicit injection style: run_id
correlation for logging *cannot* be threaded as a parameter into a bare
``_log.warning(...)`` deep in a retry loop — it fundamentally needs a
contextvar — and the recorder rides the same mechanism (ADR 0021).

The module is a thin shared helper, **not** a base class (ADR 0007). It is
import-safe: nothing here touches SQLite or the filesystem at import time
(:func:`scrape_run` lazy-imports the storage layer), so :mod:`concord.api`
can import :func:`active_recorder` on its hot path with no heavy cost and no
import cycle.
"""

import hashlib
import itertools
import json
import logging
import os
import re
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from concord.models.runs import Attempt, RunEvent, RunRecord

_log = logging.getLogger("concord.observability")

#: Cap on stored attempts per Run Event. A heavily rate-limited request can
#: retry hundreds of times; we keep the first N and count the rest so the
#: ``attempts`` JSON array can't grow unbounded.
_MAX_ATTEMPTS_PER_EVENT = 20

#: Cap on the number of distinct unmatched concrete paths a single run
#: samples (and warns about), so a systematically-misrouted scrape can't
#: flood the log or the ``unmatched_sample`` column.
_MAX_UNMATCHED_SAMPLE = 20


# ---------------------------------------------------------------------------
# Context variables + readers
# ---------------------------------------------------------------------------

_run_id: ContextVar[str | None] = ContextVar("concord_run_id", default=None)
_recorder: "ContextVar[Recorder | None]" = ContextVar("concord_recorder", default=None)


def current_run_id() -> str | None:
    """Return the active Scrape Run's id, or ``None`` outside a run."""
    return _run_id.get()


def active_recorder() -> "Recorder | None":
    """Return the active :class:`Recorder`, or ``None`` outside a run.

    The HTTP clients call this on every request; a ``None`` return means no
    scrape is active and the client should record nothing.
    """
    return _recorder.get()


# ---------------------------------------------------------------------------
# Endpoint route table + normalizer
# ---------------------------------------------------------------------------

#: Suffix of the bucket a path falls to when no route matches. The full
#: sentinel is ``f"{source}{_UNMATCHED_SUFFIX}"``; :func:`normalize` returns a
#: ``matched`` flag alongside the bucket so callers never have to reconstruct
#: this string to detect the unmatched case.
_UNMATCHED_SUFFIX = ":unmatched"

# Ordered (regex, template) routes per source. First match wins; the
# template is fed to ``re.Match.expand`` so a backreference like ``\1`` can
# splice a captured path segment into the bucket. A concrete path that
# matches nothing falls to ``f"{source}{_UNMATCHED_SUFFIX}"``.
#
# Concrete per-resource URLs (``/bill/119/hr/1234``) are never the key —
# that would defeat aggregation (ADR 0021). Order matters: the more specific
# routes must precede their prefixes (``…/articles`` before the list,
# ``bill/{sub}`` before ``bill/detail`` before ``bill/list``).
#
# ``"api"`` covers api.congress.gov paths; ``"text"`` and ``"senate"`` cover
# the full congress.gov / senate.gov URLs their clients fetch. The public
# shape of this table is part of the recorder's contract.
_ROUTES: dict[str, tuple[tuple[re.Pattern[str], str], ...]] = {
    "api": (
        (
            re.compile(r"^/?daily-congressional-record/\d+/\d+/articles/?$"),
            "api:daily-record/articles",
        ),
        (re.compile(r"^/?daily-congressional-record/?$"), "api:daily-record/list"),
        (re.compile(r"^/?member/congress/\d+/?$"), "api:member/list"),
        (re.compile(r"^/?bill/\d+/[a-z]+/\d+/([a-z]+)/?$"), r"api:bill/\1"),
        (re.compile(r"^/?bill/\d+/[a-z]+/\d+/?$"), "api:bill/detail"),
        (re.compile(r"^/?bill/\d+/[a-z]+/?$"), "api:bill/list"),
        (re.compile(r"^/?house-vote/\d+/\d+/\d+/members/?$"), "api:house-vote/members"),
        (re.compile(r"^/?house-vote/\d+/\d+/\d+/?$"), "api:house-vote/detail"),
        (re.compile(r"^/?house-vote/\d+/\d+/?$"), "api:house-vote/list"),
    ),
    # text.py fetches full congress.gov URLs (not api.congress.gov paths), so
    # these routes match on the whole URL. The Congressional Record article
    # tier serves one ``…/modified/CREC-*.htm`` shape — effectively the only
    # endpoint — so a single ``text:article`` bucket suffices (ADR 0021).
    "text": (
        (
            re.compile(r"^https?://(?:www\.)?congress\.gov/.+/modified/.+\.htm$"),
            "text:article",
        ),
    ),
    # senate_xml.py also fetches full URLs. The three LIS feeds map to three
    # buckets keyed on their stable path shapes (see MENU_URL/DETAIL_URL/
    # ROSTER_URL in senate_xml.py); concrete (congress, session, roll) values
    # never enter the bucket, preserving aggregation.
    "senate": (
        (re.compile(r"^https?://.+/roll_call_lists/vote_menu_\d+_\d+\.xml$"), "senate:menu"),
        (re.compile(r"^https?://.+/roll_call_votes/.+\.xml$"), "senate:detail"),
        (re.compile(r"^https?://.+/senators_cfm\.xml$"), "senate:roster"),
    ),
}


def normalize(source: str, path: str) -> tuple[str, bool]:
    """Map a concrete request ``path`` to ``(bucket, matched)``.

    ``matched`` is ``True`` when a route fired (``bucket`` is its expanded
    template) and ``False`` when nothing matched (``bucket`` is the
    ``f"{source}{_UNMATCHED_SUFFIX}"`` sentinel). Returning the flag means
    callers key off a real value instead of re-deriving and comparing the
    sentinel string — change the format here and the unmatched-detection
    can't silently stop firing. Pure: the loud sampling + ``WARNING`` for the
    unmatched case lives in :meth:`Recorder._bucket`.
    """
    for regex, template in _ROUTES.get(source, ()):
        match = regex.match(path)
        if match is not None:
            return match.expand(template), True
    return f"{source}{_UNMATCHED_SUFFIX}", False


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------


@dataclass
class Recorder:
    """Accumulates one Scrape Run's success counts and Run Events.

    A plain object (no inheritance, per ADR 0007). Lives behind the
    :data:`_recorder` contextvar; the HTTP clients call :meth:`note_success`
    and :meth:`note_request_outcome` on it. At flush, :func:`scrape_run` turns
    its accumulated state into a single :class:`RunRecord` via
    :meth:`to_run_record` — the live runtime state stays here, the
    serializable record lives in :mod:`concord.models`.
    """

    entity: str
    command: str
    started_at: datetime
    successes: dict[str, int] = field(default_factory=dict)
    events: list[RunEvent] = field(default_factory=list)
    unmatched: set[str] = field(default_factory=set)

    def note_success(self, source: str, path: str) -> None:
        """Record one successful request against its Endpoint bucket."""
        bucket = self._bucket(source, path)
        self.successes[bucket] = self.successes.get(bucket, 0) + 1

    def note_request_outcome(
        self,
        source: str,
        path: str,
        attempts: Sequence[Attempt],
        *,
        resolved: bool,
    ) -> None:
        """Record a Run Event for a request that had ≥1 non-success attempt.

        ``attempts`` is the full ordered list of failed attempts; it is capped
        at :data:`_MAX_ATTEMPTS_PER_EVENT` (keeping the earliest) with the
        remainder counted in ``overflow_count``.
        """
        bucket = self._bucket(source, path)
        capped = list(attempts[:_MAX_ATTEMPTS_PER_EVENT])
        overflow = max(0, len(attempts) - _MAX_ATTEMPTS_PER_EVENT)
        self.events.append(
            RunEvent(
                endpoint_bucket=bucket,
                attempts=capped,
                overflow_count=overflow,
                final_status="resolved" if resolved else "failed",
                ts=datetime.now(UTC).isoformat(),
            )
        )

    def to_run_record(self, *, run_id: str, ended_at: datetime, status: str) -> RunRecord:
        """Freeze the accumulated state into a single canonical :class:`RunRecord`.

        This one object is the source for both the SQLite ledger row + events
        and the ``runs.jsonl`` backup, so the two representations cannot drift.
        """
        return RunRecord(
            run_id=run_id,
            entity=self.entity,
            command=self.command,
            started_at=self.started_at.isoformat(),
            ended_at=ended_at.isoformat(),
            status=status,
            success_counts=dict(self.successes),
            throttle_counts=None,
            unmatched_sample=sorted(self.unmatched),
            error_event_count=len(self.events),
            events=list(self.events),
        )

    def _bucket(self, source: str, path: str) -> str:
        """Normalize ``path`` to a bucket, loudly sampling unmatched paths."""
        bucket, matched = normalize(source, path)
        if (
            not matched
            and path not in self.unmatched
            and len(self.unmatched) < _MAX_UNMATCHED_SAMPLE
        ):
            self.unmatched.add(path)
            _log.warning(
                "unmatched %s endpoint path %r; bucketed as %s "
                "(add a route to observability._ROUTES)",
                source,
                path,
                bucket,
            )
        return bucket


# ---------------------------------------------------------------------------
# Central run_id-stamped logging
# ---------------------------------------------------------------------------

#: Marker attribute set on our handler so :func:`configure_logging` is
#: idempotent — repeated CLI invocations in one process (tests) must not
#: stack handlers.
_HANDLER_FLAG = "_concord_run_id_handler"

_LOG_FORMAT = "%(asctime)s %(levelname)s [%(run_id)s] %(name)s: %(message)s"


class RunIdFormatter(logging.Formatter):
    """A :class:`logging.Formatter` that stamps each record with the run_id.

    Reads the :data:`_run_id` contextvar in :meth:`format`, so every existing
    ``concord.*`` log line — including the api.py retry heartbeats — gains a
    run_id with no call-site changes. ``-`` outside a run (ADR 0021).
    """

    def format(self, record: logging.LogRecord) -> str:
        record.run_id = current_run_id() or "-"
        return super().format(record)


def configure_logging(*, level: int = logging.INFO) -> None:
    """Install the run_id-stamped stderr handler on the ``concord`` logger.

    Idempotent: a second call is a no-op (the handler carries a marker
    attribute we check for). Called from the CLI callback and ``serve``
    startup only — never at import time, per ADR 0014 (the CLI is the
    contract; library imports must stay side-effect-free).

    Attaches to the ``concord`` logger (not the root) so the run_id format is
    scoped to our lines and third-party loggers keep their own. Propagation is
    left enabled: the root has no handler in normal CLI runs (so no duplicate),
    and leaving it on means ``pytest``'s ``caplog`` — which captures at the
    root — still sees ``concord.*`` records.
    """
    logger = logging.getLogger("concord")
    for handler in logger.handlers:
        if getattr(handler, _HANDLER_FLAG, False):
            return
    handler = logging.StreamHandler(sys.stderr)
    setattr(handler, _HANDLER_FLAG, True)
    handler.setFormatter(RunIdFormatter(_LOG_FORMAT))
    logger.addHandler(handler)
    logger.setLevel(level)


# ---------------------------------------------------------------------------
# Run-id minting + the scrape_run lifecycle
# ---------------------------------------------------------------------------

#: Process-local counter folded into the run_id token so two runs minted in
#: the same wall-clock second (same entity, same pid) can't collide on the
#: ``runs.run_id`` primary key. Deterministic per process (no randomness),
#: which keeps test run_ids stable. See ADR 0021's open question.
_run_counter = itertools.count()


def _mint_run_id(started_at: datetime, entity: str) -> str:
    """Mint a sortable, collision-resistant run_id.

    Format ``"{started_at:%Y%m%dT%H%M%S}-{token}"`` — the timestamp prefix
    sorts chronologically; the token is a short Blake2b digest of
    ``(started_at, entity, pid, counter)``, avoiding ``random``/``uuid4`` so
    runs are reproducible from their inputs.
    """
    seq = next(_run_counter)
    raw = f"{started_at.isoformat()}|{entity}|{os.getpid()}|{seq}"
    token = hashlib.blake2b(raw.encode("utf-8"), digest_size=4).hexdigest()
    return f"{started_at:%Y%m%dT%H%M%S}-{token}"


@contextmanager
def scrape_run(
    *,
    entity: str,
    command: str,
    db_path: Path,
    data_dir: Path | None = None,
) -> Iterator[Recorder]:
    """Own one Scrape Run's lifecycle: mint, record, flush.

    On enter: stamp ``started_at`` (UTC), mint a run_id, bootstrap the ledger
    schema (``ensure_schema``, ADR 0012 precedent), construct a
    :class:`Recorder`, set both contextvars, and yield the recorder.

    On exit (``finally``): reset both contextvars, then flush — INSERT one
    ``runs`` row + N ``run_events`` rows (DB-authoritative), then append one
    JSON line to ``<data_dir or db_path.parent>/runs.jsonl`` (cold backup).
    The flush is best-effort: a persistence error is logged, never raised, so
    it can't mask an in-flight exception from the scrape body.

    ``db_path`` is required (no default) so this module stays a leaf and free
    of any ``concord.cli`` import; the CLI call sites resolve the default.
    """
    # Lazy import keeps this module a leaf: api.py imports active_recorder on
    # its hot path, and we don't want that to drag in the SQLite layer.
    from concord.storage.sqlite import ensure_schema  # noqa: PLC0415 - keeps module a leaf

    started_at = datetime.now(UTC)
    run_id = _mint_run_id(started_at, entity)
    ensure_schema(db_path)
    recorder = Recorder(entity=entity, command=command, started_at=started_at)

    token_id = _run_id.set(run_id)
    token_rec = _recorder.set(recorder)
    body_failed = False
    try:
        yield recorder
    except BaseException:
        body_failed = True
        raise
    finally:
        _recorder.reset(token_rec)
        _run_id.reset(token_id)
        ended_at = datetime.now(UTC)
        if body_failed:
            status = "error"
        elif any(event.final_status == "failed" for event in recorder.events):
            # Reachable only when a scraper catches a request failure and
            # carries on (e.g. Bills enrich's per-section failures, and PR 2's
            # other scrapers). The Bills basic path lets an ApiError propagate,
            # so its terminal failures land in the body_failed -> "error" branch
            # above; "partial" is the forward-looking middle state.
            status = "partial"
        else:
            status = "ok"
        try:
            record = recorder.to_run_record(run_id=run_id, ended_at=ended_at, status=status)
            _flush(record, db_path=db_path, data_dir=data_dir)
        except Exception:
            _log.exception("failed to persist scrape run %s", run_id)


def _flush(record: RunRecord, *, db_path: Path, data_dir: Path | None) -> None:
    """Persist one :class:`RunRecord`: SQLite ledger (authoritative) + JSONL backup.

    The same ``record`` drives both writes, so the DB row and the cold backup
    are provably the same value.
    """
    from concord.storage.sqlite import SqliteStorage  # noqa: PLC0415 - keeps module a leaf

    # DB authoritative.
    with SqliteStorage(db_path, load_vec=False) as storage, storage.transaction():
        storage.insert_run(record)
        storage.insert_run_events(record.run_id, record.events)

    # Cold backup — never read in normal operation, disaster-recovery only
    # (ADR 0021); the full record (row + nested events) on one line. Serialized
    # with sorted keys throughout so the backup is byte-reproducible for diffing.
    backup_dir = data_dir if data_dir is not None else db_path.parent
    backup_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record.model_dump(mode="json"), sort_keys=True)
    with (backup_dir / "runs.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
