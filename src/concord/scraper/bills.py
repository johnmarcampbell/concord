"""Stage 0 — Bills scraper.

Walks ``api.congress.gov``'s two Bill endpoints to build the canonical
identity record per Bill:

1. ``/v3/bill/{congress}/{bill_type}`` — list endpoint. Stubs only;
   used solely to discover Bill numbers. Not persisted.
2. ``/v3/bill/{c}/{t}/{n}`` — detail endpoint. One ADR 0006 snapshot
   envelope per response is appended to ``data/bills.jsonl``.

Per ADR 0009 the bills.jsonl file holds only the Tier 1 identity
snapshots — Phase 2b will add five sibling JSONL files for the
sub-endpoints (cosponsors, actions, subjects, titles, summaries) via a
second entry point ``scrape_enrichment`` added to this same module.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from ..api import Client

#: Default Bill type codes scraped when the caller doesn't pass
#: ``bill_types``. Order matches the API documentation; chamber-grouped
#: so a partial run still covers a recognizable slice.
DEFAULT_BILL_TYPES: tuple[str, ...] = (
    "hr",
    "hres",
    "hjres",
    "hconres",
    "s",
    "sres",
    "sjres",
    "sconres",
)

#: Canonical filename inside the storage directory.
BILLS_JSONL_NAME = "bills.jsonl"


class ScrapeProgressEvent(NamedTuple):
    """Emitted by :func:`scrape_basic` once per ``(congress, bill_type)``."""

    congress: int
    bill_type: str
    bills_seen: int
    bills_written: int


class ScrapeStats(NamedTuple):
    """Outcome of one :func:`scrape_basic` invocation."""

    bills_written: int
    bills_seen: int


def scrape_basic(
    *,
    client: Client,
    congresses: Iterable[int],
    storage_dir: Path,
    fetched_at: datetime,
    bill_types: Iterable[str] | None = None,
    limit: int | None = None,
    progress: Callable[[ScrapeProgressEvent], None] | None = None,
) -> ScrapeStats:
    """Append one detail snapshot per Bill to ``<storage_dir>/bills.jsonl``.

    Behavior:

    * For each ``(congress, bill_type)`` pair, paginate the list endpoint
      to discover Bill numbers (stubs are not persisted).
    * For each stub, fetch the detail endpoint and append one ADR 0006
      envelope keyed by ``(congress, bill_type, bill_number)``.
    * If ``limit`` is set, stop once ``limit`` detail envelopes have been
      written across all pairs.

    The output file is opened in append mode; the storage directory is
    created if missing.
    """
    storage_dir.mkdir(parents=True, exist_ok=True)
    out_path = storage_dir / BILLS_JSONL_NAME
    iso = fetched_at.isoformat()

    types = tuple(bill_types) if bill_types is not None else DEFAULT_BILL_TYPES

    written = 0
    seen = 0
    done = False
    with out_path.open("a", encoding="utf-8") as fh:
        for congress in congresses:
            if done:
                break
            for bill_type in types:
                if done:
                    break
                bt = bill_type.lower()
                pair_seen = 0
                pair_written = 0
                for stub in client.list_bills(congress, bt):
                    pair_seen += 1
                    seen += 1
                    number_raw = stub.get("number")
                    if number_raw is None:
                        # Defensive: a stub without a number can't be
                        # promoted to a detail call; skip it.
                        continue
                    try:
                        bill_number = int(number_raw)
                    except (TypeError, ValueError):
                        continue
                    detail = client.get_bill_detail(congress, bt, bill_number)
                    envelope = {
                        "fetched_at": iso,
                        "key": {
                            "congress": congress,
                            "bill_type": bt,
                            "bill_number": bill_number,
                        },
                        "payload": detail,
                    }
                    fh.write(json.dumps(envelope, ensure_ascii=False))
                    fh.write("\n")
                    pair_written += 1
                    written += 1
                    if limit is not None and written >= limit:
                        done = True
                        break
                if progress is not None:
                    progress(
                        ScrapeProgressEvent(
                            congress=congress,
                            bill_type=bt,
                            bills_seen=pair_seen,
                            bills_written=pair_written,
                        )
                    )

    return ScrapeStats(bills_written=written, bills_seen=seen)


__all__ = [
    "BILLS_JSONL_NAME",
    "DEFAULT_BILL_TYPES",
    "ScrapeProgressEvent",
    "ScrapeStats",
    "scrape_basic",
]
