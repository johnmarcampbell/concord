"""Member browse index (``/members``) and profile (``/members/{bioguide_id}``)."""

import sqlite3
from datetime import date

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from concord.web import search as search_mod
from concord.web._deps import get_db

#: Page size for the ``/members`` browse-only index.
MEMBERS_PAGE_SIZE = 50

#: Cap on Recent-votes rows on the Member profile.
MEMBER_RECENT_VOTES_LIMIT = 25

#: Members with fewer than this many party-unity votes get a
#: "(not enough votes yet)" treatment instead of a percentage.
#: Mirrors :data:`concord.pipeline.index_votes.PARTY_UNITY_MIN_VOTES`.
_PARTY_UNITY_MIN_VOTES = 10


def register(app: FastAPI) -> None:
    """Register the member index and profile routes on ``app``."""
    templates: Jinja2Templates = app.state.templates

    @app.get("/members", response_class=HTMLResponse)
    def members_index(
        request: Request,
        chamber: str | None = Query(None, description="'house', 'senate', or omitted."),
        party: str | None = Query(None, description="Filter on a party name."),
        page: int = Query(1, ge=1, description="1-based page index."),
        db: sqlite3.Connection = Depends(get_db),  # noqa: B008 - FastAPI Depends pattern
    ) -> Response:
        offset = (page - 1) * MEMBERS_PAGE_SIZE
        # Normalize chamber checkbox params: the form sends either "house",
        # "senate", or omits entirely. Reject anything else to "show all".
        chamber_filter = chamber if chamber in {"house", "senate"} else None
        rows, total = search_mod.list_current_members(
            db,
            chamber=chamber_filter,
            party=party or None,
            limit=MEMBERS_PAGE_SIZE,
            offset=offset,
        )
        context = {
            "members": rows,
            "total": total,
            "page": page,
            "page_size": MEMBERS_PAGE_SIZE,
            "has_next": offset + MEMBERS_PAGE_SIZE < total,
            "has_prev": page > 1,
            "chamber": chamber_filter or "",
            "party": party or "",
        }
        return templates.TemplateResponse(request, "members/list.html", context)

    @app.get("/members/{bioguide_id}", response_class=HTMLResponse)
    def member_profile(
        request: Request,
        bioguide_id: str,
        db: sqlite3.Connection = Depends(get_db),  # noqa: B008 - FastAPI Depends pattern
    ) -> Response:
        member = search_mod.get_member(db, bioguide_id)
        if member is None:
            return templates.TemplateResponse(
                request,
                "404.html",
                {"granule_id": bioguide_id},
                status_code=404,
            )
        terms = search_mod.terms_for_member(db, bioguide_id)
        current_term = next(
            (
                t
                for t in terms
                if t["end_date"] is None or t["end_date"] >= date.today().isoformat()
            ),
            None,
        )
        term_groups = search_mod.collapse_term_history(terms)
        sponsored = search_mod.sponsored_bills_for_member(db, bioguide_id, limit=25)
        sponsored_total = search_mod.count_sponsored_bills_for_member(db, bioguide_id)
        cosponsored = search_mod.cosponsored_bills_for_member(db, bioguide_id, limit=25)
        cosponsored_total = search_mod.count_cosponsored_bills_for_member(db, bioguide_id)

        recent_votes = search_mod.recent_votes_for_member(
            db, bioguide_id, limit=MEMBER_RECENT_VOTES_LIMIT
        )
        party_unity_rows = search_mod.party_unity_for_member(db, bioguide_id)
        modal_vote_party = search_mod.member_modal_vote_party(db, bioguide_id)

        return templates.TemplateResponse(
            request,
            "members/profile.html",
            {
                "member": member,
                "terms": terms,
                "term_groups": term_groups,
                "current_term": current_term,
                "sponsored_bills": sponsored,
                "sponsored_bills_total": sponsored_total,
                "cosponsored_bills": cosponsored,
                "cosponsored_bills_total": cosponsored_total,
                "recent_votes": recent_votes,
                "party_unity_rows": party_unity_rows,
                "modal_vote_party": modal_vote_party,
                "party_unity_min_votes": _PARTY_UNITY_MIN_VOTES,
            },
        )
