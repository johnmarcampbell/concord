"""Storage Protocol shared by every backend.

A backend is anything that can answer ``has(granule_id)`` and accept
``write(proceeding)``. Both methods are idempotent: re-writing a Proceeding
that has already been stored is a no-op, so the orchestrator can loop blindly
over articles without ever checking — though it's still expected to call
``has`` first to skip the text-fetch HTTP for already-stored articles.
"""

from typing import Protocol

from concord.models.proceedings import Proceeding


class Storage(Protocol):
    """A persistence target for :class:`Proceeding` records.

    Implementations must guarantee:

    - ``has(granule_id)`` returns ``True`` if a Proceeding with that granule
      ID has been written by this or any previous process, ``False`` otherwise.
    - ``write(proceeding)`` persists the Proceeding. Writing a Proceeding
      whose ``granule_id`` is already stored is a no-op (idempotent).
    """

    def has(self, granule_id: str) -> bool: ...

    def write(self, proceeding: Proceeding) -> None: ...
