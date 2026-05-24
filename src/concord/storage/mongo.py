"""MongoDB storage backend (optional).

Implements the :class:`Storage` Protocol on top of a single collection.
Each :class:`Proceeding` is one document; ``granule_id`` is the natural key
and is enforced by a unique index, so dedup is the database's job and the
backend's ``has()`` / ``write()`` methods are thin wrappers.

This module imports :mod:`pymongo` only inside :meth:`MongoStorage.from_uri`,
so the package can be imported even when pymongo isn't installed. Install
the optional dependency to use this backend::

    pip install concord[mongo]

For tests, pass any pymongo-compatible collection (e.g. from `mongomock
<https://github.com/mongomock/mongomock>`_) directly to the constructor —
:class:`MongoStorage` doesn't care whether it's the real driver or a fake.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..models import Proceeding

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass


class MongoStorage:
    """Single-collection MongoDB backend for :class:`Proceeding` records.

    Parameters
    ----------
    collection:
        A pymongo (or pymongo-compatible) ``Collection`` instance.

    The constructor declares a unique index on ``granule_id`` so concurrent
    writers can't insert duplicates even if their in-memory dedup sets
    disagree.
    """

    def __init__(self, collection: Any) -> None:
        self._collection = collection
        self._collection.create_index("granule_id", unique=True)

    # -- alternate constructors -------------------------------------------

    @classmethod
    def from_uri(
        cls,
        uri: str,
        *,
        db: str = "concord",
        collection: str = "proceedings",
    ) -> MongoStorage:
        """Build a :class:`MongoStorage` from a ``mongodb://`` connection URI.

        Lazily imports :mod:`pymongo` so the rest of the package stays
        importable without it. Raises :class:`ImportError` with a clear
        install hint if the optional dependency isn't installed.
        """
        try:
            from pymongo import MongoClient  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - exercised via install hint
            raise ImportError(
                "MongoDB support requires pymongo. Install with "
                "'pip install concord[mongo]' (or 'uv sync --extra mongo')."
            ) from exc

        client: Any = MongoClient(uri)
        return cls(collection=client[db][collection])

    # -- Storage Protocol -------------------------------------------------

    def has(self, granule_id: str) -> bool:
        return (
            self._collection.find_one({"granule_id": granule_id}, projection={"_id": 1}) is not None
        )

    def write(self, proceeding: Proceeding) -> None:
        # Use mode="json" so date / datetime / HttpUrl serialize to strings.
        # The Proceeding round-trips losslessly via Proceeding.model_validate
        # against the resulting document (granule_id is always present).
        document = proceeding.model_dump(mode="json")
        try:
            self._collection.insert_one(document)
        except Exception as exc:
            # We can't `import pymongo.errors.DuplicateKeyError` here without
            # making pymongo a hard dependency, so match by class name. Other
            # collection-level errors (write failures, network issues) propagate.
            if exc.__class__.__name__ == "DuplicateKeyError":
                return  # idempotent: already stored
            raise
