"""Landing page (``/``) and federated search (``/search``) routes.

The only rate-limited routes (one OpenAI embed call per ``/search``), so
:func:`register` takes the ``Limiter``. Symbols used only by these two
routes — the embedder dependency, the bare-identifier regex, the section
options, the page/result caps, and the htmx/date helpers — live here
rather than in shared modules.
"""

import re
import sqlite3
from datetime import date
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from slowapi import Limiter

from concord.embedding import Embedder
from concord.web import search as search_mod
from concord.web._deps import get_db
from concord.web._helpers import resolve_top_bills
from concord.web.snippets import keyword_snippet, semantic_snippet

#: Limit applied to ``/search`` (one OpenAI embed call per request).
SEARCH_RATE_LIMIT = "30/minute"

#: Page size for search results.
SEARCH_PAGE_SIZE = 20

#: Cap on Member hits surfaced in a federated ``/search``. Members are an
#: at-most-N peek alongside Proceedings; deeper exploration goes through
#: ``/members``.
MEMBER_RESULT_LIMIT = 10

#: Cap on Bill hits surfaced in a federated ``/search``.
BILL_RESULT_LIMIT = 10

#: Regex that matches a bare Bill identifier like ``"HR 1234"`` or
#: ``"S.47"``. Used to detect "did you mean this Bill?" queries and
#: redirect to the Bill page when there's exactly one match across the
#: in-scope Congresses.
_BARE_BILL_RE = re.compile(
    r"^\s*(HR|HRES|HJRES|HCONRES|S|SRES|SJRES|SCONRES)\.?\s*(\d+)\s*$",
    re.IGNORECASE,
)

_SECTION_OPTIONS = (
    "Senate Section",
    "House Section",
    "Extensions of Remarks Section",
    "Daily Digest",
)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def _parse_optional_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"invalid date {value!r}: expected YYYY-MM-DD"
        ) from exc


def _get_embedder(request: Request) -> Embedder:
    return request.app.state.embedder  # type: ignore[no-any-return]


def register(app: FastAPI, limiter: Limiter) -> None:  # noqa: C901 — wraps the branchy search handler
    """Register the landing and search routes on ``app``."""
    templates: Jinja2Templates = app.state.templates

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        db: sqlite3.Connection = Depends(get_db),  # noqa: B008 - FastAPI Depends pattern
    ) -> Response:
        # Bills are the headline entity, so the landing page leads with them
        # (curated Top Bills + recently-active bills) rather than an empty
        # search prompt.
        recent_bills, bills_total = search_mod.list_bills(db, limit=6, offset=0)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "query": "",
                "top_bills": resolve_top_bills(db),
                "recent_bills": recent_bills,
                "bills_total": bills_total,
            },
        )

    @app.get("/search", response_class=HTMLResponse)
    @limiter.limit(SEARCH_RATE_LIMIT)
    def search_endpoint(  # noqa: C901, PLR0913 — one arg per FastAPI query param
        request: Request,
        q: str = Query("", description="Search query."),
        date_from: str | None = Query(None, alias="from", description="YYYY-MM-DD"),
        date_to: str | None = Query(None, alias="to", description="YYYY-MM-DD"),
        section: str | None = Query(None, description="Senate Section, House Section, etc."),
        page: int = Query(1, ge=1, description="1-based page index."),
        members_on: str = Query("on", alias="members", description="'on' to include Members."),
        bills_on: str = Query("on", alias="bills", description="'on' to include Bills."),
        proceedings_on: str = Query(
            "on", alias="proceedings", description="'on' to include Proceedings."
        ),
        db: sqlite3.Connection = Depends(get_db),  # noqa: B008 - FastAPI Depends pattern
        embedder: Embedder = Depends(_get_embedder),  # noqa: B008 - FastAPI Depends pattern
    ) -> Response:
        date_from_parsed = _parse_optional_date(date_from)
        date_to_parsed = _parse_optional_date(date_to)
        offset = (page - 1) * SEARCH_PAGE_SIZE

        include_members = members_on == "on"
        include_bills = bills_on == "on"
        include_proceedings = proceedings_on == "on"

        # -- Bare-identifier short-circuit -------------------------------
        # "HR 1" / "s.47" → redirect to the bill page when there is
        # exactly one match across in-scope Congresses. Multiple matches
        # fall through and render via search_bills below; zero matches
        # also fall through (the FTS section will handle it).
        bare_match = _BARE_BILL_RE.match(q) if q.strip() else None
        if bare_match:
            bill_type_q = bare_match.group(1).lower()
            bill_number_q = int(bare_match.group(2))
            rows = db.execute(
                "SELECT congress FROM bills WHERE bill_type = ? AND bill_number = ? "
                "ORDER BY congress DESC",
                (bill_type_q, bill_number_q),
            ).fetchall()
            if len(rows) == 1:
                congress = int(rows[0]["congress"])
                return RedirectResponse(
                    url=f"/bills/{congress}/{bill_type_q}/{bill_number_q}",
                    status_code=307,
                )

        # -- Members section ---------------------------------------------
        member_hits: list[search_mod.MemberHit] = []
        if include_members and q.strip():
            try:
                member_hits = search_mod.search_members(db, query=q, limit=MEMBER_RESULT_LIMIT)
            except sqlite3.OperationalError:
                # FTS5 query syntax errors land here; show no Members
                # rather than 500 on a malformed query.
                member_hits = []

        # -- Bills section -----------------------------------------------
        bill_hits: list[search_mod.BillHit] = []
        if include_bills and q.strip():
            try:
                bill_hits = search_mod.search_bills(db, query=q, limit=BILL_RESULT_LIMIT)
            except sqlite3.OperationalError:
                bill_hits = []

        # -- Proceedings section -----------------------------------------
        rendered_results: list[dict[str, Any]] = []
        total_proceedings = 0
        if include_proceedings:
            results = search_mod.search(
                db,
                embedder,
                query=q,
                date_from=date_from_parsed,
                date_to=date_to_parsed,
                section=section,
                limit=SEARCH_PAGE_SIZE,
                offset=offset,
            )
            total_proceedings = results.total
            for r in results.results:
                try:
                    snip = keyword_snippet(db, r.chunk_id, q) if q.strip() else ""
                except sqlite3.OperationalError:
                    snip = ""
                if not snip:
                    snip = semantic_snippet(r.chunk_text)
                rendered_results.append({"result": r, "snippet": snip})

        context = {
            "query": q,
            "from_date": date_from or "",
            "to_date": date_to or "",
            "section": section or "",
            "results": rendered_results,
            "total": total_proceedings,
            "page": page,
            "page_size": SEARCH_PAGE_SIZE,
            "has_next": include_proceedings and (offset + SEARCH_PAGE_SIZE) < total_proceedings,
            "has_prev": page > 1,
            "sections": _SECTION_OPTIONS,
            "member_hits": member_hits,
            "bill_hits": bill_hits,
            "include_members": include_members,
            "include_bills": include_bills,
            "include_proceedings": include_proceedings,
        }

        template = "_results.html" if _is_htmx(request) else "search.html"
        return templates.TemplateResponse(request, template, context)
