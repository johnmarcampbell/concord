"""Bill storage helpers (Phase 2, ADR 0008).

Owns the ``bills`` mirror table, its Phase-2b tier-2 child tables
(``bill_cosponsors`` / ``bill_actions`` / ``bill_subjects`` / ``bill_titles``
/ ``bill_summaries``), the ``bills_fts`` search index, and the ``bill_briefs``
record table (ADR 0019/0020): their DDL, column tuples, INSERT/UPSERT SQL, the
BillDetail serializer, the schema migrations that grew this seam, and the
persistence/query helpers. ``SqliteStorage`` composes these and owns the
transaction boundary; the helpers here are pure SQL over a connection, except
:func:`upsert_bill`, whose single-row write owns its own commit.
"""

import sqlite3
from collections.abc import Sequence
from typing import Any

from concord.models.bills import (
    BillAction,
    BillCosponsor,
    BillDetail,
    BillSubject,
    BillSummary,
    BillTitle,
)
from concord.storage._ddl import rebuild_table_add_not_null
from concord.storage._sql import insert_sql, upsert_sql

BILLS_SCHEMA = """
-- Bills (Phase 2a). Identity record per Bill — sponsor goes here too
-- because Congress's rules cap one Sponsor per Bill. The mutable
-- political-graph data (cosponsors, actions, subjects, titles,
-- summaries) is added in Phase 2b as child tables. ``bill_id`` is the
-- flattened "{congress}-{bill_type}-{bill_number}" PK chosen so Phase 5
-- chunks linkage matches ADR 0008. The sponsor column is bare TEXT (no
-- REFERENCES members) so ingest is robust to any Phase 1 gap.
CREATE TABLE IF NOT EXISTS bills (
    bill_id              TEXT PRIMARY KEY,
    congress             INTEGER NOT NULL,
    bill_type            TEXT NOT NULL
        CHECK (bill_type IN ('hr', 'hres', 'hjres', 'hconres', 's', 'sres', 'sjres', 'sconres')),
    bill_number          INTEGER NOT NULL,
    origin_chamber       TEXT NOT NULL
        CHECK (origin_chamber IN ('House', 'Senate')),
    title                TEXT NOT NULL,
    introduced_date      TEXT,
    policy_area          TEXT,
    sponsor_bioguide_id  TEXT,
    latest_action_date   TEXT,
    latest_action_text   TEXT,
    update_date          TEXT NOT NULL,
    fetched_at           TEXT NOT NULL,
    cosponsors_fetched_at TEXT,
    actions_fetched_at    TEXT,
    subjects_fetched_at   TEXT,
    titles_fetched_at     TEXT,
    summaries_fetched_at  TEXT,
    last_enrichment_error TEXT,
    UNIQUE (congress, bill_type, bill_number)
);

CREATE INDEX IF NOT EXISTS idx_bills_sponsor
    ON bills (sponsor_bioguide_id);
CREATE INDEX IF NOT EXISTS idx_bills_latest_action
    ON bills (latest_action_date DESC);
CREATE INDEX IF NOT EXISTS idx_bills_policy_area
    ON bills (policy_area);
CREATE INDEX IF NOT EXISTS idx_bills_congress
    ON bills (congress);

-- Tier-2 child tables (Phase 2b). Each row points back to bills via
-- bill_id with ON DELETE CASCADE so wiping a Bill takes its enrichment
-- with it. bioguide_id on bill_cosponsors is bare TEXT (no FK) so the
-- table doesn't depend on Phase 1 having indexed every cosponsoring
-- Member.
CREATE TABLE IF NOT EXISTS bill_cosponsors (
    bill_id                     TEXT NOT NULL REFERENCES bills(bill_id) ON DELETE CASCADE,
    bioguide_id                 TEXT NOT NULL,
    sponsorship_date            TEXT NOT NULL,
    sponsorship_withdrawn_date  TEXT,
    is_original_cosponsor       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bill_id, bioguide_id)
);
CREATE INDEX IF NOT EXISTS idx_bill_cosponsors_bioguide
    ON bill_cosponsors (bioguide_id);

CREATE TABLE IF NOT EXISTS bill_actions (
    bill_id        TEXT NOT NULL REFERENCES bills(bill_id) ON DELETE CASCADE,
    ord            INTEGER NOT NULL,
    action_date    TEXT NOT NULL,
    action_text    TEXT NOT NULL,
    action_code    TEXT NOT NULL,
    source_system  TEXT NOT NULL,
    PRIMARY KEY (bill_id, ord)
);
CREATE INDEX IF NOT EXISTS idx_bill_actions_date
    ON bill_actions (action_date DESC);

CREATE TABLE IF NOT EXISTS bill_subjects (
    bill_id  TEXT NOT NULL REFERENCES bills(bill_id) ON DELETE CASCADE,
    subject  TEXT NOT NULL,
    PRIMARY KEY (bill_id, subject)
);

CREATE TABLE IF NOT EXISTS bill_titles (
    bill_id     TEXT NOT NULL REFERENCES bills(bill_id) ON DELETE CASCADE,
    ord         INTEGER NOT NULL,
    title_type  TEXT NOT NULL,
    title_text  TEXT NOT NULL,
    chamber     TEXT,
    PRIMARY KEY (bill_id, ord)
);

CREATE TABLE IF NOT EXISTS bill_summaries (
    bill_id        TEXT NOT NULL REFERENCES bills(bill_id) ON DELETE CASCADE,
    version_code   TEXT NOT NULL,
    action_date    TEXT NOT NULL,
    action_desc    TEXT NOT NULL,
    summary_text   TEXT NOT NULL,
    PRIMARY KEY (bill_id, version_code)
);

CREATE VIRTUAL TABLE IF NOT EXISTS bills_fts USING fts5(
    bill_id UNINDEXED,
    identifier,
    title,
    policy_area,
    short_title,
    subjects,
    tokenize = 'porter'
);

-- bill_briefs is a RECORD table, not a mirror (ADR 0019). It holds the
-- LLM-generated executive summary of a Bill (ADR 0020): derived from the
-- data but non-deterministic and not rebuildable from JSONL, so a mirror
-- re-derivation (re-run load/index) must NOT recreate or destroy it. The
-- loaders only UPSERT mirror rows and ensure_schema is CREATE IF NOT
-- EXISTS, so this row survives a rebuild. Keyed by (bill_id, lens): an
-- empty lens is the neutral, cacheable default; a non-empty lens is a
-- user-conditioned brief. facts_hash pins the brief to the fact-pack it
-- was generated from (+ model + prompt_version) so the web layer can flag
-- a brief stale when the underlying mirror data moves.
CREATE TABLE IF NOT EXISTS bill_briefs (
    bill_id            TEXT NOT NULL REFERENCES bills(bill_id) ON DELETE CASCADE,
    lens               TEXT NOT NULL DEFAULT '',
    executive_summary  TEXT NOT NULL,
    facts_hash         TEXT NOT NULL,
    model              TEXT NOT NULL,
    prompt_version     INTEGER NOT NULL,
    generated_at       TEXT NOT NULL,
    PRIMARY KEY (bill_id, lens)
);
"""

_BILL_COLUMNS: tuple[str, ...] = (
    "bill_id",
    "congress",
    "bill_type",
    "bill_number",
    "origin_chamber",
    "title",
    "introduced_date",
    "policy_area",
    "sponsor_bioguide_id",
    "latest_action_date",
    "latest_action_text",
    "update_date",
    "fetched_at",
)

#: Cap on placeholders per ``bill_id IN (...)`` lookup. SQLite defaults to
#: 32766 in modern builds but historically 999; keeping the chunk well
#: below either limit avoids the compile-time complaints on older
#: distros without measurably hurting throughput.
_BILL_IDS_PRESENT_CHUNK = 500

#: The tier-2 sections enriched after the tier-1 bills upsert. One name per
#: ``*_fetched_at`` column / ``replace_*`` writer.
BILL_TIER2_SECTIONS: tuple[str, ...] = (
    "cosponsors",
    "actions",
    "subjects",
    "titles",
    "summaries",
)

# UPSERT on the parent row. The five *_fetched_at columns are *not* in
# _BILL_COLUMNS — they're owned by the tier-2 loaders' per-section UPDATEs,
# and clobbering them on every tier-1 upsert would reset the "enriched"
# state every time `concord load bills` runs.
_BILL_UPSERT_SQL = upsert_sql("bills", _BILL_COLUMNS, conflict=("bill_id",))

# Per-child column tuples — used to generate INSERT SQL and to project
# pydantic models into row tuples.
_COSPONSOR_COLUMNS: tuple[str, ...] = (
    "bill_id",
    "bioguide_id",
    "sponsorship_date",
    "sponsorship_withdrawn_date",
    "is_original_cosponsor",
)

_ACTION_COLUMNS: tuple[str, ...] = (
    "bill_id",
    "ord",
    "action_date",
    "action_text",
    "action_code",
    "source_system",
)

_SUBJECT_COLUMNS: tuple[str, ...] = ("bill_id", "subject")

_TITLE_COLUMNS: tuple[str, ...] = (
    "bill_id",
    "ord",
    "title_type",
    "title_text",
    "chamber",
)

_SUMMARY_COLUMNS: tuple[str, ...] = (
    "bill_id",
    "version_code",
    "action_date",
    "action_desc",
    "summary_text",
)

_COSPONSOR_INSERT_SQL = insert_sql("bill_cosponsors", _COSPONSOR_COLUMNS)
_ACTION_INSERT_SQL = insert_sql("bill_actions", _ACTION_COLUMNS)
_SUBJECT_INSERT_SQL = insert_sql("bill_subjects", _SUBJECT_COLUMNS)
_TITLE_INSERT_SQL = insert_sql("bill_titles", _TITLE_COLUMNS)
_SUMMARY_INSERT_SQL = insert_sql("bill_summaries", _SUMMARY_COLUMNS)

# bill_briefs is a record table (ADR 0019); these power upsert_bill_brief.
# The (bill_id, lens) pair is the conflict target.
_BILL_BRIEF_COLUMNS: tuple[str, ...] = (
    "bill_id",
    "lens",
    "executive_summary",
    "facts_hash",
    "model",
    "prompt_version",
    "generated_at",
)

_BILL_BRIEF_UPSERT_SQL = upsert_sql(
    "bill_briefs", _BILL_BRIEF_COLUMNS, conflict=("bill_id", "lens")
)


def m001_add_bill_last_enrichment_error(conn: sqlite3.Connection) -> None:
    """ADR 0016: ``bills.last_enrichment_error TEXT NULL``.

    Idempotent against DBs whose ``_BASE_SCHEMA`` already declares the
    column (fresh installs after ADR 0017 landed) and against DBs that
    were touched by the pre-ADR-0017 ``_POST_RELEASE_COLUMNS`` code
    path (any 0.2.x boot prior to 0017). ``PRAGMA table_info`` reports
    the existing column set; if the column is present we skip the
    ALTER and the runner still bumps ``user_version``.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(bills)")}
    if "last_enrichment_error" not in existing:
        conn.execute("ALTER TABLE bills ADD COLUMN last_enrichment_error TEXT")


def m002_add_bill_briefs(conn: sqlite3.Connection) -> None:
    """ADR 0019/0020: the ``bill_briefs`` record table.

    ``CREATE TABLE IF NOT EXISTS`` is idempotent against fresh installs
    (whose ``_BASE_SCHEMA`` already declares the table; this ALTER is a
    no-op) and against pre-0020 DBs (which gain the table here). The DDL
    must stay byte-equivalent to the ``_BASE_SCHEMA`` declaration — the
    schema-equivalence test (ADR 0017) fails if they drift.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS bill_briefs (
            bill_id            TEXT NOT NULL REFERENCES bills(bill_id) ON DELETE CASCADE,
            lens               TEXT NOT NULL DEFAULT '',
            executive_summary  TEXT NOT NULL,
            facts_hash         TEXT NOT NULL,
            model              TEXT NOT NULL,
            prompt_version     INTEGER NOT NULL,
            generated_at       TEXT NOT NULL,
            PRIMARY KEY (bill_id, lens)
        );
        """
    )


def m006_bill_children_not_null(conn: sqlite3.Connection) -> None:
    """ADR 0024: tighten the bill child tables' columns to ``NOT NULL``.

    Guarded table rebuilds — no-ops on fresh installs whose ``_BASE_SCHEMA``
    already declares the constraints. Covers ``bill_cosponsors.sponsorship_date``,
    ``bill_actions.{action_code,source_system}`` and
    ``bill_summaries.{action_date,action_desc}``. Legacy rows holding a ``NULL`` in
    any tightened column are dropped (derived state, rebuildable per ADR 0002);
    each rebuild preserves the FK to ``bills``.
    """
    for table, columns in (
        ("bill_cosponsors", ("sponsorship_date",)),
        ("bill_actions", ("action_code", "source_system")),
        ("bill_summaries", ("action_date", "action_desc")),
    ):
        rebuild_table_add_not_null(conn, table=table, not_null_columns=columns)


def upsert_bill(conn: sqlite3.Connection, bill: BillDetail, *, fetched_at: str) -> None:
    """UPSERT one Bill row keyed on ``bill_id`` and commit.

    Latest snapshot wins per ADR 0006; the loader is responsible for feeding
    only the latest snapshot per natural key. Owns its own commit — a
    single-bill tier-1 write is durable immediately.
    """
    conn.execute(_BILL_UPSERT_SQL, _row_from_bill(bill, fetched_at=fetched_at))
    conn.commit()


def get_bill(conn: sqlite3.Connection, bill_id: str) -> sqlite3.Row | None:
    """Return the ``bills`` row for ``bill_id``, or ``None`` if absent."""
    cursor = conn.execute("SELECT * FROM bills WHERE bill_id = ?", (bill_id,))
    row: sqlite3.Row | None = cursor.fetchone()
    return row


def bill_ids_present(conn: sqlite3.Connection, bill_ids: Sequence[str]) -> set[str]:
    """Return the subset of ``bill_ids`` that already have a row in ``bills``.

    Used by the tier-2 loader to filter out orphan rows in one query instead
    of an N+1 ``get_bill`` loop. Empty input returns an empty set without
    touching the DB. The query is chunked at :data:`_BILL_IDS_PRESENT_CHUNK`
    placeholders to stay under SQLite's compile-time variable cap.
    """
    if not bill_ids:
        return set()
    present: set[str] = set()
    ids = list(bill_ids)
    for start in range(0, len(ids), _BILL_IDS_PRESENT_CHUNK):
        chunk = ids[start : start + _BILL_IDS_PRESENT_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        cursor = conn.execute(
            f"SELECT bill_id FROM bills WHERE bill_id IN ({placeholders})",  # noqa: S608 - placeholders only
            chunk,
        )
        present.update(row["bill_id"] for row in cursor)
    return present


def replace_cosponsors(
    conn: sqlite3.Connection,
    bill_id: str,
    cosponsors: Sequence[BillCosponsor],
    *,
    fetched_at: str,
) -> None:
    """DELETE-then-INSERT every Cosponsor for one bill_id; stamp the fetched_at column.

    Idempotent: re-running with the same set produces the same final state.
    Safe to call for a Bill not in the ``bills`` table — the FK will block the
    INSERT before any rows are written; callers should filter out unknown
    ``bill_id`` before invoking.
    """
    conn.execute("DELETE FROM bill_cosponsors WHERE bill_id = ?", (bill_id,))
    if cosponsors:
        conn.executemany(
            _COSPONSOR_INSERT_SQL,
            [
                (
                    bill_id,
                    c.bioguide_id,
                    c.sponsorship_date,
                    c.sponsorship_withdrawn_date,
                    1 if c.is_original_cosponsor else 0,
                )
                for c in cosponsors
            ],
        )
    conn.execute(
        "UPDATE bills SET cosponsors_fetched_at = ? WHERE bill_id = ?",
        (fetched_at, bill_id),
    )


def replace_actions(
    conn: sqlite3.Connection,
    bill_id: str,
    actions: Sequence[BillAction],
    *,
    fetched_at: str,
) -> None:
    """DELETE-then-INSERT every BillAction for one bill_id; stamp the column."""
    conn.execute("DELETE FROM bill_actions WHERE bill_id = ?", (bill_id,))
    if actions:
        conn.executemany(
            _ACTION_INSERT_SQL,
            [
                (
                    bill_id,
                    i,
                    a.action_date,
                    a.action_text,
                    a.action_code,
                    a.source_system,
                )
                for i, a in enumerate(actions)
            ],
        )
    conn.execute(
        "UPDATE bills SET actions_fetched_at = ? WHERE bill_id = ?",
        (fetched_at, bill_id),
    )


def replace_subjects(
    conn: sqlite3.Connection,
    bill_id: str,
    subjects: Sequence[BillSubject],
    *,
    fetched_at: str,
) -> None:
    """DELETE-then-INSERT every BillSubject for one bill_id; stamp the column.

    Duplicate ``name`` values in the input are dedup'd before INSERT so the
    per-row PK (``bill_id, subject``) doesn't trip.
    """
    seen: set[str] = set()
    deduped: list[BillSubject] = []
    for s in subjects:
        if s.name in seen:
            continue
        seen.add(s.name)
        deduped.append(s)
    conn.execute("DELETE FROM bill_subjects WHERE bill_id = ?", (bill_id,))
    if deduped:
        conn.executemany(
            _SUBJECT_INSERT_SQL,
            [(bill_id, s.name) for s in deduped],
        )
    conn.execute(
        "UPDATE bills SET subjects_fetched_at = ? WHERE bill_id = ?",
        (fetched_at, bill_id),
    )


def replace_titles(
    conn: sqlite3.Connection,
    bill_id: str,
    titles: Sequence[BillTitle],
    *,
    fetched_at: str,
) -> None:
    """DELETE-then-INSERT every BillTitle for one bill_id; stamp the column."""
    conn.execute("DELETE FROM bill_titles WHERE bill_id = ?", (bill_id,))
    if titles:
        conn.executemany(
            _TITLE_INSERT_SQL,
            [(bill_id, i, t.title_type, t.title_text, t.chamber) for i, t in enumerate(titles)],
        )
    conn.execute(
        "UPDATE bills SET titles_fetched_at = ? WHERE bill_id = ?",
        (fetched_at, bill_id),
    )


def replace_summaries(
    conn: sqlite3.Connection,
    bill_id: str,
    summaries: Sequence[BillSummary],
    *,
    fetched_at: str,
) -> None:
    """DELETE-then-INSERT every BillSummary for one bill_id; stamp the column.

    Duplicate ``version_code`` values in the input are dedup'd; the latest by
    list order wins.
    """
    latest_per_version: dict[str, BillSummary] = {}
    for s in summaries:
        latest_per_version[s.version_code] = s
    conn.execute("DELETE FROM bill_summaries WHERE bill_id = ?", (bill_id,))
    if latest_per_version:
        conn.executemany(
            _SUMMARY_INSERT_SQL,
            [
                (
                    bill_id,
                    s.version_code,
                    s.action_date,
                    s.action_desc,
                    s.summary_text,
                )
                for s in latest_per_version.values()
            ],
        )
    conn.execute(
        "UPDATE bills SET summaries_fetched_at = ? WHERE bill_id = ?",
        (fetched_at, bill_id),
    )


def set_enrichment_error(conn: sqlite3.Connection, bill_id: str, error: str) -> None:
    """Record an enrichment-attempt error on the bills row."""
    conn.execute(
        "UPDATE bills SET last_enrichment_error = ? WHERE bill_id = ?",
        (error, bill_id),
    )


def clear_enrichment_error(conn: sqlite3.Connection, bill_id: str) -> None:
    """Clear any previously-recorded enrichment-attempt error."""
    conn.execute(
        "UPDATE bills SET last_enrichment_error = NULL WHERE bill_id = ?",
        (bill_id,),
    )


def cosponsors_for_bill(conn: sqlite3.Connection, bill_id: str) -> list[sqlite3.Row]:
    """Return cosponsor rows for ``bill_id``, original cosponsors first."""
    cursor = conn.execute(
        "SELECT * FROM bill_cosponsors WHERE bill_id = ? "
        "ORDER BY is_original_cosponsor DESC, sponsorship_date ASC, bioguide_id ASC",
        (bill_id,),
    )
    return cursor.fetchall()


def actions_for_bill(conn: sqlite3.Connection, bill_id: str) -> list[sqlite3.Row]:
    """Return action rows for ``bill_id``, newest-first (scrape order breaks date ties)."""
    # Sort newest-first by date; fall back to scrape-order ord for same-date
    # ties. Sorting in SQL (not relying on the API's newest-first order) keeps
    # the UI's reverse-chronological rendering correct if upstream order flips.
    cursor = conn.execute(
        "SELECT * FROM bill_actions WHERE bill_id = ? "
        "ORDER BY (action_date IS NULL), action_date DESC, ord ASC",
        (bill_id,),
    )
    return cursor.fetchall()


def subjects_for_bill(conn: sqlite3.Connection, bill_id: str) -> list[sqlite3.Row]:
    """Return subject rows for ``bill_id``, alphabetical."""
    cursor = conn.execute(
        "SELECT * FROM bill_subjects WHERE bill_id = ? ORDER BY subject ASC",
        (bill_id,),
    )
    return cursor.fetchall()


def titles_for_bill(conn: sqlite3.Connection, bill_id: str) -> list[sqlite3.Row]:
    """Return title rows for ``bill_id``, in scrape order."""
    cursor = conn.execute(
        "SELECT * FROM bill_titles WHERE bill_id = ? ORDER BY ord ASC",
        (bill_id,),
    )
    return cursor.fetchall()


def summaries_for_bill(conn: sqlite3.Connection, bill_id: str) -> list[sqlite3.Row]:
    """Return summary rows for ``bill_id``, oldest action first."""
    cursor = conn.execute(
        "SELECT * FROM bill_summaries WHERE bill_id = ? ORDER BY action_date ASC",
        (bill_id,),
    )
    return cursor.fetchall()


def upsert_bill_brief(
    conn: sqlite3.Connection,
    *,
    bill_id: str,
    lens: str,
    executive_summary: str,
    facts_hash: str,
    model: str,
    prompt_version: int,
    generated_at: str,
) -> None:
    """Insert or replace the cached brief for ``(bill_id, lens)``.

    A record-table write (ADR 0019): the brief is generated state, not a
    mirror projection. Safe to call for a Bill not in ``bills`` only if the FK
    target exists — callers generate from a loaded bill, so the parent row is
    present.
    """
    conn.execute(
        _BILL_BRIEF_UPSERT_SQL,
        (bill_id, lens, executive_summary, facts_hash, model, prompt_version, generated_at),
    )


def _row_from_bill(bill: BillDetail, *, fetched_at: str) -> tuple[Any, ...]:
    """Project a :class:`BillDetail` into the column tuple expected by SQL."""
    dumped: dict[str, Any] = bill.model_dump(mode="json")
    dumped["fetched_at"] = fetched_at
    return tuple(dumped[col] for col in _BILL_COLUMNS)
