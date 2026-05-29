"""Shared helper for constructing the ADR 0006 snapshot envelope.

Every entity-test module that exercises a mutable entity (Members from
Phase 1, Bills/Votes/etc. from later phases) uses :func:`wrap_snapshot`
to keep the envelope shape consistent.
"""

from datetime import datetime
from typing import Any


def wrap_snapshot(
    payload: dict[str, Any],
    *,
    fetched_at: datetime,
    key: dict[str, Any],
) -> dict[str, Any]:
    """Build one ADR-0006-shaped snapshot envelope around ``payload``.

    Returns a dict whose keys are exactly ``fetched_at``, ``key``,
    ``payload`` — matching :class:`concord.models.Snapshot` and the
    JSONL row shape the scraper appends.
    """
    return {
        "fetched_at": fetched_at.isoformat(),
        "key": key,
        "payload": payload,
    }


__all__ = ["wrap_snapshot"]
