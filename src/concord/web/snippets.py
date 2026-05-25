"""Display snippets for search results.

Two flavors, both safe to render directly into HTML:

- :func:`keyword_snippet` — uses FTS5's built-in ``snippet()`` to pull
  ~32 tokens of context around matched terms with ``<mark>`` highlights.
- :func:`semantic_snippet` — truncates a chunk around its middle for
  results that matched only by vector similarity.

Both escape all output except a fixed allowlist (``<mark>`` and ``</mark>``)
so user-controlled or chunk-controlled text can't smuggle markup.
"""

import html
import re
import sqlite3

#: Default snippet length for the semantic path, in characters.
DEFAULT_SEMANTIC_LEN = 240

_WHITESPACE_RUN = re.compile(r"\s+")
# SQLite drops null bytes in TEXT, so the placeholders have to be printable.
# These tokens are chosen to (a) survive html.escape unchanged and (b) be
# unlikely to appear in real Congressional Record text.
_MARK_OPEN_PLACEHOLDER = "__CONCORD_MARK_BEGIN__"
_MARK_CLOSE_PLACEHOLDER = "__CONCORD_MARK_END__"


def keyword_snippet(db: sqlite3.Connection, chunk_id: int, query: str) -> str:
    """Build a highlighted snippet for an FTS5-matched chunk.

    Uses FTS5's ``snippet()`` function with sentinel placeholders so we
    can escape the body before re-introducing the allowed ``<mark>``
    tags. Returns a ready-to-render HTML string.
    """
    safe_query = '"' + query.replace('"', '""') + '"'
    row = db.execute(
        "SELECT snippet(chunks_fts, 0, ?, ?, '…', 32) "
        "FROM chunks_fts WHERE chunks_fts MATCH ? AND rowid = ?",
        (_MARK_OPEN_PLACEHOLDER, _MARK_CLOSE_PLACEHOLDER, safe_query, chunk_id),
    ).fetchone()
    if row is None or row[0] is None:
        return ""
    return _finalize(row[0])


def semantic_snippet(chunk_text: str, *, length: int = DEFAULT_SEMANTIC_LEN) -> str:
    """Truncate a chunk to ``length`` chars around the middle for display.

    No highlights — semantic matches don't have specific terms to mark.
    HTML-escapes the result.
    """
    cleaned = _WHITESPACE_RUN.sub(" ", chunk_text).strip()
    if len(cleaned) <= length:
        return html.escape(cleaned)
    half = length // 2
    middle = len(cleaned) // 2
    start = max(0, middle - half)
    end = min(len(cleaned), start + length)
    body = cleaned[start:end]
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(cleaned) else ""
    return prefix + html.escape(body) + suffix


# -- internals --------------------------------------------------------------


def _finalize(raw: str) -> str:
    """Collapse whitespace, HTML-escape, then restore the ``<mark>`` allowlist."""
    cleaned = _WHITESPACE_RUN.sub(" ", raw).strip()
    escaped = html.escape(cleaned)
    return escaped.replace(_MARK_OPEN_PLACEHOLDER, "<mark>").replace(
        _MARK_CLOSE_PLACEHOLDER, "</mark>"
    )
