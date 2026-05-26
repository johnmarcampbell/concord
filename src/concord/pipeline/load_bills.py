"""Stage 1 — Bills loader.

Projects the ADR 0006 snapshot stream in ``<storage_dir>/bills.jsonl``
into the ``bills`` SQLite table. The natural key is the composite
``(congress, bill_type, bill_number)``; the loader keeps the latest
snapshot per key and UPSERTs one row each.

Re-running the loader over an unchanged JSONL is a no-op. Re-running
over a JSONL that gained new snapshots converges the SQL projection to
the latest-snapshot view.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

from ..models import parse_bill
from ..scraper.bills import BILLS_JSONL_NAME
from ..storage.sqlite import SqliteStorage

_log = logging.getLogger("concord.pipeline.load_bills")


class LoadStats(NamedTuple):
    """Outcome of one :func:`load` invocation."""

    bills_written: int
    snapshots_read: int
    malformed: int


def load(
    *,
    storage_dir: Path,
    db_path: Path,
    limit: int | None = None,
) -> LoadStats:
    """Project the latest Bill snapshot per key into SQLite.

    Reads ``<storage_dir>/bills.jsonl``. If the file is missing, returns
    a zeroed :class:`LoadStats` — matching the no-op-on-missing
    convention set by :mod:`concord.pipeline.load_members`. Stops after
    ``limit`` bills have been UPSERTed when set.
    """
    jsonl_path = storage_dir / BILLS_JSONL_NAME
    if not jsonl_path.exists():
        return LoadStats(bills_written=0, snapshots_read=0, malformed=0)

    latest_per_key: dict[tuple[int, str, int], tuple[datetime, dict[str, Any]]] = {}
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
                congress = int(key["congress"])
                bill_type = str(key["bill_type"]).lower()
                bill_number = int(key["bill_number"])
                fetched_at = datetime.fromisoformat(envelope["fetched_at"])
                payload = envelope["payload"]
            except (KeyError, TypeError, ValueError) as exc:
                malformed += 1
                _log.warning("skipping line %d with bad envelope: %s", line_no, exc)
                continue

            cell = (congress, bill_type, bill_number)
            current = latest_per_key.get(cell)
            if current is None or fetched_at > current[0]:
                latest_per_key[cell] = (fetched_at, payload)

    bills_written = 0
    storage = SqliteStorage(db_path, load_vec=False)
    try:
        for (_c, _t, _n), (fetched_at, payload) in latest_per_key.items():
            try:
                bill = parse_bill(payload)
            except Exception as exc:
                malformed += 1
                _log.warning("skipping bill after parse failure: %s", exc)
                continue
            storage.upsert_bill(bill, fetched_at=fetched_at.isoformat())
            bills_written += 1
            if limit is not None and bills_written >= limit:
                break
    finally:
        storage.close()

    return LoadStats(
        bills_written=bills_written,
        snapshots_read=snapshots_read,
        malformed=malformed,
    )


__all__ = ["LoadStats", "load"]
