"""FastAPI app for the public-facing search demo.

One process. Reads from the SQLite store at ``app.state.db_path``,
embeds queries via the ``Embedder`` stashed on ``app.state.embedder``.
Per-request, opens a fresh ``sqlite3.Connection`` (with the
``sqlite-vec`` extension loaded) so WAL-mode visibility of new writes
from the pipeline is automatic.

Routes:

- ``GET /``                            — landing page (search box, empty state)
- ``GET /search?q=…&from=…&to=…&section=…&page=…``
                                       — results page; partial fragment when
                                         the request carries ``HX-Request: true``
- ``GET /proceedings/{granule_id}``    — full document view
- ``GET /healthz``                     — ``{"ok": true}``, no DB hit
- ``GET /static/{path}``               — CSS

Rate limiting (slowapi, in-process per-IP) is applied to ``/search``
only — the other routes don't call OpenAI and don't need protection.
"""

import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlite_vec  # type: ignore[import-untyped]
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from ..embedding import Embedder
from . import search as search_mod
from .snippets import keyword_snippet, semantic_snippet

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

#: Limit applied to ``/search`` (one OpenAI embed call per request).
SEARCH_RATE_LIMIT = "30/minute"

#: Page size for search results.
SEARCH_PAGE_SIZE = 20

#: Page size for the ``/members`` browse-only index.
MEMBERS_PAGE_SIZE = 50

#: Cap on Member hits surfaced in a federated ``/search``. Members are an
#: at-most-N peek alongside Proceedings; deeper exploration goes through
#: ``/members``.
MEMBER_RESULT_LIMIT = 10

_HERE = Path(__file__).parent
_TEMPLATES_DIR = _HERE / "templates"
_STATIC_DIR = _HERE / "static"


def create_app(
    db_path: Path | str,
    *,
    embedder: Embedder | None = None,
) -> FastAPI:
    """Build a fully wired FastAPI app.

    Parameters
    ----------
    db_path:
        Path to the SQLite database the pipeline produced.
    embedder:
        Optional injected :class:`Embedder`. When ``None`` (production
        default), constructs one from a default ``openai.OpenAI()``.
        Tests pass a stub to avoid network and OpenAI key requirements.
    """
    db_path = Path(db_path)

    if embedder is None:
        # Lazy import so the module is importable when openai isn't
        # available (tests inject their own embedder).
        import openai

        embedder = Embedder(openai.OpenAI())

    limiter = Limiter(key_func=get_remote_address)
    app = FastAPI(
        title="Concord",
        description="Search the Congressional Record (demo).",
        version="0.1.0",
    )
    app.state.db_path = db_path
    app.state.embedder = embedder
    app.state.limiter = limiter
    # slowapi's handler signature is narrower than Starlette's generic one;
    # the call is correct at runtime.
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.state.templates = templates

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    _register_routes(app, limiter)
    return app


# -- routes ----------------------------------------------------------------


def _register_routes(app: FastAPI, limiter: Limiter) -> None:
    templates = app.state.templates

    def get_db(request: Request) -> Iterator[sqlite3.Connection]:
        """Per-request connection with sqlite-vec loaded."""
        conn = sqlite3.connect(request.app.state.db_path)
        conn.row_factory = sqlite3.Row
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        try:
            yield conn
        finally:
            conn.close()

    def get_embedder(request: Request) -> Embedder:
        return request.app.state.embedder  # type: ignore[no-any-return]

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        return templates.TemplateResponse(  # type: ignore[no-any-return]
            request, "index.html", {"query": ""}
        )

    @app.get("/search", response_class=HTMLResponse)
    @limiter.limit(SEARCH_RATE_LIMIT)
    def search_endpoint(
        request: Request,
        q: str = Query("", description="Search query."),
        date_from: str | None = Query(None, alias="from", description="YYYY-MM-DD"),
        date_to: str | None = Query(None, alias="to", description="YYYY-MM-DD"),
        section: str | None = Query(None, description="Senate Section, House Section, etc."),
        page: int = Query(1, ge=1, description="1-based page index."),
        members_on: str = Query("on", alias="members", description="'on' to include Members."),
        proceedings_on: str = Query(
            "on", alias="proceedings", description="'on' to include Proceedings."
        ),
        db: sqlite3.Connection = Depends(get_db),  # noqa: B008 - FastAPI Depends pattern
        embedder: Embedder = Depends(get_embedder),  # noqa: B008 - FastAPI Depends pattern
    ) -> Response:
        date_from_parsed = _parse_optional_date(date_from)
        date_to_parsed = _parse_optional_date(date_to)
        offset = (page - 1) * SEARCH_PAGE_SIZE

        include_members = members_on == "on"
        include_proceedings = proceedings_on == "on"

        # -- Members section ---------------------------------------------
        member_hits: list[search_mod.MemberHit] = []
        if include_members and q.strip():
            try:
                member_hits = search_mod.search_members(db, query=q, limit=MEMBER_RESULT_LIMIT)
            except sqlite3.OperationalError:
                # FTS5 query syntax errors land here; show no Members
                # rather than 500 on a malformed query.
                member_hits = []

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
            "include_members": include_members,
            "include_proceedings": include_proceedings,
        }

        template = "_results.html" if _is_htmx(request) else "search.html"
        return templates.TemplateResponse(request, template, context)  # type: ignore[no-any-return]

    @app.get("/proceedings/{granule_id}", response_class=HTMLResponse)
    def proceeding(
        request: Request,
        granule_id: str,
        db: sqlite3.Connection = Depends(get_db),  # noqa: B008 - FastAPI Depends pattern
    ) -> Response:
        row = search_mod.get_proceeding(db, granule_id)
        if row is None:
            return templates.TemplateResponse(  # type: ignore[no-any-return]
                request,
                "404.html",
                {"granule_id": granule_id},
                status_code=404,
            )
        return templates.TemplateResponse(  # type: ignore[no-any-return]
            request, "proceeding.html", {"p": row}
        )

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
        return templates.TemplateResponse(  # type: ignore[no-any-return]
            request, "members/list.html", context
        )

    @app.get("/members/{bioguide_id}", response_class=HTMLResponse)
    def member_profile(
        request: Request,
        bioguide_id: str,
        db: sqlite3.Connection = Depends(get_db),  # noqa: B008 - FastAPI Depends pattern
    ) -> Response:
        member = search_mod.get_member(db, bioguide_id)
        if member is None:
            return templates.TemplateResponse(  # type: ignore[no-any-return]
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
        return templates.TemplateResponse(  # type: ignore[no-any-return]
            request,
            "members/profile.html",
            {"member": member, "terms": terms, "current_term": current_term},
        )

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"ok": True})


# -- helpers ---------------------------------------------------------------


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


__all__: list[Any] = [
    "MEMBERS_PAGE_SIZE",
    "MEMBER_RESULT_LIMIT",
    "SEARCH_PAGE_SIZE",
    "SEARCH_RATE_LIMIT",
    "create_app",
]
