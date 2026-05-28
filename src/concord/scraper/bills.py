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

import json
import logging
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

from concord.api import Client
from concord.scraper._common import (
    is_stub_unchanged,
    load_freshness_map,
    parse_signal_timestamp,
)

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


def _parse_bill_number(stub: dict[str, Any]) -> int | None:
    """Return the bill number from a list-endpoint stub, or ``None`` if absent/invalid."""
    raw = stub.get("number")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


class ScrapeProgressEvent(NamedTuple):
    """Emitted by :func:`scrape_basic` once per ``(congress, bill_type)``."""

    congress: int
    bill_type: str
    bills_seen: int
    bills_written: int
    is_pair_done: bool = False
    category_total: int | None = None


class ScrapeStats(NamedTuple):
    """Outcome of one :func:`scrape_basic` invocation."""

    bills_written: int
    bills_seen: int
    bills_skipped: int = 0


def _bill_stub_signal(stub: dict[str, Any]) -> datetime | None:
    """Return the max of ``updateDate`` / ``updateDateIncludingText`` on a stub."""
    candidates = (
        parse_signal_timestamp(stub.get("updateDate")),
        parse_signal_timestamp(stub.get("updateDateIncludingText")),
    )
    parsed = [c for c in candidates if c is not None]
    return max(parsed) if parsed else None


class _BasicPairResult(NamedTuple):
    seen: int
    written: int
    skipped: int
    done: bool


def _scrape_basic_pair(  # noqa: PLR0913 — per-pair worker, all kwargs are state forwarded from scrape_basic
    *,
    client: Client,
    congress: int,
    bt: str,
    fh: Any,
    iso: str,
    remaining: float,
    skip_unchanged: bool,
    freshness: dict[tuple[Any, ...], datetime],
    progress: Callable[[ScrapeProgressEvent], None] | None,
) -> _BasicPairResult:
    """Walk one ``(congress, bill_type)`` slot; write detail envelopes."""
    pair_seen = 0
    pair_written = 0
    pair_skipped = 0
    pair_total: list[int | None] = [None]
    done = False
    for stub in client.list_bills(congress, bt, on_total=lambda t: pair_total.__setitem__(0, t)):
        pair_seen += 1
        bill_number = _parse_bill_number(stub)
        if bill_number is None:
            continue
        if skip_unchanged and is_stub_unchanged(
            freshness=freshness,
            key=(congress, bt, bill_number),
            signal=_bill_stub_signal(stub),
        ):
            pair_skipped += 1
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
        if progress is not None:
            progress(
                ScrapeProgressEvent(
                    congress=congress,
                    bill_type=bt,
                    bills_seen=pair_seen,
                    bills_written=pair_written,
                    category_total=pair_total[0],
                )
            )
        if pair_written >= remaining:
            done = True
            break
    if progress is not None:
        progress(
            ScrapeProgressEvent(
                congress=congress,
                bill_type=bt,
                bills_seen=pair_seen,
                bills_written=pair_written,
                is_pair_done=True,
                category_total=pair_total[0],
            )
        )
    return _BasicPairResult(seen=pair_seen, written=pair_written, skipped=pair_skipped, done=done)


def scrape_basic(
    *,
    client: Client,
    congresses: Iterable[int],
    storage_dir: Path,
    fetched_at: datetime,
    bill_types: Iterable[str] | None = None,
    limit: int | None = None,
    progress: Callable[[ScrapeProgressEvent], None] | None = None,
    skip_unchanged: bool = False,
) -> ScrapeStats:
    """Append one detail snapshot per Bill to ``<storage_dir>/bills.jsonl``.

    Behavior:

    * For each ``(congress, bill_type)`` pair, paginate the list endpoint
      to discover Bill numbers (stubs are not persisted).
    * For each stub, fetch the detail endpoint and append one ADR 0006
      envelope keyed by ``(congress, bill_type, bill_number)``.
    * If ``limit`` is set, stop once ``limit`` detail envelopes have been
      written across all pairs.

    When ``skip_unchanged`` is set, stubs whose ``updateDate`` /
    ``updateDateIncludingText`` is not newer than the latest snapshot in
    ``bills.jsonl`` are skipped (no detail fetch, no JSONL write). See
    ADR 0015.

    The output file is opened in append mode; the storage directory is
    created if missing.
    """
    storage_dir.mkdir(parents=True, exist_ok=True)
    out_path = storage_dir / BILLS_JSONL_NAME
    iso = fetched_at.isoformat()

    types = tuple(bill_types) if bill_types is not None else DEFAULT_BILL_TYPES
    _limit: float = float("inf") if limit is None else limit

    freshness = (
        load_freshness_map(out_path, ("congress", "bill_type", "bill_number"))
        if skip_unchanged
        else {}
    )

    written = 0
    seen = 0
    skipped = 0
    done = False
    with out_path.open("a", encoding="utf-8") as fh:
        for congress in congresses:
            if done:
                break
            for bill_type in types:
                if done:
                    break
                remaining = _limit - written
                pair = _scrape_basic_pair(
                    client=client,
                    congress=congress,
                    bt=bill_type.lower(),
                    fh=fh,
                    iso=iso,
                    remaining=remaining,
                    skip_unchanged=skip_unchanged,
                    freshness=freshness,
                    progress=progress,
                )
                seen += pair.seen
                written += pair.written
                skipped += pair.skipped
                if pair.done:
                    done = True

    return ScrapeStats(bills_written=written, bills_seen=seen, bills_skipped=skipped)


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
    bills_done: int = 0
    bills_total: int | None = None


class EnrichStats(NamedTuple):
    """Outcome of one :func:`scrape_enrichment` invocation."""

    bills_enriched: int
    snapshots_written: int
    section_failures: int
    sections_skipped: int = 0


_ENRICHMENT_FETCHERS: dict[str, str] = {
    "cosponsors": "get_bill_cosponsors",
    "actions": "get_bill_actions",
    "subjects": "get_bill_subjects",
    "titles": "get_bill_titles",
    "summaries": "get_bill_summaries",
}


class _BillEnrichResult(NamedTuple):
    written: int
    failures: int
    skipped: int
    partial: tuple[str, ...]


def _enrich_one_bill(
    *,
    client: Client,
    bill_key: tuple[int, str, int],
    requested_sections: tuple[str, ...],
    handles: dict[str, Any],
    iso: str,
    skip_unchanged: bool,
    section_freshness: dict[str, dict[tuple[Any, ...], datetime]],
    signal: datetime | None,
) -> _BillEnrichResult:
    """Fetch enrichment sections for one bill; write per-section envelopes."""
    congress, bill_type, bill_number = bill_key
    bt = bill_type.lower()
    normalized_key = (congress, bt, bill_number)
    written = 0
    failures = 0
    skipped = 0
    partial: list[str] = []
    for section in requested_sections:
        if skip_unchanged and is_stub_unchanged(
            freshness=section_freshness[section],
            key=normalized_key,
            signal=signal,
        ):
            skipped += 1
            continue
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
            failures += 1
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
        written += 1
    return _BillEnrichResult(
        written=written, failures=failures, skipped=skipped, partial=tuple(partial)
    )


def scrape_enrichment(  # noqa: PLR0913 — one kwarg per knob; collapsing into an options object hides the call sites
    *,
    client: Client,
    bill_keys: Iterable[tuple[int, str, int]],
    storage_dir: Path,
    fetched_at: datetime,
    sections: Iterable[str] | None = None,
    limit: int | None = None,
    bills_total: int | None = None,
    progress: Callable[[EnrichProgressEvent], None] | None = None,
    skip_unchanged: bool = False,
    bill_signal_lookup: Callable[[tuple[int, str, int]], datetime | None] | None = None,
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
    # Per-section freshness maps gate which sub-endpoint fetches we
    # skip when ``skip_unchanged`` is set (ADR 0015). Each section can
    # refresh independently — see ADR 0009.
    section_freshness: dict[str, dict[tuple[Any, ...], datetime]] = {}
    if skip_unchanged:
        for section in requested_sections:
            section_freshness[section] = load_freshness_map(
                storage_dir / enrichment_jsonl_name(section),
                ("congress", "bill_type", "bill_number"),
            )

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
        sections_skipped = 0
        for bill_key in bill_keys:
            congress, bill_type, bill_number = bill_key
            bt = bill_type.lower()
            normalized_key = (congress, bt, bill_number)
            signal = (
                bill_signal_lookup(normalized_key)
                if skip_unchanged and bill_signal_lookup is not None
                else None
            )
            result = _enrich_one_bill(
                client=client,
                bill_key=bill_key,
                requested_sections=requested_sections,
                handles=handles,
                iso=iso,
                skip_unchanged=skip_unchanged,
                section_freshness=section_freshness,
                signal=signal,
            )
            snapshots_written += result.written
            section_failures += result.failures
            sections_skipped += result.skipped
            bills_enriched += 1
            if progress is not None:
                progress(
                    EnrichProgressEvent(
                        bill_key=(congress, bt, bill_number),
                        sections_written=result.written,
                        partial_failures=result.partial,
                        bills_done=bills_enriched,
                        bills_total=bills_total,
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
        sections_skipped=sections_skipped,
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
