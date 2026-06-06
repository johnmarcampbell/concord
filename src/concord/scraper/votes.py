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

import logging
import time
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import IO, Any, NamedTuple

from concord.api import Client
from concord.scraper._common import (
    append_snapshot,
    is_stub_unchanged,
    load_freshness_map,
    parse_signal_timestamp,
)
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
    is_pair_done: bool = False
    category_total: int | None = None


class ScrapeStats(NamedTuple):
    """Outcome of one :func:`scrape_house` or :func:`scrape_senate` invocation."""

    votes_written: int
    positions_written: int
    votes_seen: int
    votes_skipped: int = 0
    positions_skipped: int = 0


class _PairResult(NamedTuple):
    seen: int
    written: int
    positions_written: int
    done: bool
    pair_total: int | None = None
    skipped: int = 0
    positions_skipped: int = 0


def scrape_house(
    *,
    client: Client,
    congresses: Iterable[int],
    storage_dir: Path,
    fetched_at: datetime,
    sessions: Iterable[int] = (1, 2),
    limit: int | None = None,
    progress: Callable[[ScrapeProgressEvent], None] | None = None,
    skip_unchanged: bool = False,
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
    sessions_tuple = tuple(sessions)

    # Detail and positions refresh independently under ``skip_unchanged``
    # (ADR 0015) — so a positions-only failure on the previous run
    # doesn't strand the roll when detail is fresh.
    key_fields = ("chamber", "congress", "session", "roll_number")
    detail_freshness = load_freshness_map(detail_path, key_fields) if skip_unchanged else {}
    positions_freshness = load_freshness_map(members_path, key_fields) if skip_unchanged else {}

    total_written = 0
    total_positions = 0
    total_seen = 0
    total_skipped = 0
    total_positions_skipped = 0
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
                    fetched_at=fetched_at,
                    remaining=remaining,
                    skip_unchanged=skip_unchanged,
                    detail_freshness=detail_freshness,
                    positions_freshness=positions_freshness,
                )
                total_seen += pair.seen
                total_written += pair.written
                total_positions += pair.positions_written
                total_skipped += pair.skipped
                total_positions_skipped += pair.positions_skipped
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
                            is_pair_done=True,
                            category_total=pair.pair_total,
                        )
                    )

    return ScrapeStats(
        votes_written=total_written,
        positions_written=total_positions,
        votes_seen=total_seen,
        votes_skipped=total_skipped,
        positions_skipped=total_positions_skipped,
    )


def _scrape_pair(  # noqa: PLR0913 — pair worker forwards state from scrape_house
    *,
    client: Client,
    congress: int,
    session: int,
    detail_fh: IO[str],
    members_fh: IO[str],
    fetched_at: datetime,
    remaining: int | None,
    per_vote_progress: Callable[[ScrapeProgressEvent], None] | None = None,
    skip_unchanged: bool = False,
    detail_freshness: dict[tuple[Any, ...], datetime] | None = None,
    positions_freshness: dict[tuple[Any, ...], datetime] | None = None,
) -> _PairResult:
    """Walk one ``(congress, session)`` slot; write detail + members envelopes."""
    detail_freshness = detail_freshness or {}
    positions_freshness = positions_freshness or {}
    seen = 0
    written = 0
    skipped = 0
    positions_written = 0
    positions_skipped = 0
    done = False
    pair_total: int | None = None

    def _capture_total(t: int) -> None:
        nonlocal pair_total
        pair_total = t

    for stub in client.list_house_votes(congress, session, on_total=_capture_total):
        seen += 1
        roll_number = _parse_roll_number(stub)
        if roll_number is None:
            # ADR 0018: skip only when the envelope key cannot be
            # constructed; without a roll number we cannot fetch detail.
            _log.warning(
                "scraper.skip.missing_key",
                extra={
                    "source": "congress.gov/v3/house-vote",
                    "congress": congress,
                    "session": session,
                    "missing": "rollCallNumber",
                    "fragment": {
                        "voteQuestion": stub.get("voteQuestion"),
                        "updateDate": stub.get("updateDate"),
                    },
                },
            )
            continue
        roll_key: tuple[Any, ...] = ("house", congress, session, roll_number)
        signal = parse_signal_timestamp(stub.get("updateDate")) if skip_unchanged else None
        skip_detail = skip_unchanged and is_stub_unchanged(
            freshness=detail_freshness, key=roll_key, signal=signal
        )
        skip_positions = skip_unchanged and is_stub_unchanged(
            freshness=positions_freshness, key=roll_key, signal=signal
        )
        if skip_detail and skip_positions:
            skipped += 1
            positions_skipped += 1
            continue
        key: dict[str, str | int] = {
            "chamber": "house",
            "congress": congress,
            "session": session,
            "roll_number": roll_number,
        }
        if not skip_detail:
            detail = client.get_house_vote_detail(congress, session, roll_number)
            append_snapshot(detail_fh, fetched_at=fetched_at, key=key, payload=detail)
            written += 1
        else:
            skipped += 1
        if not skip_positions:
            if _fetch_and_write_members(
                client, congress, session, roll_number, members_fh, fetched_at, key
            ):
                positions_written += 1
        else:
            positions_skipped += 1
        if per_vote_progress is not None:
            per_vote_progress(
                ScrapeProgressEvent(
                    chamber="house",
                    congress=congress,
                    session=session,
                    votes_seen=seen,
                    votes_written=written,
                    category_total=pair_total,
                )
            )
        if remaining is not None and written >= remaining:
            done = True
            break
    return _PairResult(
        seen=seen,
        written=written,
        positions_written=positions_written,
        done=done,
        pair_total=pair_total,
        skipped=skipped,
        positions_skipped=positions_skipped,
    )


def _parse_roll_number(stub: dict[str, Any]) -> int | None:
    raw = stub.get("rollCallNumber") or stub.get("rollNumber")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _fetch_and_write_members(
    client: Client,
    congress: int,
    session: int,
    roll_number: int,
    fh: IO[str],
    fetched_at: datetime,
    key: dict[str, str | int],
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
    append_snapshot(fh, fetched_at=fetched_at, key=key, payload=members)
    return True


def scrape_senate(  # noqa: PLR0913 — kwargs match scrape_house surface
    *,
    client_xml: SenateClient,
    congresses: Iterable[int],
    storage_dir: Path,
    fetched_at: datetime,
    sessions: Iterable[int] = (1, 2),
    limit: int | None = None,
    progress: Callable[[ScrapeProgressEvent], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    skip_unchanged: bool = False,
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
    sessions_tuple = tuple(sessions)

    # Roster envelope — always fetched, even if --limit truncates votes.
    roster_xml = client_xml.get_current_senators_xml().decode("utf-8")
    with roster_path.open("a", encoding="utf-8") as roster_fh:
        append_snapshot(
            roster_fh,
            fetched_at=fetched_at,
            key={"source": "senators_cfm"},
            payload=roster_xml,
        )

    # Senate menu XML carries no per-roll modify-date (ADR 0015) — skip
    # is presence-only. ``known_keys`` is the set of rolls already
    # snapshotted in senate_votes.jsonl.
    known_keys: set[tuple[Any, ...]] = (
        set(
            load_freshness_map(
                detail_path, ("chamber", "congress", "session", "roll_number")
            ).keys()
        )
        if skip_unchanged
        else set()
    )

    total_written = 0
    total_seen = 0
    total_skipped = 0
    done = False

    with detail_path.open("a", encoding="utf-8") as detail_fh:
        for congress in congresses:
            if done:
                break
            for session in sessions_tuple:
                if done:
                    break
                pair = _scrape_senate_pair(
                    client_xml=client_xml,
                    congress=congress,
                    session=session,
                    detail_fh=detail_fh,
                    fetched_at=fetched_at,
                    remaining=None if limit is None else max(0, limit - total_written),
                    skip_unchanged=skip_unchanged,
                    known_keys=known_keys,
                    sleep=sleep,
                    progress=progress,
                )
                total_seen += pair.seen
                total_written += pair.written
                total_skipped += pair.skipped
                if pair.done:
                    done = True

    return ScrapeStats(
        votes_written=total_written,
        positions_written=0,
        votes_seen=total_seen,
        votes_skipped=total_skipped,
    )


class _SenatePairResult(NamedTuple):
    seen: int
    written: int
    skipped: int
    done: bool


def _scrape_senate_pair(  # noqa: PLR0913 — pair worker forwards state from scrape_senate
    *,
    client_xml: SenateClient,
    congress: int,
    session: int,
    detail_fh: IO[str],
    fetched_at: datetime,
    remaining: int | None,
    skip_unchanged: bool,
    known_keys: set[tuple[Any, ...]],
    sleep: Callable[[float], None],
    progress: Callable[[ScrapeProgressEvent], None] | None,
) -> _SenatePairResult:
    roll_numbers = client_xml.list_roll_call_numbers(congress, session)
    seen = len(roll_numbers)
    senate_pair_total = len(roll_numbers)
    pair_written = 0
    pair_skipped = 0
    done = False
    for roll_number in roll_numbers:
        if skip_unchanged and ("senate", congress, session, roll_number) in known_keys:
            pair_skipped += 1
            continue
        detail_bytes = client_xml.get_roll_call_xml(congress, session, roll_number)
        detail_xml = detail_bytes.decode("utf-8")
        append_snapshot(
            detail_fh,
            fetched_at=fetched_at,
            key={
                "chamber": "senate",
                "congress": congress,
                "session": session,
                "roll_number": roll_number,
            },
            payload=detail_xml,
        )
        pair_written += 1
        if progress is not None:
            progress(
                ScrapeProgressEvent(
                    chamber="senate",
                    congress=congress,
                    session=session,
                    votes_seen=seen,
                    votes_written=pair_written,
                    category_total=senate_pair_total,
                )
            )
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
                is_pair_done=True,
                category_total=senate_pair_total,
            )
        )
    return _SenatePairResult(seen=seen, written=pair_written, skipped=pair_skipped, done=done)


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
