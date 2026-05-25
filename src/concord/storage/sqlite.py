"""SQLite storage — the recommended derived store.

One file on disk, one ``proceedings`` table that mirrors the
:class:`Proceeding` model 1:1. Dedup is enforced by SQL: ``granule_id`` is
the primary key, and writes use ``INSERT OR IGNORE`` so re-running over an
unchanged JSONL is a no-op.

WAL mode is enabled on construction so the web layer can read concurrently
while the pipeline writes. The class isn't thread-safe — the underlying
``sqlite3.Connection`` is created with ``check_same_thread=True``, which is
the right default for a one-process loader.

Stage 2 (chunks + FTS5 + ``sqlite-vec``) extends this schema in
:doc:`docs/plans/stage-2-index`; Stage 1 only creates the ``proceedings``
table and its secondary indexes.
"""

import sqlite3
from pathlib import Path
from types import TracebackType
from typing import Any

from ..models import Proceeding

# Columns in the exact order they appear in the INSERT statement. Keeping
# this list in one place makes it easy to add a column later: extend here,
# extend _row_from_proceeding, extend the DDL.
_COLUMNS: tuple[str, ...] = (
    "granule_id",
    "issue_date",
    "congress",
    "session",
    "volume",
    "issue_number",
    "update_date",
    "section",
    "title",
    "start_page",
    "end_page",
    "text_url",
    "pdf_url",
    "text",
    "fetched_at",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS proceedings (
    granule_id   TEXT PRIMARY KEY,
    issue_date   TEXT NOT NULL,
    congress     INTEGER NOT NULL,
    session      INTEGER NOT NULL,
    volume       INTEGER NOT NULL,
    issue_number INTEGER NOT NULL,
    update_date  TEXT NOT NULL,
    section      TEXT NOT NULL,
    title        TEXT NOT NULL,
    start_page   TEXT NOT NULL,
    end_page     TEXT NOT NULL,
    text_url     TEXT NOT NULL,
    pdf_url      TEXT NOT NULL,
    text         TEXT NOT NULL,
    fetched_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_proceedings_issue_date
    ON proceedings (issue_date);

CREATE INDEX IF NOT EXISTS idx_proceedings_congress
    ON proceedings (congress);
"""

_INSERT_SQL = (
    "INSERT OR IGNORE INTO proceedings (" + ", ".join(_COLUMNS) + ") "
    "VALUES (" + ", ".join("?" for _ in _COLUMNS) + ")"
)


class SqliteStorage:
    """SQLite-backed :class:`Storage` implementation.

    Parameters
    ----------
    path:
        Filesystem path for the ``.db`` file. Parent directories are
        created on first use.

    The class opens one long-lived :class:`sqlite3.Connection` on
    construction. WAL mode is enabled so other readers (eventually the web
    layer) can run concurrently with writes. Not thread-safe; instantiate
    one per writer.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        # WAL gives us concurrent reads alongside our single writer.
        self._conn.execute("PRAGMA journal_mode = WAL")
        # Foreign keys aren't used yet but Stage 2's chunks table will need
        # them on; enabling here is cheap and saves a footgun later.
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- Storage Protocol -------------------------------------------------

    def has(self, granule_id: str) -> bool:
        cursor = self._conn.execute(
            "SELECT 1 FROM proceedings WHERE granule_id = ? LIMIT 1",
            (granule_id,),
        )
        return cursor.fetchone() is not None

    def write(self, proceeding: Proceeding) -> None:
        self._conn.execute(_INSERT_SQL, _row_from_proceeding(proceeding))
        self._conn.commit()

    # -- introspection ----------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    def __len__(self) -> int:
        """Total number of distinct Proceedings stored."""
        cursor = self._conn.execute("SELECT COUNT(*) FROM proceedings")
        (count,) = cursor.fetchone()
        return int(count)

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SqliteStorage":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


# -- serialization ---------------------------------------------------------


def _row_from_proceeding(proceeding: Proceeding) -> tuple[Any, ...]:
    """Project a :class:`Proceeding` into the column tuple expected by SQL.

    Uses ``model_dump(mode="json")`` so dates, datetimes, and HttpUrl
    values come out as ISO/string forms that SQLite stores in TEXT columns
    without surprise. Order must match :data:`_COLUMNS`.
    """
    dumped: dict[str, Any] = proceeding.model_dump(mode="json")
    return tuple(dumped[col] for col in _COLUMNS)
