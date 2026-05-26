"""Stage 0 — Members scraper.

Walks ``api.congress.gov``'s ``/v3/member/congress/{congress}`` endpoint
for each requested Congress and appends one ADR 0006 snapshot envelope
to ``data/members.jsonl`` per Member returned. The Stage 1 loader is
responsible for deduplicating by Bioguide ID — a Member who served in
several Congresses will appear in each Congress's listing and produce
one snapshot per appearance.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from ..api import Client


class ScrapeProgressEvent(NamedTuple):
    """Emitted by :func:`scrape` once per Congress, after its pagination
    completes."""

    congress: int
    written_in_congress: int
    total_written: int


def scrape(
    *,
    client: Client,
    congresses: Iterable[int],
    storage_path: Path,
    fetched_at: datetime,
    progress: Callable[[ScrapeProgressEvent], None] | None = None,
) -> int:
    """Append one snapshot envelope per Member to ``storage_path``.

    Returns the total number of snapshots written. The output file is
    opened in append mode (created if missing); the parent directory is
    created if needed.
    """
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    iso = fetched_at.isoformat()
    with storage_path.open("a", encoding="utf-8") as fh:
        for congress in congresses:
            written_in_congress = 0
            for payload in client.list_members(congress):
                bioguide_id = payload.get("bioguideId")
                if not bioguide_id:
                    # Defensive: a Member without a bioguide_id can't be
                    # keyed; skip rather than write an un-loadable line.
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
                total += 1
            if progress is not None:
                progress(
                    ScrapeProgressEvent(
                        congress=congress,
                        written_in_congress=written_in_congress,
                        total_written=total,
                    )
                )
    return total


__all__ = ["ScrapeProgressEvent", "scrape"]
