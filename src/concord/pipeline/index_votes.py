"""Stage 2 — Votes indexer.

Computes two derived datasets from the ``votes`` + ``vote_positions``
tables:

1. ``votes.is_party_unity`` — a per-vote boolean. True iff a majority
   of Republican Yea/Nay positions opposed a majority of Democratic
   Yea/Nay positions on that vote. Election votes are never flagged.
2. ``member_party_unity`` — one row per ``(bioguide_id, congress)``
   for Republican and Democratic Members. Carries the denominator
   (count of party-unity votes the Member cast Yea/Nay on) and the
   numerator (of those, the count where the Member agreed with their
   party's majority).

Both passes are truncate-then-repopulate: re-running converges to the
latest snapshot of the underlying tables. See [ADR 0011] for the
methodology.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import NamedTuple

from concord.storage.sqlite import SqliteStorage

#: Members below this many party-unity votes in a Congress are shown
#: with the "(not enough votes yet)" treatment on the web layer. The
#: indexer still writes the row; the UI suppresses the percentage.
PARTY_UNITY_MIN_VOTES = 10

_PARTIES_MAJOR = ("R", "D")

# SQL kept as module-level constants — only `?` placeholders are used at
# runtime for any user-influenced values.
_PARTY_MAJORITIES_SQL = """
    SELECT
        vp.vote_id      AS vote_id,
        vp.vote_party   AS vote_party,
        vp.position     AS position,
        COUNT(*)        AS n
    FROM vote_positions vp
    JOIN votes v ON v.vote_id = vp.vote_id
    WHERE v.vote_kind = 'standard'
      AND vp.vote_party IN ('R', 'D')
      AND vp.position IN ('Yea', 'Nay')
    GROUP BY vp.vote_id, vp.vote_party, vp.position
"""

_POSITIONS_SQL = """
    SELECT
        vp.bioguide_id  AS bioguide_id,
        v.congress      AS congress,
        vp.vote_id      AS vote_id,
        vp.vote_party   AS vote_party,
        vp.position     AS position
    FROM vote_positions vp
    JOIN votes v ON v.vote_id = vp.vote_id
    WHERE v.is_party_unity = 1
      AND vp.position IN ('Yea', 'Nay')
"""


class IndexStats(NamedTuple):
    """Outcome of one :func:`index` invocation."""

    votes_flagged_party_unity: int
    members_scored: int


def index(*, db_path: Path, limit: int | None = None) -> IndexStats:
    """Recompute ``votes.is_party_unity`` + ``member_party_unity``.

    ``limit`` is a **spot-check knob only**: it caps the row set in
    the numerator-pass ``vote_positions`` query, leaving the first-pass
    party-majority computation untouched. With a small limit some
    Members' denominators are truncated mid-Member, producing
    intentionally incomplete `member_party_unity` rows — that's
    acceptable for "did the pipeline wire up at all" checks but not
    for any production stat. Production runs leave it unset; the
    full party-unity pass takes <30s at 3-Congress scope.
    """
    storage = SqliteStorage(db_path, load_vec=False)
    try:
        conn = storage.connection
        majorities = _compute_party_majorities(conn)
        party_unity_votes = _votes_with_party_split(majorities)
        _apply_is_party_unity_flag(conn, party_unity_votes)

        conn.execute("DELETE FROM member_party_unity")
        if not party_unity_votes:
            conn.commit()
            return IndexStats(votes_flagged_party_unity=0, members_scored=0)

        members_scored = _populate_member_party_unity(conn, majorities, limit=limit)
        conn.commit()
        return IndexStats(
            votes_flagged_party_unity=len(party_unity_votes),
            members_scored=members_scored,
        )
    finally:
        storage.close()


def _compute_party_majorities(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str], str]:
    """Per (vote_id, party): which side ('Yea' / 'Nay') won that party's majority."""
    totals: dict[tuple[str, str], dict[str, int]] = {}
    for row in conn.execute(_PARTY_MAJORITIES_SQL):
        key = (row["vote_id"], row["vote_party"])
        totals.setdefault(key, {"Yea": 0, "Nay": 0})[row["position"]] = int(row["n"])
    majorities: dict[tuple[str, str], str] = {}
    for key, t in totals.items():
        if t["Yea"] == t["Nay"]:
            continue
        majorities[key] = "Yea" if t["Yea"] > t["Nay"] else "Nay"
    return majorities


def _votes_with_party_split(majorities: dict[tuple[str, str], str]) -> set[str]:
    """Votes where the R majority and the D majority landed on opposite sides."""
    party_unity: set[str] = set()
    vote_ids = {vote_id for (vote_id, _party) in majorities}
    for vote_id in vote_ids:
        r_maj = majorities.get((vote_id, "R"))
        d_maj = majorities.get((vote_id, "D"))
        if r_maj is None or d_maj is None:
            continue
        if r_maj != d_maj:
            party_unity.add(vote_id)
    return party_unity


def _apply_is_party_unity_flag(
    conn: sqlite3.Connection,
    party_unity_votes: set[str],
) -> None:
    """Reset every vote's flag to 0, then mark the party-unity set as 1."""
    conn.execute("UPDATE votes SET is_party_unity = 0")
    if not party_unity_votes:
        return
    ids = list(party_unity_votes)
    chunk = 500
    for start in range(0, len(ids), chunk):
        slice_ = ids[start : start + chunk]
        placeholders = ",".join("?" for _ in slice_)
        sql = f"UPDATE votes SET is_party_unity = 1 WHERE vote_id IN ({placeholders})"  # noqa: S608
        conn.execute(sql, slice_)


def _populate_member_party_unity(
    conn: sqlite3.Connection,
    majorities: dict[tuple[str, str], str],
    *,
    limit: int | None,
) -> int:
    """Tally per-Member numerator/denominator from `vote_positions`; insert rows.

    Returns the count of `member_party_unity` rows written.
    """
    sql = _POSITIONS_SQL
    if limit is not None:
        sql = sql + f" LIMIT {int(limit)}"

    per_member: dict[tuple[str, int], dict[str, int]] = {}
    per_member_party: dict[tuple[str, int], dict[str, int]] = {}
    for row in conn.execute(sql):
        party = row["vote_party"]
        if party not in {"R", "D", "I"}:
            continue
        mkey = (row["bioguide_id"], int(row["congress"]))
        per_member_party.setdefault(mkey, {"R": 0, "D": 0, "I": 0})[party] += 1
        if party not in _PARTIES_MAJOR:
            continue
        counts = per_member.setdefault(mkey, {"denom": 0, "numer": 0})
        counts["denom"] += 1
        majority_side = majorities.get((row["vote_id"], party))
        if majority_side is not None and row["position"] == majority_side:
            counts["numer"] += 1

    written = 0
    for mkey, counts in per_member.items():
        party_counts = per_member_party[mkey]
        modal_party = max(party_counts, key=lambda p: party_counts[p])
        if modal_party not in _PARTIES_MAJOR or counts["denom"] == 0:
            continue
        bioguide, congress = mkey
        conn.execute(
            "INSERT INTO member_party_unity "
            "(bioguide_id, congress, party, party_unity_votes_cast, party_line_votes) "
            "VALUES (?, ?, ?, ?, ?)",
            (bioguide, congress, modal_party, counts["denom"], counts["numer"]),
        )
        written += 1
    return written


__all__ = ["PARTY_UNITY_MIN_VOTES", "IndexStats", "index"]
