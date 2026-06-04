"""Shared FastAPI dependencies and constants for the web layer.

Small primitives shared across the route-registration modules
(``concord.web.routes_*``, ``concord.web.enrichment``, ``concord.web.brief``):
the bill-type allow-list (:data:`VALID_BILL_TYPES`) and two per-request SQLite
connection dependencies — :func:`get_db` (plain, for indexed SELECTs) and
:func:`get_vector_db` (with the ``sqlite-vec`` extension loaded, for hybrid
search). They live here, rather than in ``app``, so the route modules can
share them without an ``app``↔``routes`` import cycle: ``app`` imports the
route modules to register them, so they cannot import back.
"""

import sqlite3
from collections.abc import Iterator

import sqlite_vec  # type: ignore[import-untyped]
from fastapi import Request

#: Bill type codes accepted in URL paths. Mirrors
#: :data:`concord.cli.DEFAULT_BILL_TYPES`; kept here too so the web
#: package doesn't import the CLI module.
VALID_BILL_TYPES = frozenset({"hr", "hres", "hjres", "hconres", "s", "sres", "sjres", "sconres"})


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """Per-request read-only SQLite connection (no sqlite-vec extension).

    For route handlers whose queries are plain indexed SELECTs and don't
    need the vector index. The sqlite-vec-loaded equivalent is
    :func:`get_vector_db`.
    """
    conn = sqlite3.connect(request.app.state.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def get_vector_db(request: Request) -> Iterator[sqlite3.Connection]:
    """Per-request connection with sqlite-vec loaded.

    Used by routes whose queries touch the vector index (hybrid search).
    Plain-SELECT routes use the lighter :func:`get_db` instead.
    """
    conn = sqlite3.connect(request.app.state.db_path)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    try:
        yield conn
    finally:
        conn.close()
