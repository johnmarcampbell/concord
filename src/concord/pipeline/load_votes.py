"""Stage 1 — Votes loader.

Projects the two ADR 0009 JSONL files under ``<storage_dir>`` —
``house_votes.jsonl`` (detail snapshots) and
``house_vote_positions.jsonl`` (per-member positions) — into the
``votes`` and ``vote_positions`` SQLite tables.

The natural key is ``(chamber, congress, session, roll_number)``; the
loader groups each file by key, keeps the latest snapshot per key by
``fetched_at``, and upserts. Re-running over unchanged JSONL is a
no-op.

Phase 3b extends this loader to also read ``senate_votes.jsonl`` +
``senate_vote_positions.jsonl`` into the same tables (different
upstream shape, same target).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

from concord.models import parse_vote, parse_vote_positions, vote_id_from_components
from concord.scraper.votes import HOUSE_VOTE_POSITIONS_JSONL_NAME, HOUSE_VOTES_JSONL_NAME
from concord.storage.sqlite import SqliteStorage

_log = logging.getLogger("concord.pipeline.load_votes")

VoteKey = tuple[str, int, int, int]  # (chamber, congress, session, roll_number)


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
    """Project the latest Vote + positions snapshot per key into SQLite.

    Reads ``<storage_dir>/house_votes.jsonl`` and
    ``<storage_dir>/house_vote_positions.jsonl``. Either or both
    missing is a no-op for that file. Stops after ``limit`` vote rows
    have been UPSERTed when set.
    """
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

    storage = SqliteStorage(db_path, load_vec=False)
    votes_written = 0
    positions_written = 0
    try:
        with storage.transaction():
            for fetched_at, payload in latest_votes.values():
                try:
                    vote = parse_vote(payload)
                except Exception as exc:
                    malformed += 1
                    _log.warning("skipping vote after parse failure: %s", exc)
                    continue
                storage.upsert_vote(vote, fetched_at=fetched_at.isoformat())
                votes_written += 1
                if limit is not None and votes_written >= limit:
                    break

            for key, (_fetched_at, payload) in latest_positions.items():
                chamber, congress, session, roll_number = key
                vote_id = vote_id_from_components(chamber, congress, session, roll_number)
                positions = parse_vote_positions(payload)
                count = storage.upsert_vote_positions(vote_id, positions)
                positions_written += count
    finally:
        storage.close()

    return LoadStats(
        votes_written=votes_written,
        positions_written=positions_written,
        snapshots_read=snapshots_read,
        malformed=malformed,
    )


def _ingest_envelopes(
    path: Path,
    latest_per_key: dict[VoteKey, tuple[datetime, dict[str, Any]]],
) -> tuple[int, int]:
    """Read one JSONL file, populate ``latest_per_key`` in place.

    Returns ``(snapshots_read, malformed)``.
    """
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


__all__ = ["LoadStats", "load"]
