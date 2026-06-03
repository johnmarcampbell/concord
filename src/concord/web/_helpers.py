"""Cross-route request/data helpers for the web layer.

Small helpers used by more than one entity route module. They live here,
rather than in ``concord.web.app``, so the route modules can share them
without importing ``app`` (which imports *them* to register routes — the
same cycle ``_deps`` exists to avoid). Single-route helpers stay in their
owning ``routes_*`` module; only genuinely cross-module ones land here.
"""

import sqlite3
from typing import Any

from concord.web import search as search_mod
from concord.web.top_bills import CURATED_TOP_BILLS


def resolve_top_bills(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Resolve CURATED_TOP_BILLS against the local store, in curated order.

    Degrades gracefully: any curated bill missing from the store is skipped.
    Shared by the landing page (``/``) and the bills index (``/bills``).
    """
    keys = [(c.congress, c.bill_type, c.bill_number) for c in CURATED_TOP_BILLS]
    resolved = search_mod.get_curated_bills(db, keys)
    return [
        {"bill": hit, "label": entry.label, "blurb": entry.blurb}
        for entry in CURATED_TOP_BILLS
        if (hit := resolved.get((entry.congress, entry.bill_type, entry.bill_number))) is not None
    ]
