"""Shared INSERT / UPSERT statement builders for the storage layer.

Tiny, dependency-free helpers used by the per-domain storage modules
(``runs``, ``members``, ``votes``, ``bills``) and ``sqlite`` itself to build
parameterized statements from a column tuple. They live here — rather than in
``sqlite`` — because ``sqlite`` imports the domain modules, so the domain
modules cannot import back from ``sqlite`` without a cycle. One canonical
builder also keeps every table's INSERT/UPSERT byte-for-byte consistent.

Both helpers interpolate only static, code-defined table/column names (never
user data), so the ``# noqa: S608`` on the SQL string is the correct,
intentional suppression.
"""


def insert_sql(table: str, columns: tuple[str, ...]) -> str:
    """Build ``INSERT INTO {table} (...) VALUES (?, ...)`` for ``columns``."""
    return (
        f"INSERT INTO {table} ("  # noqa: S608 - static table/column names
        + ", ".join(columns)
        + ") VALUES ("
        + ", ".join("?" for _ in columns)
        + ")"
    )


def upsert_sql(table: str, columns: tuple[str, ...], *, conflict: tuple[str, ...]) -> str:
    """Build an UPSERT: insert ``columns``; on ``conflict`` overwrite the rest.

    Every column not in ``conflict`` is set from ``excluded`` — the standard
    "latest snapshot wins" mirror write (ADR 0006). ``conflict`` doubles as the
    ``ON CONFLICT`` target and the do-not-overwrite key set.
    """
    updates = ", ".join(f"{col} = excluded.{col}" for col in columns if col not in conflict)
    return (
        insert_sql(table, columns) + f" ON CONFLICT({', '.join(conflict)}) DO UPDATE SET " + updates
    )
