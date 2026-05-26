"""Stage 0 — Bills scraper.

Walks ``api.congress.gov``'s seven Bill endpoints to build the canonical
identity record per Bill plus the five tier-2 enrichment streams:

1. ``/v3/bill/{congress}/{bill_type}`` — list endpoint. Stubs only;
   used solely to discover Bill numbers. Not persisted.
2. ``/v3/bill/{c}/{t}/{n}`` — detail endpoint. One ADR 0006 snapshot
   envelope per response is appended to ``data/bills.jsonl``.
3. ``/v3/bill/{c}/{t}/{n}/{section}`` for each of cosponsors, actions,
   subjects, titles, summaries — one envelope per (bill, section) is
   appended to the corresponding ``data/bill_<section>.jsonl`` file
   per ADR 0009.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

from concord.api import Client

_log = logging.getLogger("concord.scraper.bills")

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

#: Tier-2 sub-endpoint names. Each maps to its own ``bill_<section>.jsonl``
#: under the storage directory per ADR 0009.
BILL_ENRICHMENT_SECTIONS: tuple[str, ...] = (
    "cosponsors",
    "actions",
    "subjects",
    "titles",
    "summaries",
)


def enrichment_jsonl_name(section: str) -> str:
    """Return the canonical JSONL filename for one tier-2 section."""
    return f"bill_{section}.jsonl"


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


class EnrichProgressEvent(NamedTuple):
    """Emitted by :func:`scrape_enrichment` once per Bill.

    ``sections_written`` is the count of sub-endpoints that wrote a
    snapshot successfully; ``partial_failures`` records the sections
    that raised. A run-level catastrophic failure surfaces as an
    exception, not a progress event.
    """

    bill_key: tuple[int, str, int]
    sections_written: int
    partial_failures: tuple[str, ...]


class EnrichStats(NamedTuple):
    """Outcome of one :func:`scrape_enrichment` invocation."""

    bills_enriched: int
    snapshots_written: int
    section_failures: int


_ENRICHMENT_FETCHERS: dict[str, str] = {
    "cosponsors": "get_bill_cosponsors",
    "actions": "get_bill_actions",
    "subjects": "get_bill_subjects",
    "titles": "get_bill_titles",
    "summaries": "get_bill_summaries",
}


def scrape_enrichment(
    *,
    client: Client,
    bill_keys: Iterable[tuple[int, str, int]],
    storage_dir: Path,
    fetched_at: datetime,
    sections: Iterable[str] | None = None,
    limit: int | None = None,
    progress: Callable[[EnrichProgressEvent], None] | None = None,
) -> EnrichStats:
    """Append one ADR 0006 envelope per (bill, section) to ``data/bill_<section>.jsonl``.

    For each bill in ``bill_keys``, fetch each requested ``section`` from
    its dedicated sub-endpoint and write one envelope per (bill, section)
    pair. Per ADR 0009, each section has its own JSONL file under
    ``storage_dir``; failures in one section don't prevent the others
    from being written.

    The output files are opened in append mode; the storage directory is
    created if missing. Re-running over the same bills appends new
    snapshots — the loader projects the latest per (bill_id, section)
    into SQLite.
    """
    storage_dir.mkdir(parents=True, exist_ok=True)
    requested_sections = tuple(sections) if sections is not None else BILL_ENRICHMENT_SECTIONS
    for s in requested_sections:
        if s not in _ENRICHMENT_FETCHERS:
            raise ValueError(
                f"unknown enrichment section {s!r}; "
                f"expected one of {', '.join(BILL_ENRICHMENT_SECTIONS)}"
            )

    iso = fetched_at.isoformat()
    # Open every requested section's file once; the per-bill loop writes
    # to whichever it just fetched.
    handles: dict[str, Any] = {}
    try:
        for section in requested_sections:
            handles[section] = (storage_dir / enrichment_jsonl_name(section)).open(
                "a", encoding="utf-8"
            )

        bills_enriched = 0
        snapshots_written = 0
        section_failures = 0
        for bill_key in bill_keys:
            congress, bill_type, bill_number = bill_key
            bt = bill_type.lower()
            sections_written = 0
            partial: list[str] = []
            for section in requested_sections:
                method = getattr(client, _ENRICHMENT_FETCHERS[section])
                try:
                    payload = method(congress, bt, bill_number)
                except Exception as exc:
                    _log.warning(
                        "enrichment fetch failed for %s/%s/%s/%s: %s",
                        congress,
                        bt,
                        bill_number,
                        section,
                        exc,
                    )
                    partial.append(section)
                    section_failures += 1
                    continue
                envelope = {
                    "fetched_at": iso,
                    "key": {
                        "congress": congress,
                        "bill_type": bt,
                        "bill_number": bill_number,
                    },
                    "payload": payload,
                }
                handles[section].write(json.dumps(envelope, ensure_ascii=False))
                handles[section].write("\n")
                snapshots_written += 1
                sections_written += 1
            bills_enriched += 1
            if progress is not None:
                progress(
                    EnrichProgressEvent(
                        bill_key=(congress, bt, bill_number),
                        sections_written=sections_written,
                        partial_failures=tuple(partial),
                    )
                )
            if limit is not None and bills_enriched >= limit:
                break
    finally:
        for fh in handles.values():
            fh.close()

    return EnrichStats(
        bills_enriched=bills_enriched,
        snapshots_written=snapshots_written,
        section_failures=section_failures,
    )


__all__ = [
    "BILLS_JSONL_NAME",
    "BILL_ENRICHMENT_SECTIONS",
    "DEFAULT_BILL_TYPES",
    "EnrichProgressEvent",
    "EnrichStats",
    "ScrapeProgressEvent",
    "ScrapeStats",
    "enrichment_jsonl_name",
    "scrape_basic",
    "scrape_enrichment",
]
