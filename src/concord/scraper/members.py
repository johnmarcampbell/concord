"""Stage 0 — Members scraper.

Walks ``api.congress.gov``'s ``/v3/member/congress/{congress}`` endpoint
for each requested Congress and appends one ADR 0006 snapshot envelope
to ``data/members.jsonl`` per Member returned. The Stage 1 loader is
responsible for deduplicating by Bioguide ID — a Member who served in
several Congresses will appear in each Congress's listing and produce
one snapshot per appearance.
"""

import json
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from concord.api import Client
from concord.scraper._common import (
    is_stub_unchanged,
    load_freshness_map,
    parse_signal_timestamp,
)


class ScrapeProgressEvent(NamedTuple):
    """Emitted by :func:`scrape` once per Congress, after its pagination
    completes."""

    congress: int
    written_in_congress: int
    total_written: int
    is_congress_done: bool = False
    category_total: int | None = None


class ScrapeStats(NamedTuple):
    """Outcome of one :func:`scrape` invocation."""

    members_written: int
    members_skipped: int = 0


def scrape(
    *,
    client: Client,
    congresses: Iterable[int],
    storage_path: Path,
    fetched_at: datetime,
    progress: Callable[[ScrapeProgressEvent], None] | None = None,
    skip_unchanged: bool = False,
) -> ScrapeStats:
    """Append one snapshot envelope per Member to ``storage_path``.

    Returns ``ScrapeStats`` (``members_written``, ``members_skipped``).
    The output file is opened in append mode (created if missing); the
    parent directory is created if needed.

    When ``skip_unchanged`` is set, members whose ``updateDate`` is not
    newer than the latest snapshot's ``fetched_at`` for the same
    ``(bioguide_id, congress)`` key are skipped (no JSONL write). Note
    that Members are single-fetch — the list endpoint returns the full
    payload — so the saving here is the JSONL write + downstream parse
    cost, not an HTTP call. See ADR 0015.
    """
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    iso = fetched_at.isoformat()
    freshness = (
        load_freshness_map(storage_path, ("bioguide_id", "congress")) if skip_unchanged else {}
    )
    total_written = 0
    total_skipped = 0
    with storage_path.open("a", encoding="utf-8") as fh:
        for congress in congresses:
            written_in_congress = 0
            congress_total: int | None = None

            def _capture_total(t: int) -> None:
                nonlocal congress_total
                congress_total = t

            for payload in client.list_members(congress, on_total=_capture_total):
                bioguide_id = payload.get("bioguideId")
                if not bioguide_id:
                    # Defensive: a Member without a bioguide_id can't be
                    # keyed; skip rather than write an un-loadable line.
                    continue
                if skip_unchanged and is_stub_unchanged(
                    freshness=freshness,
                    key=(bioguide_id, congress),
                    signal=parse_signal_timestamp(payload.get("updateDate")),
                ):
                    total_skipped += 1
                    continue
                envelope = {
                    "fetched_at": iso,
                    # Composite key: the same Member appears in the listing
                    # for every Congress they served in, and the payload is
                    # identical across those queries. Without ``congress``
                    # in the key we'd lose track of which Congress this
                    # snapshot represents and the loader would collapse
                    # multi-Congress careers into a single Term row.
                    "key": {"bioguide_id": bioguide_id, "congress": congress},
                    "payload": payload,
                }
                fh.write(json.dumps(envelope, ensure_ascii=False))
                fh.write("\n")
                written_in_congress += 1
                total_written += 1
                if progress is not None:
                    progress(
                        ScrapeProgressEvent(
                            congress=congress,
                            written_in_congress=written_in_congress,
                            total_written=total_written,
                            category_total=congress_total,
                        )
                    )
            if progress is not None:
                progress(
                    ScrapeProgressEvent(
                        congress=congress,
                        written_in_congress=written_in_congress,
                        total_written=total_written,
                        is_congress_done=True,
                        category_total=congress_total,
                    )
                )
    return ScrapeStats(members_written=total_written, members_skipped=total_skipped)


__all__ = ["ScrapeProgressEvent", "ScrapeStats", "scrape"]
