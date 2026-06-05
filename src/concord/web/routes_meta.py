"""Entity-agnostic routes: the methodology about page and a health check.

The two routes that don't belong to any single entity. Registered by
``create_app`` through the same ``register(app)`` seam every other route
module exposes.
"""

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates


def register(app: FastAPI) -> None:
    """Register the entity-agnostic routes: about page and health check."""
    templates: Jinja2Templates = app.state.templates

    @app.get("/about/methodology", response_class=HTMLResponse)
    def about_methodology(request: Request) -> Response:
        return templates.TemplateResponse(request, "about/methodology.html", {})

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"ok": True})
