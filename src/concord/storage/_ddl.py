"""Schema-rebuild helpers for migrations (ADR 0017 / ADR 0024).

SQLite has no ``ALTER COLUMN … SET NOT NULL``, so a migration that tightens a
column to ``NOT NULL`` must rebuild the table: copy the rows into a new table
whose DDL carries the constraint, drop the original, and rename the copy back.

This leaf lives beside :mod:`concord.storage._sql` — *not* in
:mod:`concord.storage.sqlite` — so the per-domain storage modules can import it
from their migration callables without the import cycle ``sqlite`` → domain →
``sqlite`` (``sqlite`` imports the domain modules, never the reverse).

The new table's DDL is produced by injecting ``NOT NULL`` into the *live*
``CREATE`` statement read from ``sqlite_master``, never hand-written. That keeps
CHECK / FK / DEFAULT / PRIMARY KEY clauses byte-identical by construction — the
schema-equivalence fingerprint (ADR 0017) compares ``PRAGMA table_info`` +
``index_list``, which capture *neither* CHECK nor FK, so structural preservation
must not depend on a hand-copied DDL staying in sync. See ADR 0024.

The helper assumes the unquoted-identifier DDL this project writes (no
``"quoted"`` table or column names); it raises rather than silently mis-edit if
it cannot locate the table declaration or a column definition.
"""

import re
import sqlite3

# SQLite's storage-class keywords, enough to anchor the end of a column's type
# token so ``NOT NULL`` is appended right after it.
_TYPE = r"(?:TEXT|INTEGER|REAL|BLOB|NUMERIC)"


def _swap_table_name(create_sql: str, old: str, new: str) -> str:
    """Rename only the table in a ``CREATE TABLE`` statement's declaration."""
    pattern = re.compile(
        r"(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)" + re.escape(old) + r"\b",
        re.IGNORECASE,
    )
    swapped, n = pattern.subn(rf"\g<1>{new}", create_sql, count=1)
    if n != 1:
        raise ValueError(f"could not locate the table declaration for {old!r} in its DDL")
    return swapped


def _inject_not_null(create_sql: str, column: str) -> str:
    """Append ``NOT NULL`` to ``column``'s definition (after its type token)."""
    pattern = re.compile(r"(\b" + re.escape(column) + r"\b\s+" + _TYPE + r")\b", re.IGNORECASE)
    injected, n = pattern.subn(r"\1 NOT NULL", create_sql, count=1)
    if n != 1:
        raise ValueError(f"expected exactly one definition of column {column!r}, found {n}")
    return injected


def rebuild_table_add_not_null(
    conn: sqlite3.Connection,
    *,
    table: str,
    not_null_columns: tuple[str, ...],
) -> int:
    """Rebuild ``table`` so ``not_null_columns`` become ``NOT NULL``; return rows dropped.

    Idempotent on its precondition (ADR 0017): when every target column is
    already ``NOT NULL`` — the fresh-install case, where ``_BASE_SCHEMA`` already
    declares them — this is a no-op returning ``0``. Otherwise the table is
    rebuilt and any row holding a ``NULL`` in a target column is dropped (derived
    state, rebuildable from JSONL per ADR 0002); the dropped-row count is returned
    so the caller can log it.

    Runs inside the migration runner's transaction with ``foreign_keys`` left ON.
    That is safe because none of the tightened tables is a foreign-key *target* —
    their only FKs are outgoing, re-checked against still-present parents during
    the copy — so the 12-step rebuild needs no ``PRAGMA foreign_keys`` toggle
    (which would be a no-op inside a transaction anyway). The leading
    ``DROP TABLE IF EXISTS`` clears any orphan temp table left by a prior failed,
    un-versioned attempt.
    """
    info = list(conn.execute(f"PRAGMA table_info({table})"))
    notnull_by_name: dict[str, int] = {str(row[1]): int(row[3]) for row in info}
    missing = [c for c in not_null_columns if c not in notnull_by_name]
    if missing:
        raise ValueError(f"{table!r} has no column(s): {missing}")
    if all(notnull_by_name[c] == 1 for c in not_null_columns):
        return 0

    create_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    if create_row is None:
        raise ValueError(f"table {table!r} not found")
    create_sql = str(create_row[0])
    index_sqls: list[str] = [
        str(r[0])
        for r in conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL",
            (table,),
        )
    ]

    tmp = f"{table}__new"
    new_sql = _swap_table_name(create_sql, table, tmp)
    for col in not_null_columns:
        if notnull_by_name[col] != 1:
            new_sql = _inject_not_null(new_sql, col)

    columns = ", ".join(str(row[1]) for row in info)
    not_null_filter = " AND ".join(f"{c} IS NOT NULL" for c in not_null_columns)
    count_sql = f"SELECT COUNT(*) FROM {table}"  # noqa: S608 - table identifier, never user input
    insert_sql = (
        f"INSERT INTO {tmp} ({columns}) "  # noqa: S608 - schema identifiers, never user input
        f"SELECT {columns} FROM {table} WHERE {not_null_filter}"
    )

    rows_before = int(conn.execute(count_sql).fetchone()[0])
    conn.execute(f"DROP TABLE IF EXISTS {tmp}")
    conn.execute(new_sql)
    inserted = conn.execute(insert_sql).rowcount
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")
    for idx_sql in index_sqls:
        conn.execute(idx_sql)

    return rows_before - inserted
