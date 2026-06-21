"""The daemon's backfill watermark — ``daemon_state.json`` (ADR 0026).

This is **Concord-originated operational state**, the same category as the
Scrape Run ledger (ADR 0019 / 0021): not rebuildable from upstream, not a
mirror of anything. It is stored as JSON next to ``runs.jsonl`` in ``data/``
rather than a SQLite record table so it needs no schema migration (ADR 0017)
— the daemon is its only reader and writer, and the web layer never queries
it. See ADR 0026 for the full rationale.

The file records, per entity, which Congresses have been fully backfilled,
and a single cursor for Proceedings' date-window walk. Completion markers are
written only *after* the corresponding child process exits 0 (see
:mod:`concord.daemon.loop`), so a crash mid-Tick loses at most the current
chunk, which the next Tick re-attempts idempotently.
"""

import json
import logging
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field

_log = logging.getLogger("concord.daemon.state")

#: Bumped if the on-disk shape changes incompatibly. Read code tolerates an
#: absent file (fresh deployment) by starting from defaults.
STATE_VERSION = 1

#: File name within the data dir.
STATE_FILENAME = "daemon_state.json"


class DaemonState(BaseModel):
    """Durable backfill progress for one deployment.

    ``congress_backfilled`` maps an entity name (``"members"`` / ``"bills"`` /
    ``"votes"``) to the sorted set of Congresses whose cold backfill has
    completed. ``proceedings_oldest_scraped`` is the oldest date the
    Proceedings backfill has reached so far (``None`` before the first
    backfill chunk); the next chunk scrapes the window ending the day before
    it. See ADR 0026.
    """

    version: int = STATE_VERSION
    congress_backfilled: dict[str, list[int]] = Field(default_factory=dict)
    proceedings_oldest_scraped: date | None = None

    def is_congress_done(self, entity: str, congress: int) -> bool:
        """Return whether ``congress`` is recorded backfilled for ``entity``."""
        return congress in self.congress_backfilled.get(entity, [])

    def mark_congress_done(self, entity: str, congress: int) -> None:
        """Record ``congress`` as backfilled for ``entity`` (idempotent, kept sorted)."""
        done = set(self.congress_backfilled.get(entity, []))
        done.add(congress)
        self.congress_backfilled[entity] = sorted(done)


def state_path(data_dir: Path) -> Path:
    """Return the ``daemon_state.json`` path inside ``data_dir``."""
    return data_dir / STATE_FILENAME


def load_state(data_dir: Path) -> DaemonState:
    """Load the watermark, or return a fresh default if absent/unreadable.

    A missing file is the normal cold-start case and yields defaults silently.
    A present-but-corrupt file is logged and also falls back to defaults: the
    daemon must not wedge on a bad state file, and re-deriving progress by
    re-running idempotent chunks is safe (ADR 0026).
    """
    path = state_path(data_dir)
    if not path.exists():
        return DaemonState()
    try:
        return DaemonState.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        _log.warning("ignoring unreadable %s (%s); starting from defaults", path, exc)
        return DaemonState()


def save_state(data_dir: Path, state: DaemonState) -> None:
    """Persist ``state`` to ``daemon_state.json`` atomically.

    Writes a sibling temp file and ``os.replace``s it into place so a crash
    mid-write can't leave a truncated, unparseable watermark.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    path = state_path(data_dir)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
