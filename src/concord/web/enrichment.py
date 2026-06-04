"""Web-initiated Stage 0 enrichment for one Bill (ADR 0016).

The "Request enrichment" button's whole server side: the opt-in flag
reader, the profile-page state computation, the two HTMX routes (POST to
enqueue, GET to poll), and the background-task body that runs
``scrape_enrichment`` → ``load_one`` → ``reindex_one`` for a single Bill.

``create_app`` owns the two gates (``CONGRESS_API_KEY`` +
``CONCORD_ENABLE_WEB_ENRICHMENT``) and the in-flight set / lock on
``app.state``; this module only reads them. Single-worker only — a
multi-worker deployment would need a SQLite-backed in-flight table.
"""

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from concord.scraper.bills import BILL_ENRICHMENT_SECTIONS
from concord.web._deps import VALID_BILL_TYPES, get_db

_log = logging.getLogger("concord.web")

#: Recognized truthy values for ``CONCORD_ENABLE_WEB_ENRICHMENT``. Anything
#: else (unset, empty, "0", "no", "banana") leaves enrichment disabled.
_ENRICHMENT_FLAG_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _read_enrichment_flag(raw: str | None) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in _ENRICHMENT_FLAG_TRUTHY


def _compute_enrichment_state(
    app: FastAPI,
    bill: dict[str, Any],
    bill_id: str,
) -> tuple[str | None, str | None]:
    """Pick the partial-name and error string for the profile page's button.

    Returns ``(state_partial, error)`` where ``state_partial`` is the
    template basename to include (or ``None`` when the button shouldn't
    render — disabled, or fully enriched and no error). ``error`` is the
    last failure message when state is ``"_enrichment_failed"``, else
    ``None``.
    """
    if not app.state.enrichment_enabled:
        return None, None
    if bill_id in app.state.enrichment_in_flight:
        return "_enrichment_in_flight", None
    # All five *_fetched_at columns populated wins over any recorded
    # error: an error from a prior attempt is "stale" once a later
    # success (via the button, the CLI, or any other load/index pass)
    # fills in every section. The fetched_at columns are the actual
    # source of truth for what's loaded; ``last_enrichment_error`` is
    # an annotation on top.
    any_missing = any(
        bill.get(f"{section}_fetched_at") is None for section in BILL_ENRICHMENT_SECTIONS
    )
    if not any_missing:
        return None, None
    last_error = bill.get("last_enrichment_error")
    if last_error:
        return "_enrichment_failed", last_error
    return "_enrichment_button", None


def register_enrichment_routes(app: FastAPI) -> None:  # noqa: C901 — FastAPI route declarations
    """Register the two web-initiated enrichment routes (ADR 0016)."""
    templates: Jinja2Templates = app.state.templates

    @app.post(
        "/bills/{congress}/{bill_type}/{bill_number}/enrichment",
        response_class=HTMLResponse,
    )
    async def request_enrichment(
        request: Request,
        background_tasks: BackgroundTasks,
        congress: int,
        bill_type: str,
        bill_number: int,
    ) -> Response:
        bt = bill_type.lower()
        if bt not in VALID_BILL_TYPES:
            raise HTTPException(status_code=404, detail=f"unknown bill type: {bill_type}")
        bill_id = f"{congress}-{bt}-{bill_number}"
        # Refuse to enqueue enrichment for a bill that isn't in the local
        # store. Without this check, a hand-crafted POST for an unknown
        # bill would still pay 5 sub-endpoint calls upstream; ADR 0006
        # snapshots would land in JSONL but `load_one` would no-op
        # because there's no parent ``bills`` row to attach tier-2 data
        # to.
        if not _bill_row_exists(request.app.state.db_path, bill_id):
            raise HTTPException(status_code=404, detail=f"unknown bill: {bill_id}")
        state = request.app.state
        async with state.enrichment_lock:
            if bill_id not in state.enrichment_in_flight:
                state.enrichment_in_flight.add(bill_id)
                background_tasks.add_task(_enrich_one_bill, request.app, bill_id)
        return templates.TemplateResponse(
            request,
            "bills/_enrichment_in_flight.html",
            {
                "bill": {"congress": congress, "bill_type": bt, "bill_number": bill_number},
            },
        )

    @app.get(
        "/bills/{congress}/{bill_type}/{bill_number}/enrichment-status",
        response_class=HTMLResponse,
    )
    def enrichment_status(
        request: Request,
        congress: int,
        bill_type: str,
        bill_number: int,
        db: sqlite3.Connection = Depends(get_db),  # noqa: B008 - FastAPI Depends pattern
    ) -> Response:
        bt = bill_type.lower()
        if bt not in VALID_BILL_TYPES:
            raise HTTPException(status_code=404, detail=f"unknown bill type: {bill_type}")
        bill_id = f"{congress}-{bt}-{bill_number}"
        context = {
            "bill": {"congress": congress, "bill_type": bt, "bill_number": bill_number},
        }
        if bill_id in request.app.state.enrichment_in_flight:
            return templates.TemplateResponse(request, "bills/_enrichment_in_flight.html", context)
        # Read the bill row and key off (a) the five *_fetched_at
        # columns, then (b) any recorded error. All five populated wins
        # over a stale error: a later success via the CLI or any other
        # out-of-band load pass fills in the columns but leaves any
        # prior error annotation in place, and we don't want that
        # annotation to override the observable "this bill is fully
        # enriched" state.
        row = db.execute(
            "SELECT cosponsors_fetched_at, actions_fetched_at, subjects_fetched_at, "
            "       titles_fetched_at, summaries_fetched_at, last_enrichment_error "
            "FROM bills WHERE bill_id = ?",
            (bill_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown bill: {bill_id}")
        any_missing = any(row[f"{s}_fetched_at"] is None for s in BILL_ENRICHMENT_SECTIONS)
        if not any_missing:
            return templates.TemplateResponse(request, "bills/_enrichment_done.html", context)
        last_error = row["last_enrichment_error"]
        if last_error:
            return templates.TemplateResponse(
                request,
                "bills/_enrichment_failed.html",
                {**context, "enrichment_error": last_error},
            )
        # No error recorded but not all sections populated — the
        # background task either hasn't started yet (race against this
        # poll), crashed without recording (shouldn't happen given the
        # outer try/finally; harmless if it does), or the process
        # restarted mid-job. Either way: show the button again so the
        # user can retry the still-NULL sections.
        return templates.TemplateResponse(request, "bills/_enrichment_button.html", context)


def _bill_row_exists(db_path: Path, bill_id: str) -> bool:
    """Return True iff ``bill_id`` has a row in the local ``bills`` table.

    Used to gate the enrichment POST so a hand-crafted request for a
    bill that isn't in the store can't trigger 5 upstream sub-endpoint
    calls only to no-op at the loader step.
    """
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("SELECT 1 FROM bills WHERE bill_id = ? LIMIT 1", (bill_id,))
        return cur.fetchone() is not None
    finally:
        conn.close()


def _enrich_one_bill(app: FastAPI, bill_id: str) -> None:
    """Background-task body: scrape → load_one → reindex_one for one bill.

    Runs synchronously inside FastAPI's ``BackgroundTasks`` threadpool;
    network calls happen on a worker thread so the event loop is not
    blocked. Any exception is captured on
    ``bills.last_enrichment_error``; per-section failures returned by
    the scraper (``EnrichStats.section_failures``) also surface as an
    error so partial-failure runs don't masquerade as success. The
    in-flight set is always cleared in the outermost ``finally`` so a
    crash anywhere in the job (including the initial error-clear) can't
    strand the button in the in-flight state.
    """
    from concord.api import Client  # noqa: PLC0415 — defer heavy import to first enrichment click
    from concord.pipeline import index_bills, load_bills  # noqa: PLC0415
    from concord.scraper.bills import scrape_enrichment  # noqa: PLC0415
    from concord.storage.sqlite import SqliteStorage  # noqa: PLC0415

    db_path: Path = app.state.db_path
    storage_dir: Path = app.state.storage_dir

    try:
        try:
            storage = SqliteStorage(db_path, load_vec=False)
            try:
                storage.clear_bill_enrichment_error(bill_id)
            finally:
                storage.close()

            congress_s, bill_type, bill_number_s = bill_id.split("-", 2)
            key = (int(congress_s), bill_type, int(bill_number_s))
            with Client() as client:
                stats = scrape_enrichment(
                    client=client,
                    bill_keys=[key],
                    storage_dir=storage_dir,
                    fetched_at=datetime.now(UTC),
                )
            load_bills.load_one(storage_dir=storage_dir, db_path=db_path, bill_id=bill_id)
            index_bills.reindex_one(db_path=db_path, bill_id=bill_id)
        except Exception as exc:
            _log.warning("enrichment failed for %s: %s", bill_id, exc)
            _record_enrichment_error(db_path, bill_id, str(exc)[:500])
            return
        # The scraper swallows per-section exceptions and surfaces them
        # via section_failures; a non-zero count means some upstream
        # sub-endpoint refused to answer. Report it so the UI shows
        # "failed" instead of "done" — the per-section *_fetched_at
        # columns will still be NULL for the failed sections, which the
        # status endpoint also keys off.
        if stats.section_failures > 0:
            msg = f"{stats.section_failures} section(s) failed during enrichment"
            _log.warning("enrichment for %s: %s", bill_id, msg)
            _record_enrichment_error(db_path, bill_id, msg)
    finally:
        app.state.enrichment_in_flight.discard(bill_id)


def _record_enrichment_error(db_path: Path, bill_id: str, message: str) -> None:
    """Write ``last_enrichment_error`` for one bill, best-effort.

    A failure to record the error must not strand the caller's in-flight
    set discard; the outer ``finally`` in :func:`_enrich_one_bill` runs
    regardless. We swallow exceptions here so a stale-schema or
    permissions failure on the error column doesn't propagate.
    """
    from concord.storage.sqlite import SqliteStorage  # noqa: PLC0415

    try:
        storage = SqliteStorage(db_path, load_vec=False)
        try:
            storage.set_bill_enrichment_error(bill_id, message)
        finally:
            storage.close()
    except Exception:
        _log.exception("failed to record enrichment error for %s", bill_id)
