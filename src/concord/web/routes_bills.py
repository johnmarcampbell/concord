"""Bill browse index (``/bills``) and profile (``/bills/{congress}/...``).

The profile route is the one read path that touches three other web
modules: the enrichment-button state (:mod:`concord.web.enrichment`), the
Bill Brief fact pack (:mod:`concord.web.brief`), and the curated Top-bills
resolver (:mod:`concord.web._helpers`).
"""

import sqlite3

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from concord.web import search as search_mod
from concord.web._deps import VALID_BILL_TYPES, get_db
from concord.web._helpers import resolve_top_bills
from concord.web.brief import assemble_facts, cached_view
from concord.web.enrichment import compute_enrichment_state

#: Page size for the ``/bills`` browse-only index.
BILLS_PAGE_SIZE = 50


def register(app: FastAPI) -> None:
    """Register the bill index and profile routes on ``app``."""
    templates: Jinja2Templates = app.state.templates

    @app.get("/bills", response_class=HTMLResponse)
    def bills_index(
        request: Request,
        chamber: str | None = Query(None, description="'House', 'Senate', or omitted."),
        policy_area: str | None = Query(None, description="Filter on CRS policy area."),
        congress: int | None = Query(None, description="Filter on a Congress number."),
        sponsor: str | None = Query(None, description="Filter on sponsor Bioguide ID."),
        page: int = Query(1, ge=1, description="1-based page index."),
        db: sqlite3.Connection = Depends(get_db),  # noqa: B008 - FastAPI Depends pattern
    ) -> Response:
        offset = (page - 1) * BILLS_PAGE_SIZE
        chamber_filter = chamber if chamber in {"House", "Senate"} else None
        hits, total = search_mod.list_bills(
            db,
            chamber=chamber_filter,
            policy_area=policy_area or None,
            congress=congress,
            sponsor_bioguide_id=sponsor or None,
            limit=BILLS_PAGE_SIZE,
            offset=offset,
        )
        # "Top bills" highlight section: landing-page only — hide once
        # the user has narrowed by filter or paged past the first page.
        is_landing = (
            page == 1
            and chamber_filter is None
            and not policy_area
            and congress is None
            and not sponsor
        )
        top_bills = resolve_top_bills(db) if is_landing else []
        context = {
            "bills": hits,
            "total": total,
            "page": page,
            "page_size": BILLS_PAGE_SIZE,
            "has_next": offset + BILLS_PAGE_SIZE < total,
            "has_prev": page > 1,
            "chamber": chamber_filter or "",
            "policy_area": policy_area or "",
            "congress": congress,
            "sponsor": sponsor or "",
            "top_bills": top_bills,
        }
        return templates.TemplateResponse(request, "bills/list.html", context)

    @app.get("/bills/{congress}/{bill_type}/{bill_number}", response_class=HTMLResponse)
    def bill_profile(
        request: Request,
        congress: int,
        bill_type: str,
        bill_number: int,
        db: sqlite3.Connection = Depends(get_db),  # noqa: B008 - FastAPI Depends pattern
    ) -> Response:
        bt = bill_type.lower()
        if bt not in VALID_BILL_TYPES:
            raise HTTPException(status_code=404, detail=f"unknown bill type: {bill_type}")
        bill = search_mod.get_bill(db, congress=congress, bill_type=bt, bill_number=bill_number)
        if bill is None:
            return templates.TemplateResponse(
                request,
                "404.html",
                {"granule_id": f"{congress}/{bt}/{bill_number}"},
                status_code=404,
            )
        bill_id = bill["bill_id"]
        cosponsors = search_mod.cosponsors_for_bill(db, bill_id)
        actions = search_mod.actions_for_bill(db, bill_id)
        subjects = search_mod.subjects_for_bill(db, bill_id)
        titles = search_mod.titles_for_bill(db, bill_id)
        summaries = search_mod.summaries_for_bill(db, bill_id)
        vote_history = search_mod.vote_history_for_bill(db, bill_id)
        enrichment_state, enrichment_error = compute_enrichment_state(request.app, bill, bill_id)
        brief_enabled = request.app.state.brief_enabled
        brief_facts = None
        brief_view = None
        if brief_enabled:
            # Assemble the fact pack unconditionally: it's the deterministic
            # body of the self-contained brief card, shown whether or not an
            # executive summary has been generated yet (ADR 0020).
            brief_facts = assemble_facts(
                db,
                bill,
                cosponsors=cosponsors,
                subjects=subjects,
                actions=actions,
                vote_count=len(vote_history),
                summaries=summaries,
            )
            brief_view = cached_view(db, brief_facts, model=request.app.state.briefer.model)
        return templates.TemplateResponse(
            request,
            "bills/profile.html",
            {
                "bill": bill,
                "cosponsors": cosponsors,
                "actions": actions,
                "subjects": subjects,
                "titles": titles,
                "summaries": summaries,
                "vote_history": vote_history,
                "enrichment_enabled": request.app.state.enrichment_enabled,
                "enrichment_state": enrichment_state,
                "enrichment_error": enrichment_error,
                "brief_enabled": brief_enabled,
                "brief": brief_view,
                "facts": brief_facts,
                "brief_error": None,
            },
        )
