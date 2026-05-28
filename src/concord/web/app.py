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

import asyncio
import logging
import os
import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlite_vec  # type: ignore[import-untyped]
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from concord.embedding import Embedder
from concord.scraper.bills import BILL_ENRICHMENT_SECTIONS
from concord.storage.sqlite import ensure_schema
from concord.web.top_bills import CURATED_TOP_BILLS

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

_log = logging.getLogger("concord.web")

#: Recognized truthy values for ``CONCORD_ENABLE_WEB_ENRICHMENT``. Anything
#: else (unset, empty, "0", "no", "banana") leaves enrichment disabled.
_ENRICHMENT_FLAG_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _read_enrichment_flag(raw: str | None) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in _ENRICHMENT_FLAG_TRUTHY


def create_app(
    db_path: Path | str,
    *,
    embedder: Embedder | None = None,
    storage_dir: Path | str | None = None,
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
    storage_dir:
        Path to the JSONL canonical store. Defaults to ``db_path.parent``
        to match the conventional ``./data/`` layout. Only consulted by
        the web-initiated enrichment flow (ADR 0016) — readers don't need
        it.
    """
    db_path = Path(db_path)
    # First-boot bootstrap: if the DB file is missing, create it and apply
    # the schema so the rest of create_app() (and incoming requests) can
    # assume the tables exist. See ADR 0012.
    ensure_schema(db_path)

    if embedder is None:
        import openai  # noqa: PLC0415 — guarded so tests can import this module without openai

        embedder = Embedder(openai.OpenAI())

    storage_dir_path = Path(storage_dir) if storage_dir is not None else db_path.parent

    # Two gates for the web-initiated enrichment button (ADR 0016): a key
    # to call api.congress.gov with, and an explicit opt-in flag so a
    # casual deployment can't be coaxed into triggering Stage 0 from the
    # web layer just by hand-crafting the POST URL.
    has_congress_api_key = bool(os.environ.get("CONGRESS_API_KEY"))
    enrichment_flag = _read_enrichment_flag(os.environ.get("CONCORD_ENABLE_WEB_ENRICHMENT"))
    enrichment_enabled = has_congress_api_key and enrichment_flag

    limiter = Limiter(key_func=get_remote_address)
    app = FastAPI(
        title="Concord",
        description="Search the Congressional Record (demo).",
        version="0.1.0",
    )
    app.state.db_path = db_path
    app.state.storage_dir = storage_dir_path
    app.state.embedder = embedder
    app.state.limiter = limiter
    app.state.enrichment_enabled = enrichment_enabled
    # Cross-request de-dup of in-flight enrichment jobs. Single-worker
    # only — multi-worker uvicorn would need a SQLite-backed table. See
    # ADR 0016.
    app.state.enrichment_in_flight = set()
    app.state.enrichment_lock = asyncio.Lock()
    # slowapi's handler signature is narrower than Starlette's generic one;
    # the call is correct at runtime.
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.filters["humanize_age"] = humanize_age
    app.state.templates = templates

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    _register_routes(app, limiter)
    if enrichment_enabled:
        _register_enrichment_routes(app)
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
        # "Top bills" highlight section: landing-page only — hide once
        # the user has narrowed by filter or paged past the first page.
        is_landing = (
            page == 1
            and chamber_filter is None
            and not policy_area
            and congress is None
            and not sponsor
        )
        top_bills: list[dict[str, Any]] = []
        if is_landing:
            keys = [(c.congress, c.bill_type, c.bill_number) for c in CURATED_TOP_BILLS]
            resolved = search_mod.get_curated_bills(db, keys)
            for entry in CURATED_TOP_BILLS:
                key = (entry.congress, entry.bill_type, entry.bill_number)
                hit = resolved.get(key)
                if hit is None:
                    continue
                top_bills.append(
                    {
                        "bill": hit,
                        "label": entry.label,
                        "blurb": entry.blurb,
                    }
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
            "top_bills": top_bills,
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
        enrichment_state, enrichment_error = _compute_enrichment_state(request.app, bill, bill_id)
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
                "enrichment_enabled": request.app.state.enrichment_enabled,
                "enrichment_state": enrichment_state,
                "enrichment_error": enrichment_error,
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
    last_error = bill.get("last_enrichment_error")
    if last_error:
        return "_enrichment_failed", last_error
    any_missing = any(
        bill.get(f"{section}_fetched_at") is None for section in BILL_ENRICHMENT_SECTIONS
    )
    if any_missing:
        return "_enrichment_button", None
    return None, None


def _register_enrichment_routes(app: FastAPI) -> None:
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
        if bt not in _VALID_BILL_TYPES:
            raise HTTPException(status_code=404, detail=f"unknown bill type: {bill_type}")
        bill_id = f"{congress}-{bt}-{bill_number}"
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
        db: sqlite3.Connection = Depends(_db_for_status),  # noqa: B008 - FastAPI Depends pattern
    ) -> Response:
        bt = bill_type.lower()
        if bt not in _VALID_BILL_TYPES:
            raise HTTPException(status_code=404, detail=f"unknown bill type: {bill_type}")
        bill_id = f"{congress}-{bt}-{bill_number}"
        context = {
            "bill": {"congress": congress, "bill_type": bt, "bill_number": bill_number},
        }
        if bill_id in request.app.state.enrichment_in_flight:
            return templates.TemplateResponse(request, "bills/_enrichment_in_flight.html", context)
        row = db.execute(
            "SELECT last_enrichment_error FROM bills WHERE bill_id = ?",
            (bill_id,),
        ).fetchone()
        last_error = row["last_enrichment_error"] if row is not None else None
        if last_error:
            return templates.TemplateResponse(
                request,
                "bills/_enrichment_failed.html",
                {**context, "enrichment_error": last_error},
            )
        return templates.TemplateResponse(request, "bills/_enrichment_done.html", context)


def _db_for_status(request: Request) -> Iterator[sqlite3.Connection]:
    """Per-request connection for the enrichment-status endpoint.

    The bulk-search ``get_db`` is defined as a nested closure inside
    :func:`_register_routes`, so the enrichment-status endpoint (registered
    in a separate helper) gets its own equivalent here. No sqlite-vec
    extension load — the status query is one indexed SELECT.
    """
    conn = sqlite3.connect(request.app.state.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _enrich_one_bill(app: FastAPI, bill_id: str) -> None:
    """Background-task body: scrape → load_one → reindex_one for one bill.

    Runs synchronously inside FastAPI's ``BackgroundTasks`` threadpool;
    network calls happen on a worker thread so the event loop is not
    blocked. Any exception is captured on
    ``bills.last_enrichment_error``; the in-flight set is always cleared
    in the ``finally`` block so a crash mid-job doesn't strand the
    button in the in-flight state.
    """
    from concord.api import Client  # noqa: PLC0415 — defer heavy import to first enrichment click
    from concord.pipeline import index_bills, load_bills  # noqa: PLC0415
    from concord.scraper.bills import scrape_enrichment  # noqa: PLC0415
    from concord.storage.sqlite import SqliteStorage  # noqa: PLC0415

    db_path: Path = app.state.db_path
    storage_dir: Path = app.state.storage_dir

    storage = SqliteStorage(db_path, load_vec=False)
    try:
        storage.clear_bill_enrichment_error(bill_id)
    finally:
        storage.close()

    try:
        congress_s, bill_type, bill_number_s = bill_id.split("-", 2)
        key = (int(congress_s), bill_type, int(bill_number_s))
        with Client() as client:
            scrape_enrichment(
                client=client,
                bill_keys=[key],
                storage_dir=storage_dir,
                fetched_at=datetime.now(UTC),
            )
        load_bills.load_one(storage_dir=storage_dir, db_path=db_path, bill_id=bill_id)
        index_bills.reindex_one(db_path=db_path, bill_id=bill_id)
    except Exception as exc:
        _log.warning("enrichment failed for %s: %s", bill_id, exc)
        storage = SqliteStorage(db_path, load_vec=False)
        try:
            storage.set_bill_enrichment_error(bill_id, str(exc)[:500])
        finally:
            storage.close()
    finally:
        app.state.enrichment_in_flight.discard(bill_id)


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
