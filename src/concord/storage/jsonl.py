"""JSONL file storage — the default backend.

One Proceeding per line, serialized via :meth:`Proceeding.model_dump_json`.
No external infrastructure required; the only state is a single file path.

The class scans the file on construction to populate an in-memory set of
already-stored ``granule_id``s. Lines that fail to parse (e.g. truncated
writes from a previous crash) are skipped quietly rather than crashing the
load — losing one record from a long backfill is better than losing the
ability to resume the whole thing.
"""

import json
from pathlib import Path

from concord.models import Proceeding


class JsonlStorage:
    """Append-only JSON-Lines storage for :class:`Proceeding` records.

    Parameters
    ----------
    path:
        Filesystem path for the ``.jsonl`` file. Created lazily on first
        write; missing parent directories are created automatically.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._seen: set[str] = self._load_seen_granule_ids()

    # -- Storage Protocol -------------------------------------------------

    def has(self, granule_id: str) -> bool:
        return granule_id in self._seen

    def write(self, proceeding: Proceeding) -> None:
        if proceeding.granule_id in self._seen:
            return  # idempotent: skip already-stored granule
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = proceeding.model_dump_json()
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        self._seen.add(proceeding.granule_id)

    # -- introspection ----------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    def __len__(self) -> int:
        """Number of distinct Proceedings currently stored."""
        return len(self._seen)

    # -- internals --------------------------------------------------------

    def _load_seen_granule_ids(self) -> set[str]:
        if not self._path.exists():
            return set()
        seen: set[str] = set()
        with self._path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    granule_id = record["granule_id"]
                except (json.JSONDecodeError, KeyError, TypeError):
                    # Crashed-mid-write or otherwise unparseable; skip without
                    # killing the whole load. The orchestrator will re-fetch
                    # this article on the next run since `has` returns False.
                    continue
                if isinstance(granule_id, str):
                    seen.add(granule_id)
        return seen
