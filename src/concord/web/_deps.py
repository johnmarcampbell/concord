"""Shared FastAPI dependencies and constants for the web layer.

Small primitives used by more than one route-registration module
(``concord.web.app`` and ``concord.web.brief``): the bill-type allow-list
and a per-request read-only SQLite connection. They live here, rather than
in ``app``, so the brief routes can share them without an ``app``↔``brief``
import cycle.
"""

import sqlite3
from collections.abc import Iterator

import sqlite_vec  # type: ignore[import-untyped]
from fastapi import Request

#: Bill type codes accepted in URL paths. Mirrors
#: :data:`concord.cli.DEFAULT_BILL_TYPES`; kept here too so the web
#: package doesn't import the CLI module.
VALID_BILL_TYPES = frozenset({"hr", "hres", "hjres", "hconres", "s", "sres", "sjres", "sconres"})


def db_connection(request: Request) -> Iterator[sqlite3.Connection]:
    """Per-request read-only SQLite connection (no sqlite-vec extension).

    For route handlers whose queries are plain indexed SELECTs and don't
    need the vector index. The sqlite-vec-loaded equivalent is
    :func:`get_db`.
    """
    conn = sqlite3.connect(request.app.state.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """Per-request connection with sqlite-vec loaded.

    The dependency the entity route modules share for queries that may
    touch the vector index (hybrid search). Plain-SELECT routes use the
    lighter :func:`db_connection` instead.
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
