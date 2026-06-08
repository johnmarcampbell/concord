"""Storage backends for Concord.

The :class:`Storage` Protocol is the contract every raw-store backend
implements.

- :class:`JsonlStorage` — the scraper's default write target. The canonical
  raw store per ADR-0002.
- :class:`SqliteStorage` — the recommended derived store per ADR-0003. Holds
  the ``proceedings`` table (Stage 1) and the chunks / FTS5 / vector indexes
  (Stage 2).

The Mongo backend was removed in ADR-0013; the ``Storage`` Protocol remains
as a seam for a future alternative raw-store implementation if one ever
proves useful.

Per ADR 0022 this package re-exports nothing — import from the defining
submodule (``from concord.storage.sqlite import SqliteStorage``).
"""
