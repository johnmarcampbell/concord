"""Proceeding document view (``/proceedings/{granule_id}``)."""

import sqlite3

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from concord.web import search as search_mod
from concord.web._deps import get_db


def register(app: FastAPI) -> None:
    """Register the proceeding profile route on ``app``."""
    templates: Jinja2Templates = app.state.templates

    @app.get("/proceedings/{granule_id}", response_class=HTMLResponse)
    def proceeding(
        request: Request,
        granule_id: str,
        db: sqlite3.Connection = Depends(get_db),  # noqa: B008 - FastAPI Depends pattern
    ) -> Response:
        row = search_mod.get_proceeding(db, granule_id)
        if row is None:
            return templates.TemplateResponse(
                request,
                "404.html",
                {"granule_id": granule_id},
                status_code=404,
            )
        return templates.TemplateResponse(request, "proceeding.html", {"p": row})
