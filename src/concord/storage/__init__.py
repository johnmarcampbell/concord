"""Storage backends for Concord.

The :class:`Storage` Protocol is the contract every backend implements.
:class:`JsonlStorage` is the default — a single append-only ``.jsonl`` file
with no infrastructure requirements.
"""

from .base import Storage
from .jsonl import JsonlStorage

__all__ = ["JsonlStorage", "Storage"]
