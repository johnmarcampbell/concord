"""Storage backends for Concord.

The :class:`Storage` Protocol is the contract every backend implements.

- :class:`JsonlStorage` — the scraper's default write target. The canonical
  raw store per ADR-0002.
- :class:`SqliteStorage` — the recommended derived store per ADR-0003. Holds
  the ``proceedings`` table (Stage 1) and, after Stage 2 lands, the chunks /
  FTS5 / vector indexes.
- :class:`MongoStorage` — optional, behind the ``[mongo]`` extra. Imported
  lazily so the package works without :mod:`pymongo` installed.
"""

from .base import Storage
from .jsonl import JsonlStorage
from .mongo import MongoStorage
from .sqlite import SqliteStorage, ensure_schema

__all__ = ["JsonlStorage", "MongoStorage", "SqliteStorage", "Storage", "ensure_schema"]
