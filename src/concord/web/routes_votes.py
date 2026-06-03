"""Vote browse index (``/votes``) and profile (``/votes/{chamber}/...``)."""

import sqlite3
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from concord.web import search as search_mod
from concord.web._deps import get_db

#: Page size for the ``/votes`` browse-only index.
VOTES_PAGE_SIZE = 50


def register(app: FastAPI) -> None:
    """Register the vote index and profile routes on ``app``."""
    templates: Jinja2Templates = app.state.templates

    @app.get("/votes", response_class=HTMLResponse)
    def votes_index(
        request: Request,
        chamber: str | None = Query(None, description="'house', 'senate', or omitted."),
        congress: int | None = Query(None, description="Filter on a Congress number."),
        result: str | None = Query(None, description="Filter on result string."),
        vote_kind: str | None = Query(None, description="'standard' or 'election'."),
        bill: str | None = Query(None, description="Substring match on bill_id."),
        page: int = Query(1, ge=1, description="1-based page index."),
        db: sqlite3.Connection = Depends(get_db),  # noqa: B008
    ) -> Response:
        offset = (page - 1) * VOTES_PAGE_SIZE
        chamber_filter = chamber if chamber in {"house", "senate"} else None
        kind_filter = vote_kind if vote_kind in {"standard", "election"} else None
        hits, total = search_mod.list_votes(
            db,
            chamber=chamber_filter,
            congress=congress,
            result=result or None,
            vote_kind=kind_filter,
            bill=bill or None,
            limit=VOTES_PAGE_SIZE,
            offset=offset,
        )
        context = {
            "votes": hits,
            "total": total,
            "page": page,
            "page_size": VOTES_PAGE_SIZE,
            "has_next": offset + VOTES_PAGE_SIZE < total,
            "has_prev": page > 1,
            "chamber": chamber_filter or "",
            "congress": congress,
            "result": result or "",
            "vote_kind": kind_filter or "",
            "bill": bill or "",
        }
        return templates.TemplateResponse(request, "votes/list.html", context)

    @app.get(
        "/votes/{chamber}/{congress}/{session}/{roll_number}",
        response_class=HTMLResponse,
    )
    def vote_profile(
        request: Request,
        chamber: str,
        congress: int,
        session: int,
        roll_number: int,
        db: sqlite3.Connection = Depends(get_db),  # noqa: B008
    ) -> Response:
        chamber_lc = chamber.lower()
        if chamber_lc not in {"house", "senate"}:
            raise HTTPException(status_code=404, detail=f"unknown chamber: {chamber}")
        if session not in {1, 2}:
            raise HTTPException(status_code=404, detail=f"invalid session: {session}")
        vote = search_mod.get_vote(
            db,
            chamber=chamber_lc,
            congress=congress,
            session=session,
            roll_number=roll_number,
        )
        if vote is None:
            return templates.TemplateResponse(
                request,
                "404.html",
                {"granule_id": f"{chamber_lc}/{congress}/{session}/{roll_number}"},
                status_code=404,
            )
        positions = search_mod.vote_positions_for_vote(db, vote["vote_id"])
        underlying_bill: dict[str, Any] | None = None
        if vote.get("bill_id"):
            br = db.execute(
                "SELECT bill_id, congress, bill_type, bill_number, title, policy_area "
                "FROM bills WHERE bill_id = ?",
                (vote["bill_id"],),
            ).fetchone()
            underlying_bill = dict(br) if br else None
        return templates.TemplateResponse(
            request,
            "votes/profile.html",
            {
                "vote": vote,
                "positions": positions,
                "underlying_bill": underlying_bill,
                "chamber": chamber_lc,
                "congress": congress,
                "session": session,
                "roll_number": roll_number,
            },
        )
