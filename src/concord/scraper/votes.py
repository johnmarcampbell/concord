"""Stage 0 — Votes scraper.

Walks ``api.congress.gov`` for House roll-call votes per
``(congress, session)`` slot. For each vote two endpoints are hit:

1. ``/v3/house-vote/{c}/{s}`` — list endpoint, paginated. Stubs only;
   used to discover roll numbers. Not persisted.
2. ``/v3/house-vote/{c}/{s}/{roll}`` — detail endpoint. One ADR 0006
   snapshot envelope is appended to ``data/house_votes.jsonl``.
3. ``/v3/house-vote/{c}/{s}/{roll}/members`` — per-member positions.
   One envelope appended to ``data/house_vote_positions.jsonl``.

Phase 3b adds ``scrape_senate`` alongside ``scrape_house`` here; both
write into the same canonical ``votes`` table via the loader.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import IO, Any, NamedTuple

from concord.api import Client
from concord.senate_xml import DETAIL_REQUEST_SLEEP_SECONDS, SenateClient

_log = logging.getLogger("concord.scraper.votes")

#: Canonical filename for the House vote detail snapshots inside
#: ``storage_dir``. Phase 3b adds ``senate_votes.jsonl`` alongside.
HOUSE_VOTES_JSONL_NAME = "house_votes.jsonl"

#: Canonical filename for the per-member position snapshots.
HOUSE_VOTE_POSITIONS_JSONL_NAME = "house_vote_positions.jsonl"

#: Senate detail-XML snapshots (one envelope per roll). Phase 3b.
SENATE_VOTES_JSONL_NAME = "senate_votes.jsonl"

#: Senate roster snapshots from senators_cfm.xml. Phase 3b.
SENATE_ROSTER_JSONL_NAME = "senate_roster.jsonl"


class ScrapeProgressEvent(NamedTuple):
    """Emitted once per ``(congress, session)`` pair."""

    chamber: str
    congress: int
    session: int
    votes_seen: int
    votes_written: int


class ScrapeStats(NamedTuple):
    """Outcome of one :func:`scrape_house` invocation."""

    votes_written: int
    positions_written: int
    votes_seen: int


class _PairResult(NamedTuple):
    seen: int
    written: int
    positions_written: int
    done: bool


def scrape_house(
    *,
    client: Client,
    congresses: Iterable[int],
    storage_dir: Path,
    fetched_at: datetime,
    sessions: Iterable[int] = (1, 2),
    limit: int | None = None,
    progress: Callable[[ScrapeProgressEvent], None] | None = None,
) -> ScrapeStats:
    """Append one detail + one members snapshot per House vote.

    For each ``(congress, session)`` slot the list endpoint is walked
    to discover roll numbers. For each roll the detail endpoint and
    the members endpoint are both fetched and one ADR 0006 envelope
    per response is appended to the corresponding JSONL file.

    ``limit`` counts *detail* fetches; for each detail fetched the
    paired members fetch always also runs so the two files stay
    aligned. The storage directory is created if missing; both files
    are opened in append mode.
    """
    storage_dir.mkdir(parents=True, exist_ok=True)
    detail_path = storage_dir / HOUSE_VOTES_JSONL_NAME
    members_path = storage_dir / HOUSE_VOTE_POSITIONS_JSONL_NAME
    iso = fetched_at.isoformat()
    sessions_tuple = tuple(sessions)

    total_written = 0
    total_positions = 0
    total_seen = 0
    done = False
    with (
        detail_path.open("a", encoding="utf-8") as detail_fh,
        members_path.open("a", encoding="utf-8") as members_fh,
    ):
        for congress in congresses:
            if done:
                break
            for session in sessions_tuple:
                if done:
                    break
                remaining = None if limit is None else max(0, limit - total_written)
                pair = _scrape_pair(
                    client=client,
                    congress=congress,
                    session=session,
                    detail_fh=detail_fh,
                    members_fh=members_fh,
                    iso=iso,
                    remaining=remaining,
                )
                total_seen += pair.seen
                total_written += pair.written
                total_positions += pair.positions_written
                if pair.done:
                    done = True
                if progress is not None:
                    progress(
                        ScrapeProgressEvent(
                            chamber="house",
                            congress=congress,
                            session=session,
                            votes_seen=pair.seen,
                            votes_written=pair.written,
                        )
                    )

    return ScrapeStats(
        votes_written=total_written,
        positions_written=total_positions,
        votes_seen=total_seen,
    )


def _scrape_pair(
    *,
    client: Client,
    congress: int,
    session: int,
    detail_fh: IO[str],
    members_fh: IO[str],
    iso: str,
    remaining: int | None,
) -> _PairResult:
    """Walk one ``(congress, session)`` slot; write detail + members envelopes."""
    seen = 0
    written = 0
    positions_written = 0
    done = False
    for stub in client.list_house_votes(congress, session):
        seen += 1
        roll_number = _parse_roll_number(stub)
        if roll_number is None:
            continue
        detail = client.get_house_vote_detail(congress, session, roll_number)
        key = {
            "chamber": "house",
            "congress": congress,
            "session": session,
            "roll_number": roll_number,
        }
        _append_envelope(detail_fh, iso=iso, key=key, payload=detail)
        if _fetch_and_write_members(client, congress, session, roll_number, members_fh, iso, key):
            positions_written += 1
        written += 1
        if remaining is not None and written >= remaining:
            done = True
            break
    return _PairResult(seen=seen, written=written, positions_written=positions_written, done=done)


def _parse_roll_number(stub: dict[str, Any]) -> int | None:
    raw = stub.get("rollCallNumber") or stub.get("rollNumber")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _append_envelope(
    fh: IO[str],
    *,
    iso: str,
    key: dict[str, Any],
    payload: Any,
) -> None:
    fh.write(json.dumps({"fetched_at": iso, "key": key, "payload": payload}, ensure_ascii=False))
    fh.write("\n")


def _fetch_and_write_members(
    client: Client,
    congress: int,
    session: int,
    roll_number: int,
    fh: IO[str],
    iso: str,
    key: dict[str, Any],
) -> bool:
    """Best-effort fetch + write of the members payload; returns True on success.

    A members-fetch failure doesn't roll back the detail write — re-running
    the scraper picks the missing members snapshot up next time.
    """
    try:
        members = client.get_house_vote_members(congress, session, roll_number)
    except Exception as exc:
        _log.warning(
            "members fetch failed for house/%s/%s/%s: %s",
            congress,
            session,
            roll_number,
            exc,
        )
        return False
    _append_envelope(fh, iso=iso, key=key, payload=members)
    return True


def scrape_senate(
    *,
    client_xml: SenateClient,
    congresses: Iterable[int],
    storage_dir: Path,
    fetched_at: datetime,
    sessions: Iterable[int] = (1, 2),
    limit: int | None = None,
    progress: Callable[[ScrapeProgressEvent], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> ScrapeStats:
    """Append snapshot envelopes for every Senate roll-call vote.

    For each ``(congress, session)`` slot the menu XML is fetched and
    parsed in-process to discover roll numbers (not persisted). For
    each roll the detail XML is fetched and appended verbatim to
    ``data/senate_votes.jsonl``. A single ``senators_cfm.xml`` fetch
    happens once per call and is appended to ``data/senate_roster.jsonl``.

    The detail XML envelope's ``payload`` is the raw XML decoded as
    UTF-8 (not parsed JSON) — keeping the load-step parsing in
    :mod:`concord.senate_xml` and the snapshot file robust to upstream
    schema changes. ``limit`` caps detail fetches; the roster fetch
    always runs.
    """
    storage_dir.mkdir(parents=True, exist_ok=True)
    detail_path = storage_dir / SENATE_VOTES_JSONL_NAME
    roster_path = storage_dir / SENATE_ROSTER_JSONL_NAME
    iso = fetched_at.isoformat()
    sessions_tuple = tuple(sessions)

    # Roster envelope — always fetched, even if --limit truncates votes.
    roster_xml = client_xml.get_current_senators_xml().decode("utf-8")
    with roster_path.open("a", encoding="utf-8") as roster_fh:
        _append_envelope(
            roster_fh,
            iso=iso,
            key={"source": "senators_cfm"},
            payload=roster_xml,
        )

    total_written = 0
    total_seen = 0
    done = False

    with detail_path.open("a", encoding="utf-8") as detail_fh:
        for congress in congresses:
            if done:
                break
            for session in sessions_tuple:
                if done:
                    break
                remaining = None if limit is None else max(0, limit - total_written)
                roll_numbers = client_xml.list_roll_call_numbers(congress, session)
                seen = len(roll_numbers)
                total_seen += seen
                pair_written = 0
                for roll_number in roll_numbers:
                    detail_bytes = client_xml.get_roll_call_xml(congress, session, roll_number)
                    detail_xml = detail_bytes.decode("utf-8")
                    _append_envelope(
                        detail_fh,
                        iso=iso,
                        key={
                            "chamber": "senate",
                            "congress": congress,
                            "session": session,
                            "roll_number": roll_number,
                        },
                        payload=detail_xml,
                    )
                    pair_written += 1
                    total_written += 1
                    if remaining is not None and pair_written >= remaining:
                        done = True
                        break
                    if DETAIL_REQUEST_SLEEP_SECONDS > 0:
                        sleep(DETAIL_REQUEST_SLEEP_SECONDS)
                if progress is not None:
                    progress(
                        ScrapeProgressEvent(
                            chamber="senate",
                            congress=congress,
                            session=session,
                            votes_seen=seen,
                            votes_written=pair_written,
                        )
                    )

    return ScrapeStats(
        votes_written=total_written,
        positions_written=0,
        votes_seen=total_seen,
    )


__all__ = [
    "HOUSE_VOTES_JSONL_NAME",
    "HOUSE_VOTE_POSITIONS_JSONL_NAME",
    "SENATE_ROSTER_JSONL_NAME",
    "SENATE_VOTES_JSONL_NAME",
    "ScrapeProgressEvent",
    "ScrapeStats",
    "scrape_house",
    "scrape_senate",
]
