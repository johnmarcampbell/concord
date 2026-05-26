"""Stage 1 — Members loader.

Projects the ADR 0006 snapshot stream in ``data/members.jsonl`` into the
``members`` and ``member_terms`` SQLite tables. The contract:

* Read every snapshot. Group by ``key["bioguide_id"]``.
* For each group, keep the snapshot with the latest ``fetched_at``.
* For each kept snapshot, parse the payload into a typed
  :class:`Member` + list of :class:`Term`s.
* UPSERT the Member row on ``bioguide_id`` and DELETE-then-INSERT the
  Term rows so the projection reflects exactly what the latest snapshot
  says (no stale Terms left behind if the API ever drops one).

Re-running the loader over an unchanged JSONL is a no-op (idempotent).
Re-running over a JSONL that gained new snapshots converges the SQL
state to the latest-snapshot projection.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

from ..models import parse_member
from ..storage.sqlite import SqliteStorage

_log = logging.getLogger("concord.pipeline.load_members")


class LoadStats(NamedTuple):
    """Outcome of one :func:`load` invocation."""

    members_written: int
    terms_written: int
    snapshots_read: int
    malformed: int


def load(*, jsonl_path: Path, db_path: Path) -> LoadStats:
    """Load the JSONL snapshot stream into SQLite.

    Returns the count of Members and Terms written plus diagnostics.
    """
    latest: dict[str, tuple[datetime, dict[str, Any]]] = {}
    snapshots_read = 0
    malformed = 0

    with jsonl_path.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            snapshots_read += 1
            try:
                envelope = json.loads(line)
            except json.JSONDecodeError as exc:
                malformed += 1
                _log.warning("skipping malformed line %d: %s", line_no, exc)
                continue
            try:
                bioguide_id = envelope["key"]["bioguide_id"]
                fetched_at_raw = envelope["fetched_at"]
                payload = envelope["payload"]
                fetched_at = datetime.fromisoformat(fetched_at_raw)
            except (KeyError, TypeError, ValueError) as exc:
                malformed += 1
                _log.warning("skipping line %d with bad envelope: %s", line_no, exc)
                continue
            current = latest.get(bioguide_id)
            if current is None or fetched_at > current[0]:
                latest[bioguide_id] = (fetched_at, payload)

    members_written = 0
    terms_written = 0

    storage = SqliteStorage(db_path, load_vec=False)
    try:
        for bioguide_id, (fetched_at, payload) in latest.items():
            try:
                member, terms = parse_member(payload)
            except Exception as exc:  # pragma: no cover - defensive parse guard
                malformed += 1
                _log.warning("skipping %s after parse failure: %s", bioguide_id, exc)
                continue
            storage.upsert_member(member, terms, fetched_at=fetched_at.isoformat())
            members_written += 1
            terms_written += len(terms)
    finally:
        storage.close()

    return LoadStats(
        members_written=members_written,
        terms_written=terms_written,
        snapshots_read=snapshots_read,
        malformed=malformed,
    )


__all__ = ["LoadStats", "load"]
