"""Domain models for the Scrape Run ledger (ADR 0021).

A **Scrape Run** records one Stage-0 execution for one entity: per-endpoint
counts of successful requests and a **Run Event** for every request that hit
an error. Unlike the upstream entities in this package, these are
Concord-*originated* records — there is no ``from_congress_api`` factory; they
are produced by the :class:`~concord.observability.Recorder` and persisted by
the SQLite ledger.

These types are the single canonical shape of "one Scrape Run": the
:mod:`concord.storage.sqlite` ledger projects a :class:`RunRecord` into the
``runs`` row + ``run_events`` rows, and the ``runs.jsonl`` cold backup
serializes *the same object*. Both representations are built from one value,
so they cannot drift (the failure this prevents: a hand-maintained dict whose
key — e.g. ``message`` vs ``msg`` — silently diverges from the producer).
"""

from pydantic import BaseModel, ConfigDict


class Attempt(BaseModel):
    """One non-success try within a logical request's retry loop.

    Exactly one of ``status`` / ``transport_class`` is set: ``status`` for an
    HTTP response (including 429s and 5xx), ``transport_class`` for a
    transport-level failure (the exception class name, e.g. ``ConnectError``).
    ``n`` is the 1-based position within the request's attempt sequence.
    """

    model_config = ConfigDict(extra="ignore")

    n: int
    status: int | None
    transport_class: str | None
    message: str


class RunEvent(BaseModel):
    """The detail record of one error-encountering logical request.

    Emitted iff a request had ≥1 non-success attempt (a first-try success is
    aggregated as a count instead). ``final_status`` is ``"resolved"`` when a
    later retry succeeded, ``"failed"`` when the request gave up / raised. The
    ``attempts`` list is capped with ``overflow_count`` carrying the remainder
    so a heavily rate-limited request can't store an unbounded array.
    ``endpoint_bucket`` and the other field names match the ``run_events``
    columns one-for-one.
    """

    model_config = ConfigDict(extra="ignore")

    endpoint_bucket: str
    attempts: list[Attempt]
    overflow_count: int
    final_status: str
    ts: str


class RunRecord(BaseModel):
    """One persisted Scrape Run: the ``runs`` row plus its ``run_events``.

    Field names match the ``runs`` columns one-for-one, except ``events``,
    which the ledger fans out into the child ``run_events`` table (and which
    the JSONL backup nests inline). ``status`` is ``ok`` / ``partial`` /
    ``error``; ``throttle_counts`` is reserved (always ``None`` today).
    """

    model_config = ConfigDict(extra="ignore")

    run_id: str
    entity: str
    command: str
    started_at: str
    ended_at: str | None
    status: str
    success_counts: dict[str, int]
    throttle_counts: dict[str, int] | None
    unmatched_sample: list[str]
    error_event_count: int
    events: list[RunEvent]


__all__ = ["Attempt", "RunEvent", "RunRecord"]
