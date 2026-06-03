"""FastAPI app for the public-facing search demo — the assembly root.

One process. Reads from the SQLite store at ``app.state.db_path``,
embeds queries via the ``Embedder`` stashed on ``app.state.embedder``.
Per-request, the route modules open a fresh ``sqlite3.Connection`` (with
the ``sqlite-vec`` extension loaded via :func:`concord.web._deps.get_db`)
so WAL-mode visibility of new writes from the pipeline is automatic.

This module is deliberately thin: :func:`create_app` bootstraps the
schema (ADR 0012), constructs the OpenAI-backed ``Embedder``/``Briefer``,
sets up ``app.state``, and then calls a ``register_*`` function per
concern. The routes themselves live in sibling modules:

- ``routes_search``      — landing page (``/``) + federated ``/search``
- ``routes_bills``       — ``/bills`` index + Bill profile
- ``routes_members``     — ``/members`` index + Member profile
- ``routes_votes``       — ``/votes`` index + Vote profile
- ``routes_proceedings`` — Proceeding document view
- ``enrichment``         — web-initiated Stage 0 enrichment (ADR 0016)
- ``brief``              — Bill Brief generation (ADR 0020)
- ``filters``            — Jinja filters (``humanize_age``, ``ordinal``)

Rate limiting (slowapi, in-process per-IP) is applied to ``/search``
only — the other routes don't call OpenAI and don't need protection.
"""

import asyncio
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from concord.brief import Briefer
from concord.embedding import Embedder
from concord.storage.sqlite import ensure_schema
from concord.web import (
    routes_bills,
    routes_members,
    routes_proceedings,
    routes_search,
    routes_votes,
)
from concord.web.brief import register_brief_routes
from concord.web.enrichment import _read_enrichment_flag, register_enrichment_routes
from concord.web.filters import register_filters

_HERE = Path(__file__).parent
_TEMPLATES_DIR = _HERE / "templates"
_STATIC_DIR = _HERE / "static"


def create_app(
    db_path: Path | str,
    *,
    embedder: Embedder | None = None,
    briefer: Briefer | None = None,
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
    briefer:
        Optional injected :class:`Briefer` for the Bill Brief feature
        (ADR 0020). When ``embedder`` is also ``None`` (production
        default), constructed from the *same* ``openai.OpenAI()`` client.
        When ``None`` (the case for tests that only inject an embedder),
        the brief feature is disabled and its routes are not registered.
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

        # One OpenAI client backs both query embeddings (ADR 0004) and
        # Bill Brief generation (ADR 0020). serve already requires the key.
        client = openai.OpenAI()
        embedder = Embedder(client)
        if briefer is None:
            briefer = Briefer(client)

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
    app.state.briefer = briefer
    # Bill Brief feature is on whenever a Briefer is present (ADR 0020);
    # in production that is always, since serve builds one from the same
    # OpenAI client the embedder uses.
    app.state.brief_enabled = briefer is not None
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
    register_filters(templates)
    app.state.templates = templates

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    _register_meta_routes(app)
    routes_search.register(app, limiter)
    routes_bills.register(app)
    routes_members.register(app)
    routes_votes.register(app)
    routes_proceedings.register(app)
    if enrichment_enabled:
        register_enrichment_routes(app)
    if briefer is not None:
        register_brief_routes(app)
    return app


def _register_meta_routes(app: FastAPI) -> None:
    """Register the entity-agnostic routes: about page and health check."""
    templates: Jinja2Templates = app.state.templates

    @app.get("/about/methodology", response_class=HTMLResponse)
    def about_methodology(request: Request) -> Response:
        return templates.TemplateResponse(request, "about/methodology.html", {})

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"ok": True})


__all__ = ["create_app"]
