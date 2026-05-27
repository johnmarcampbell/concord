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

import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlite_vec  # type: ignore[import-untyped]
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from concord.embedding import Embedder

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

#: Page size for the ``/bills`` browse-only index.
BILLS_PAGE_SIZE = 50

#: Page size for the ``/votes`` browse-only index.
VOTES_PAGE_SIZE = 50

#: Cap on Recent-votes rows on the Member profile.
MEMBER_RECENT_VOTES_LIMIT = 25

#: Members with fewer than this many party-unity votes get a
#: "(not enough votes yet)" treatment instead of a percentage.
#: Mirrors :data:`concord.pipeline.index_votes.PARTY_UNITY_MIN_VOTES`.
_PARTY_UNITY_MIN_VOTES = 10

#: Cap on Member hits surfaced in a federated ``/search``. Members are an
#: at-most-N peek alongside Proceedings; deeper exploration goes through
#: ``/members``.
MEMBER_RESULT_LIMIT = 10

#: Cap on Bill hits surfaced in a federated ``/search``.
BILL_RESULT_LIMIT = 10

#: Bill type codes accepted in URL paths. Mirrors
#: :data:`concord.cli.DEFAULT_BILL_TYPES`; kept here too so the web
#: package doesn't import the CLI module.
_VALID_BILL_TYPES = frozenset({"hr", "hres", "hjres", "hconres", "s", "sres", "sjres", "sconres"})

#: Regex that matches a bare Bill identifier like ``"HR 1234"`` or
#: ``"S.47"``. Used to detect "did you mean this Bill?" queries and
#: redirect to the Bill page when there's exactly one match across the
#: in-scope Congresses.
_BARE_BILL_RE = re.compile(
    r"^\s*(HR|HRES|HJRES|HCONRES|S|SRES|SJRES|SCONRES)\.?\s*(\d+)\s*$",
    re.IGNORECASE,
)

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
        import openai  # noqa: PLC0415 — guarded so tests can import this module without openai

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
    templates.env.filters["humanize_age"] = humanize_age
    app.state.templates = templates

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    _register_routes(app, limiter)
    return app


# -- routes ----------------------------------------------------------------


def _register_routes(app: FastAPI, limiter: Limiter) -> None:  # noqa: C901, PLR0915 — FastAPI route declarations
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
        embedder: Embedder = Depends(get_embedder),  # noqa: B008 - FastAPI Depends pattern
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
        sponsored = search_mod.sponsored_bills_for_member(db, bioguide_id, limit=25)
        sponsored_total = search_mod.count_sponsored_bills_for_member(db, bioguide_id)
        cosponsored = search_mod.cosponsored_bills_for_member(db, bioguide_id, limit=25)
        cosponsored_total = search_mod.count_cosponsored_bills_for_member(db, bioguide_id)

        recent_votes = search_mod.recent_votes_for_member(
            db, bioguide_id, limit=MEMBER_RECENT_VOTES_LIMIT
        )
        party_unity_rows = search_mod.party_unity_for_member(db, bioguide_id)
        modal_vote_party = search_mod.member_modal_vote_party(db, bioguide_id)

        return templates.TemplateResponse(  # type: ignore[no-any-return]
            request,
            "members/profile.html",
            {
                "member": member,
                "terms": terms,
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
        }
        return templates.TemplateResponse(  # type: ignore[no-any-return]
            request, "bills/list.html", context
        )

    @app.get("/bills/{congress}/{bill_type}/{bill_number}", response_class=HTMLResponse)
    def bill_profile(
        request: Request,
        congress: int,
        bill_type: str,
        bill_number: int,
        db: sqlite3.Connection = Depends(get_db),  # noqa: B008 - FastAPI Depends pattern
    ) -> Response:
        bt = bill_type.lower()
        if bt not in _VALID_BILL_TYPES:
            raise HTTPException(status_code=404, detail=f"unknown bill type: {bill_type}")
        bill = search_mod.get_bill(db, congress=congress, bill_type=bt, bill_number=bill_number)
        if bill is None:
            return templates.TemplateResponse(  # type: ignore[no-any-return]
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
        return templates.TemplateResponse(  # type: ignore[no-any-return]
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
            },
        )

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
        return templates.TemplateResponse(  # type: ignore[no-any-return]
            request, "votes/list.html", context
        )

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
            return templates.TemplateResponse(  # type: ignore[no-any-return]
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
        return templates.TemplateResponse(  # type: ignore[no-any-return]
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

    @app.get("/about/methodology", response_class=HTMLResponse)
    def about_methodology(request: Request) -> Response:
        return templates.TemplateResponse(  # type: ignore[no-any-return]
            request, "about/methodology.html", {}
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


#: Coarse buckets used by :func:`humanize_age`. Order matters: each tuple
#: is ``(threshold_in_seconds, singular_unit_seconds, unit_name)``; the
#: first whose threshold the elapsed time crosses determines the unit.
_AGE_BUCKETS: tuple[tuple[int, int, str], ...] = (
    (60, 1, "second"),
    (3600, 60, "minute"),
    (86_400, 3600, "hour"),
    (2_592_000, 86_400, "day"),
    (31_536_000, 2_592_000, "month"),
    (10**18, 31_536_000, "year"),
)

#: Threshold (in seconds) below which we collapse to "just now".
_JUST_NOW_SECONDS = 30


def humanize_age(value: str | datetime | None, *, now: datetime | None = None) -> str:
    """Render an ISO 8601 timestamp as a coarse "N units ago" string.

    Returns ``"just now"`` for ages under 30 seconds, ``"in the future"``
    for future timestamps (clock skew), and an empty string for ``None``
    or unparseable input. Used by the Bill profile to label each tier-2
    section's last-fetched moment.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return ""
    else:
        parsed = value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    current = now if now is not None else datetime.now(UTC)
    delta = (current - parsed).total_seconds()
    if delta < 0:
        return "in the future"
    if delta < _JUST_NOW_SECONDS:
        return "just now"
    for threshold, unit_seconds, unit_name in _AGE_BUCKETS:
        if delta < threshold:
            count = int(delta // unit_seconds)
            plural = "" if count == 1 else "s"
            return f"{count} {unit_name}{plural} ago"
    return ""


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
    "BILLS_PAGE_SIZE",
    "BILL_RESULT_LIMIT",
    "MEMBERS_PAGE_SIZE",
    "MEMBER_RESULT_LIMIT",
    "SEARCH_PAGE_SIZE",
    "SEARCH_RATE_LIMIT",
    "create_app",
    "humanize_age",
]
