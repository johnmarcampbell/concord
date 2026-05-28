"""Stage 1 — Votes loader.

Projects the four ADR 0009 JSONL files under ``<storage_dir>`` into
the ``votes`` and ``vote_positions`` SQLite tables:

- ``house_votes.jsonl`` — House detail snapshots from api.congress.gov.
- ``house_vote_positions.jsonl`` — House per-member positions.
- ``senate_votes.jsonl`` — Senate detail XML snapshots (raw payload),
  parsed at load time via :mod:`concord.senate_xml`.
- ``senate_roster.jsonl`` — Senate roster snapshots (``senators_cfm.xml``);
  used at load time to build the LIS→Bioguide bridge for the
  position rows.

House: natural key is ``(chamber, congress, session, roll_number)``;
the loader groups each file by key, keeps the latest snapshot per key
by ``fetched_at``, and upserts.

Senate: positions in the XML are keyed by ``member_full``
(``"Surname (P-ST)"``); the loader resolves each to a Bioguide ID via
(1) the latest ``senators_cfm.xml`` snapshot and, when missing,
(2) a date-overlap query against the Phase 1 ``members`` /
``member_terms`` tables. Unresolved positions log a warning and are
skipped; the parent vote row still loads. Re-running over unchanged
JSONL is a no-op for both chambers.
"""

import json
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import ValidationError

from concord.models import (
    ParsedVoteDetail,
    Vote,
    VotePosition,
    vote_id_from_components,
)
from concord.scraper.votes import (
    HOUSE_VOTE_POSITIONS_JSONL_NAME,
    HOUSE_VOTES_JSONL_NAME,
    SENATE_ROSTER_JSONL_NAME,
    SENATE_VOTES_JSONL_NAME,
)
from concord.senate_xml import parse_senate_roster, parse_vote_detail
from concord.storage.sqlite import SqliteStorage

_log = logging.getLogger("concord.pipeline.load_votes")

VoteKey = tuple[str, int, int, int]  # (chamber, congress, session, roll_number)

#: Senate ``member_full`` pattern: ``"Surname (P-ST)"`` — also handles
#: hyphenated surnames (``"Hyde-Smith (R-MS)"``) and multi-word ones
#: (``"Van Hollen (D-MD)"``). Party group is broad to tolerate party
#: switches between the vote date and the roster snapshot.
_MEMBER_FULL_RE = re.compile(r"^(?P<last>[^()]+?)\s*\((?P<party>[A-Z]+)-(?P<state>[A-Z]{2})\)$")


class LoadStats(NamedTuple):
    """Outcome of one :func:`load` invocation."""

    votes_written: int
    positions_written: int
    snapshots_read: int
    malformed: int


def load(
    *,
    storage_dir: Path,
    db_path: Path,
    limit: int | None = None,
) -> LoadStats:
    """Project the latest snapshots per key into ``votes`` + ``vote_positions``.

    Reads up to four JSONL files under ``storage_dir``. Missing files
    are a no-op for that chamber. ``limit`` caps the *total* vote rows
    UPSERTed across both chambers.
    """
    storage = SqliteStorage(db_path, load_vec=False)
    votes_written = 0
    positions_written = 0
    snapshots_read = 0
    malformed = 0
    try:
        with storage.transaction():
            house_stats = _load_house(
                storage,
                storage_dir,
                limit=limit,
            )
            votes_written += house_stats.votes_written
            positions_written += house_stats.positions_written
            snapshots_read += house_stats.snapshots_read
            malformed += house_stats.malformed

            remaining = None if limit is None else max(0, limit - votes_written)
            senate_stats = _load_senate(
                storage,
                storage_dir,
                limit=remaining,
            )
            votes_written += senate_stats.votes_written
            positions_written += senate_stats.positions_written
            snapshots_read += senate_stats.snapshots_read
            malformed += senate_stats.malformed
    finally:
        storage.close()

    return LoadStats(
        votes_written=votes_written,
        positions_written=positions_written,
        snapshots_read=snapshots_read,
        malformed=malformed,
    )


def _load_house(
    storage: SqliteStorage,
    storage_dir: Path,
    *,
    limit: int | None,
) -> LoadStats:
    """House branch — JSON payloads from api.congress.gov."""
    votes_path = storage_dir / HOUSE_VOTES_JSONL_NAME
    positions_path = storage_dir / HOUSE_VOTE_POSITIONS_JSONL_NAME

    snapshots_read = 0
    malformed = 0

    latest_votes: dict[VoteKey, tuple[datetime, dict[str, Any]]] = {}
    if votes_path.exists():
        read, bad = _ingest_envelopes(votes_path, latest_votes)
        snapshots_read += read
        malformed += bad

    latest_positions: dict[VoteKey, tuple[datetime, dict[str, Any]]] = {}
    if positions_path.exists():
        read, bad = _ingest_envelopes(positions_path, latest_positions)
        snapshots_read += read
        malformed += bad

    votes_written = 0
    positions_written = 0
    for key, (fetched_at, payload) in latest_votes.items():
        # The API leaves ``chamber`` ``null`` on some payloads; we know it
        # from the envelope key (the loader queried /house-vote/…), so pass
        # it explicitly rather than letting the parser guess.
        chamber = key[0]
        try:
            vote = Vote.from_congress_api(payload, chamber=chamber)
        except (KeyError, ValueError, ValidationError) as exc:
            malformed += 1
            _log.warning("skipping house vote after parse failure: %s; payload=%r", exc, payload)
            continue
        storage.upsert_vote(vote, fetched_at=fetched_at.isoformat())
        votes_written += 1
        if limit is not None and votes_written >= limit:
            break

    for key, (_fetched_at, payload) in latest_positions.items():
        chamber, congress, session, roll_number = key
        vote_id = vote_id_from_components(chamber, congress, session, roll_number)
        positions = _parse_position_rows(payload, vote_id)
        count = storage.upsert_vote_positions(vote_id, positions)
        positions_written += count

    return LoadStats(
        votes_written=votes_written,
        positions_written=positions_written,
        snapshots_read=snapshots_read,
        malformed=malformed,
    )


def _load_senate(
    storage: SqliteStorage,
    storage_dir: Path,
    *,
    limit: int | None,
) -> LoadStats:
    """Senate branch — raw XML payloads from senate.gov LIS feeds."""
    votes_path = storage_dir / SENATE_VOTES_JSONL_NAME
    roster_path = storage_dir / SENATE_ROSTER_JSONL_NAME

    snapshots_read = 0
    malformed = 0

    # Empty bridge if no roster file — historical fallback via members table
    # still works.
    bridge: dict[str, str] = {}
    if roster_path.exists():
        roster_xml = _latest_string_payload(roster_path)
        if roster_xml is not None:
            try:
                bridge = parse_senate_roster(roster_xml.encode("utf-8"))
            except Exception as exc:
                _log.warning("could not parse senators_cfm roster: %s", exc)
        snapshots_read += _count_lines(roster_path)

    if not votes_path.exists():
        return LoadStats(
            votes_written=0,
            positions_written=0,
            snapshots_read=snapshots_read,
            malformed=malformed,
        )

    latest_votes: dict[VoteKey, tuple[datetime, str]] = {}
    read, bad = _ingest_string_envelopes(votes_path, latest_votes)
    snapshots_read += read
    malformed += bad

    votes_written = 0
    positions_written = 0
    conn = storage.connection
    for fetched_at, xml_payload in latest_votes.values():
        try:
            detail = parse_vote_detail(xml_payload.encode("utf-8"))
        except Exception as exc:
            malformed += 1
            _log.warning("skipping senate vote after parse failure: %s", exc)
            continue
        vote = _vote_from_parsed_detail(detail)
        storage.upsert_vote(vote, fetched_at=fetched_at.isoformat())
        votes_written += 1

        positions = _resolve_positions(detail, bridge, conn)
        if positions:
            storage.upsert_vote_positions(detail.vote_id, positions)
            positions_written += len(positions)
        else:
            # Still clear stale position rows for an idempotent rerun.
            storage.upsert_vote_positions(detail.vote_id, [])

        if limit is not None and votes_written >= limit:
            break

    return LoadStats(
        votes_written=votes_written,
        positions_written=positions_written,
        snapshots_read=snapshots_read,
        malformed=malformed,
    )


def _parse_position_rows(payload: dict[str, Any], vote_id: str) -> list[VotePosition]:
    """Project a ``/.../members`` payload's ``results`` array into VotePositions.

    Each row goes through :meth:`VotePosition.from_congress_api`; a row that
    fails validation is logged with its payload and skipped (the API has
    occasionally emitted placeholders for vacant seats). Duplicates by
    Bioguide are deduped, keeping the last row.
    """
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    by_bioguide: dict[str, VotePosition] = {}
    for row in results:
        if not isinstance(row, dict):
            continue
        try:
            position = VotePosition.from_congress_api(row)
        except (KeyError, ValueError, ValidationError) as exc:
            _log.warning("skipping position row for %s: %s; payload=%r", vote_id, exc, row)
            continue
        by_bioguide[position.bioguide_id] = position
    return list(by_bioguide.values())


def _vote_from_parsed_detail(detail: ParsedVoteDetail) -> Vote:
    """Project a Senate ``ParsedVoteDetail`` into the canonical ``Vote`` model."""
    return Vote(
        vote_id=detail.vote_id,
        chamber=detail.chamber,
        congress=detail.congress,
        session=detail.session,
        roll_number=detail.roll_number,
        vote_kind=detail.vote_kind,
        start_date=detail.start_date,
        vote_question=detail.vote_question,
        vote_type=detail.vote_type,
        threshold=detail.threshold,
        result=detail.result,
        yea_count=detail.yea_count,
        nay_count=detail.nay_count,
        present_count=detail.present_count,
        not_voting_count=detail.not_voting_count,
        bill_id=detail.bill_id,
        amendment_id=detail.amendment_id,
        is_party_unity=False,
        update_date=detail.update_date,
    )


def _resolve_positions(
    detail: ParsedVoteDetail,
    bridge: dict[str, str],
    conn: sqlite3.Connection,
) -> list[VotePosition]:
    """Map each parsed Senate position to a ``VotePosition`` row.

    Resolution: direct hit in the senators_cfm bridge → done. Miss → a
    last-name + state + date-overlap query against ``members`` /
    ``member_terms``. Failure → log + skip (the vote still loads).
    """
    vote_date = _date_only(detail.start_date)
    positions: list[VotePosition] = []
    for p in detail.positions:
        bioguide = bridge.get(p.member_full)
        if bioguide is None:
            bioguide = _resolve_historical(p.member_full, vote_date, conn)
        if bioguide is None:
            _log.warning(
                "unresolved_member: vote_id=%s member_full=%r — position skipped",
                detail.vote_id,
                p.member_full,
            )
            continue
        positions.append(
            VotePosition(
                bioguide_id=bioguide,
                position=p.vote_cast,
                vote_party=p.party,
                vote_state=p.state,
            )
        )
    return positions


def _resolve_historical(
    member_full: str,
    vote_date: str | None,
    conn: sqlite3.Connection,
) -> str | None:
    """Date-overlap query against ``members``/``member_terms`` for past senators.

    Returns ``None`` when the bridge parse fails, the query has zero
    hits, or the query has more than one hit — the load step doesn't
    guess between candidates.
    """
    match = _MEMBER_FULL_RE.match(member_full.strip())
    if match is None:
        return None
    last_name = match.group("last").strip()
    state = match.group("state").strip()
    if vote_date is None:
        # Without a date we can't distinguish overlapping terms; skip.
        return None
    cursor = conn.execute(
        """
        SELECT DISTINCT m.bioguide_id
        FROM members m
        JOIN member_terms t ON m.bioguide_id = t.bioguide_id
        WHERE t.chamber = 'senate'
          AND m.last_name = ?
          AND t.state = ?
          AND (t.start_date IS NULL OR t.start_date <= ?)
          AND (t.end_date IS NULL OR t.end_date >= ?)
        """,
        (last_name, state, vote_date, vote_date),
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        return None
    return str(rows[0][0])


#: Length of ``YYYY-MM-DD`` — the ISO 8601 date prefix.
_ISO_DATE_LEN = 10


def _date_only(start_date: str) -> str | None:
    """Extract the ISO date prefix (``YYYY-MM-DD``) from a Vote.start_date."""
    if not start_date or len(start_date) < _ISO_DATE_LEN:
        return None
    return start_date[:_ISO_DATE_LEN]


def _ingest_envelopes(
    path: Path,
    latest_per_key: dict[VoteKey, tuple[datetime, dict[str, Any]]],
) -> tuple[int, int]:
    """Read one JSONL file with dict payloads, populate ``latest_per_key`` in place."""
    snapshots_read = 0
    malformed = 0
    with path.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            snapshots_read += 1
            try:
                envelope = json.loads(line)
                key_raw = envelope["key"]
                chamber = str(key_raw["chamber"]).lower()
                congress = int(key_raw["congress"])
                session = int(key_raw["session"])
                roll_number = int(key_raw["roll_number"])
                fetched_at = datetime.fromisoformat(envelope["fetched_at"])
                payload = envelope["payload"]
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                malformed += 1
                _log.warning("skipping malformed %s line %d: %s", path.name, line_no, exc)
                continue
            key: VoteKey = (chamber, congress, session, roll_number)
            current = latest_per_key.get(key)
            if current is None or fetched_at > current[0]:
                latest_per_key[key] = (fetched_at, payload)
    return snapshots_read, malformed


def _ingest_string_envelopes(
    path: Path,
    latest_per_key: dict[VoteKey, tuple[datetime, str]],
) -> tuple[int, int]:
    """Same as ``_ingest_envelopes`` but ``payload`` is a string (raw XML)."""
    snapshots_read = 0
    malformed = 0
    with path.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            snapshots_read += 1
            try:
                envelope = json.loads(line)
                key_raw = envelope["key"]
                chamber = str(key_raw["chamber"]).lower()
                congress = int(key_raw["congress"])
                session = int(key_raw["session"])
                roll_number = int(key_raw["roll_number"])
                fetched_at = datetime.fromisoformat(envelope["fetched_at"])
                payload = envelope["payload"]
                if not isinstance(payload, str):
                    raise TypeError("senate payload must be a string")  # noqa: TRY301
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                malformed += 1
                _log.warning("skipping malformed %s line %d: %s", path.name, line_no, exc)
                continue
            key: VoteKey = (chamber, congress, session, roll_number)
            current = latest_per_key.get(key)
            if current is None or fetched_at > current[0]:
                latest_per_key[key] = (fetched_at, payload)
    return snapshots_read, malformed


def _latest_string_payload(path: Path) -> str | None:
    """Return the latest ``payload`` (by ``fetched_at``) from a string-payload JSONL.

    Used for the senate_roster.jsonl file where the key shape is
    ``{"source": "senators_cfm"}`` — there's no per-roll key to group
    by, just "latest snapshot wins".
    """
    latest_at: datetime | None = None
    latest_payload: str | None = None
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                envelope = json.loads(line)
                fetched_at = datetime.fromisoformat(envelope["fetched_at"])
                payload = envelope["payload"]
                if not isinstance(payload, str):
                    continue
            except (KeyError, ValueError, json.JSONDecodeError):
                continue
            if latest_at is None or fetched_at > latest_at:
                latest_at = fetched_at
                latest_payload = payload
    return latest_payload


def _count_lines(path: Path) -> int:
    with path.open("r", encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip())


__all__ = ["LoadStats", "load"]
