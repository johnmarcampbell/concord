"""Bill Brief web layer (ADR 0020).

The brief feature's web orchestration: assemble the deterministic fact
pack, read-or-generate the cached brief with staleness, and the POST
route. The pure brief logic — the fact pack, the LLM call, and the
:class:`~concord.brief.BriefView` shape — lives in :mod:`concord.brief`;
this module wires it to the SQLite read layer (:mod:`concord.web.search`)
and the record-table writer (:class:`~concord.storage.sqlite.SqliteStorage`).

Both the profile GET (cached, possibly stale) and the generate POST
(fresh or cache-hit) flow through one seam — :func:`load_brief_view` and
:func:`get_or_generate_brief` — so the cache/staleness policy and the
view shape exist in exactly one place.
"""

import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from concord.brief import (
    BRIEF_PROMPT_VERSION,
    Briefer,
    BriefError,
    BriefFacts,
    BriefView,
    build_facts,
    facts_hash,
)
from concord.storage.sqlite import SqliteStorage
from concord.web import search as search_mod
from concord.web._deps import VALID_BILL_TYPES, get_db

_log = logging.getLogger("concord.web.brief")

#: Shown to the user when generation fails; the underlying exception is
#: logged, not surfaced.
_GENERATION_ERROR = "Couldn't generate a brief right now. Please try again."


def assemble_facts(agg: search_mod.BillAggregate) -> BriefFacts:
    """Build the deterministic Bill Brief fact pack (ADR 0020) from an aggregate.

    A pure projection over an already-fetched
    :class:`~concord.web.search.BillAggregate` — no database access. Buckets
    cosponsor party from the aggregate's rows, plucks the latest CRS
    summary, and counts actions / votes for the pure core
    :func:`concord.brief.build_facts`.
    """
    latest_summary = agg.summaries[-1] if agg.summaries else None
    party_counts = search_mod.cosponsor_party_counts(agg.cosponsors)
    return build_facts(
        bill=agg.bill,
        cosponsors=agg.cosponsors,
        cosponsor_party_counts=party_counts,
        subjects=agg.subjects,
        action_count=len(agg.actions),
        vote_count=len(agg.vote_history),
        latest_summary=latest_summary,
    )


def _view_from_row(row: dict[str, Any], *, stale: bool) -> BriefView:
    return BriefView(
        executive_summary=row["executive_summary"],
        lens=row["lens"],
        generated_at=row["generated_at"],
        model=row["model"],
        stale=stale,
    )


def cached_view(
    db: sqlite3.Connection,
    facts: BriefFacts,
    *,
    model: str,
    lens: str = "",
) -> BriefView | None:
    """Return the cached brief for ``(facts.bill_id, lens)`` as a stale-flagged view.

    Takes a pre-assembled fact pack so the caller can both render the
    deterministic facts and compute staleness from the same object. The
    single read path is :func:`concord.web.search.get_bill_brief`.
    """
    cached = search_mod.get_bill_brief(db, facts.bill_id, lens)
    if cached is None:
        return None
    current_hash = facts_hash(facts, model=model, prompt_version=BRIEF_PROMPT_VERSION)
    return _view_from_row(cached, stale=current_hash != cached["facts_hash"])


def get_or_generate_brief(
    db: sqlite3.Connection,
    storage: SqliteStorage,
    briefer: Briefer,
    *,
    facts: BriefFacts,
    lens: str,
) -> tuple[BriefView | None, str | None]:
    """Read-or-generate the brief for ``(facts.bill_id, lens)``.

    Returns ``(view, error)``. A cache hit on the same fact-pack hash is
    reused with no LLM call. On a generation failure we fall back to any
    existing cached brief (flagged stale) so a failed *regenerate* doesn't
    hide a still-good brief; the error is surfaced alongside it.
    """
    current_hash = facts_hash(facts, model=briefer.model, prompt_version=BRIEF_PROMPT_VERSION)
    cached = search_mod.get_bill_brief(db, facts.bill_id, lens)
    if cached is not None and cached["facts_hash"] == current_hash:
        return _view_from_row(cached, stale=False), None
    try:
        generated = briefer.generate(facts, lens=lens or None)
    except BriefError as exc:
        # exc_info=True captures the chained __cause__ (the real OpenAI /
        # network error), so the operator's log shows *why* it failed.
        _log.warning("brief generation failed for %s: %s", facts.bill_id, exc, exc_info=True)
        if cached is not None:
            # A stale brief still beats showing nothing on the error path.
            return _view_from_row(cached, stale=True), _GENERATION_ERROR
        return None, _GENERATION_ERROR
    generated_at = datetime.now(UTC).isoformat()
    storage.upsert_bill_brief(
        bill_id=facts.bill_id,
        lens=lens,
        executive_summary=generated.executive_summary,
        facts_hash=current_hash,
        model=briefer.model,
        prompt_version=BRIEF_PROMPT_VERSION,
        generated_at=generated_at,
    )
    view = BriefView(
        executive_summary=generated.executive_summary,
        lens=lens,
        generated_at=generated_at,
        model=briefer.model,
        stale=False,
    )
    return view, None


def register(app: FastAPI) -> None:
    """Register the synchronous Bill Brief generation route (ADR 0020).

    A ``def`` (not ``async``) route runs in FastAPI's threadpool, so the
    one blocking OpenAI call doesn't stall the event loop and no
    background-task machinery is needed.
    """
    templates: Jinja2Templates = app.state.templates

    @app.post(
        "/bills/{congress}/{bill_type}/{bill_number}/brief",
        response_class=HTMLResponse,
    )
    def generate_brief(
        request: Request,
        congress: int,
        bill_type: str,
        bill_number: int,
        lens: str = Form(""),
        db: sqlite3.Connection = Depends(get_db),  # noqa: B008 - FastAPI Depends pattern
    ) -> Response:
        bt = bill_type.lower()
        if bt not in VALID_BILL_TYPES:
            raise HTTPException(status_code=404, detail=f"unknown bill type: {bill_type}")
        agg = search_mod.BillAggregate.from_natural_key(
            db, congress=congress, bill_type=bt, bill_number=bill_number
        )
        if agg is None:
            raise HTTPException(
                status_code=404, detail=f"unknown bill: {congress}/{bt}/{bill_number}"
            )
        facts = assemble_facts(agg)
        with SqliteStorage(request.app.state.db_path, load_vec=False) as storage:
            view, error = get_or_generate_brief(
                db, storage, request.app.state.briefer, facts=facts, lens=lens.strip()
            )
        return templates.TemplateResponse(
            request,
            "bills/_brief.html",
            {
                "bill": {"congress": congress, "bill_type": bt, "bill_number": bill_number},
                "facts": facts,
                "brief": view,
                "brief_error": error,
                "brief_enabled": True,
            },
        )
