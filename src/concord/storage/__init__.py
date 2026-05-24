"""Storage backends for Concord.

The :class:`Storage` Protocol is the contract every backend implements.

- :class:`JsonlStorage` — the default, a single append-only ``.jsonl`` file
  with no infrastructure requirements.
- :class:`MongoStorage` — optional, behind the ``[mongo]`` extra. Imported
  lazily so the package works without :mod:`pymongo` installed.
"""

from .base import Storage
from .jsonl import JsonlStorage
from .mongo import MongoStorage

__all__ = ["JsonlStorage", "MongoStorage", "Storage"]
