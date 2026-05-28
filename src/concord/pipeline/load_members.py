"""Stage 1 — Members loader.

Projects the ADR 0006 snapshot stream in ``data/members.jsonl`` into the
``members`` and ``member_terms`` SQLite tables.

The natural key for a Member snapshot is the composite
``(bioguide_id, congress)``: the ``/v3/member/congress/{n}`` list endpoint
returns the **same** payload for every Congress a Member served in, so
the queried Congress is the only thing that distinguishes consecutive
snapshots. The scraper stores that Congress in ``key["congress"]``; the
loader uses it to project one Term row per (member, congress) the API
listed them in.

Contract:

* Read every snapshot. Group by ``(key["bioguide_id"], key["congress"])``.
* For each group, keep the snapshot with the latest ``fetched_at`` — one
  authoritative payload per (member, congress) cell.
* For each kept snapshot, project a single Term for the queried Congress.
* For each Member, UPSERT the identity row (from the latest snapshot
  overall for that bioguide_id) and DELETE-then-INSERT every Term that
  any snapshot produced for them.

Re-running the loader over an unchanged JSONL is a no-op (idempotent).
Re-running over a JSONL that gained new snapshots converges the SQL
state to the latest-snapshot projection.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

from concord.models import Term, parse_member_identity, parse_member_term
from concord.storage.sqlite import SqliteStorage

_log = logging.getLogger("concord.pipeline.load_members")


class LoadStats(NamedTuple):
    """Outcome of one :func:`load` invocation."""

    members_written: int
    terms_written: int
    snapshots_read: int
    malformed: int


def load(*, jsonl_path: Path, db_path: Path) -> LoadStats:  # noqa: C901, PLR0915 — pipeline orchestrator
    """Load the JSONL snapshot stream into SQLite.

    Returns the count of Members and Terms written plus diagnostics.
    """
    # Latest snapshot per (bioguide_id, congress) — the natural key.
    latest_per_cell: dict[tuple[str, int], tuple[datetime, dict[str, Any]]] = {}
    # Latest snapshot per bioguide_id, ignoring congress — for the
    # Member identity row, which doesn't vary by Congress.
    latest_per_member: dict[str, tuple[datetime, dict[str, Any]]] = {}

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
                key = envelope["key"]
                bioguide_id = key["bioguide_id"]
                congress = int(key["congress"])
                fetched_at = datetime.fromisoformat(envelope["fetched_at"])
                payload = envelope["payload"]
            except (KeyError, TypeError, ValueError) as exc:
                # Envelopes from before the composite-key migration lack
                # ``congress`` and aren't loadable here — they need a
                # re-scrape to restore the queried-Congress signal.
                malformed += 1
                _log.warning("skipping line %d with bad envelope: %s", line_no, exc)
                continue

            cell = (bioguide_id, congress)
            current_cell = latest_per_cell.get(cell)
            if current_cell is None or fetched_at > current_cell[0]:
                latest_per_cell[cell] = (fetched_at, payload)

            current_member = latest_per_member.get(bioguide_id)
            if current_member is None or fetched_at > current_member[0]:
                latest_per_member[bioguide_id] = (fetched_at, payload)

    # Project each (bioguide_id, congress) snapshot into one Term row.
    terms_by_member: dict[str, list[Term]] = {}
    for (bioguide_id, congress), (_, payload) in latest_per_cell.items():
        try:
            term = parse_member_term(payload, congress=congress)
        except Exception as exc:  # pragma: no cover - defensive parse guard
            malformed += 1
            _log.warning("skipping term %s/%d after parse failure: %s", bioguide_id, congress, exc)
            continue
        if term is None:
            # The payload didn't include a terms.item covering ``congress``
            # — the API contradicted its own listing. Log + skip.
            _log.warning(
                "no terms.item covers Congress %d in payload for %s; skipping that Term row",
                congress,
                bioguide_id,
            )
            continue
        terms_by_member.setdefault(bioguide_id, []).append(term)

    members_written = 0
    terms_written = 0

    storage = SqliteStorage(db_path, load_vec=False)
    try:
        for bioguide_id, (fetched_at, payload) in latest_per_member.items():
            try:
                member = parse_member_identity(payload)
            except Exception as exc:  # pragma: no cover - defensive parse guard
                malformed += 1
                _log.warning("skipping %s after parse failure: %s", bioguide_id, exc)
                continue
            terms = terms_by_member.get(bioguide_id, [])
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
