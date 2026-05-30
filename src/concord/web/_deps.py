"""Shared FastAPI dependencies and constants for the web layer.

Small primitives used by more than one route-registration module
(``concord.web.app`` and ``concord.web.brief``): the bill-type allow-list
and a per-request read-only SQLite connection. They live here, rather than
in ``app``, so the brief routes can share them without an ``app``↔``brief``
import cycle.
"""

import sqlite3
from collections.abc import Iterator

from fastapi import Request

#: Bill type codes accepted in URL paths. Mirrors
#: :data:`concord.cli.DEFAULT_BILL_TYPES`; kept here too so the web
#: package doesn't import the CLI module.
VALID_BILL_TYPES = frozenset({"hr", "hres", "hjres", "hconres", "s", "sres", "sjres", "sconres"})


def db_connection(request: Request) -> Iterator[sqlite3.Connection]:
    """Per-request read-only SQLite connection (no sqlite-vec extension).

    For route handlers whose queries are plain indexed SELECTs and don't
    need the vector index. The bulk-search ``get_db`` closure in
    ``concord.web.app`` is the sqlite-vec-loaded equivalent.
    """
    conn = sqlite3.connect(request.app.state.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
