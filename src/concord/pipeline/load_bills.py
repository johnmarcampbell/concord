"""Stage 1 — Bills loader.

Projects the ADR 0006 snapshot stream in ``<storage_dir>/bills.jsonl``
into the ``bills`` SQLite table. The natural key is the composite
``(congress, bill_type, bill_number)``; the loader keeps the latest
snapshot per key and UPSERTs one row each.

Phase 2b adds five sibling tier-2 JSONL files (per ADR 0009) — the
loader reads each when present and projects to its child table,
stamping the corresponding ``bills.<section>_fetched_at`` column. A
Bill present in tier-2 snapshots but absent from ``bills.jsonl`` is
counted as a tier-2 orphan and skipped (it would violate the FK).

Re-running the loader over an unchanged JSONL is a no-op. Re-running
over a JSONL that gained new snapshots converges the SQL projection to
the latest-snapshot view.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

from concord.models import (
    BillAction,
    BillSubject,
    BillSummary,
    BillTitle,
    Cosponsor,
    parse_bill,
    parse_bill_action,
    parse_bill_subject,
    parse_bill_summary,
    parse_bill_title,
    parse_cosponsor,
)
from concord.scraper.bills import (
    BILL_ENRICHMENT_SECTIONS,
    BILLS_JSONL_NAME,
    enrichment_jsonl_name,
)
from concord.storage.sqlite import SqliteStorage

_log = logging.getLogger("concord.pipeline.load_bills")


class LoadStats(NamedTuple):
    """Outcome of one :func:`load` invocation."""

    bills_written: int
    snapshots_read: int
    malformed: int
    tier2_snapshots_read: int = 0
    tier2_bills_updated: int = 0
    tier2_orphans_skipped: int = 0


def load(
    *,
    storage_dir: Path,
    db_path: Path,
    limit: int | None = None,
) -> LoadStats:
    """Project the latest Bill snapshot per key into SQLite.

    Reads ``<storage_dir>/bills.jsonl`` plus the five tier-2 sibling
    files when present. If the directory has no files, returns a
    zeroed :class:`LoadStats`. Stops after ``limit`` bills have been
    UPSERTed when set — note that ``limit`` applies to the tier-1
    projection; tier-2 projection runs against every bill present.
    """
    bills_written = 0
    snapshots_read = 0
    malformed = 0

    jsonl_path = storage_dir / BILLS_JSONL_NAME
    latest_per_key: dict[tuple[int, str, int], tuple[datetime, dict[str, Any]]] = {}
    if jsonl_path.exists():
        snapshots_read, malformed = _ingest_tier1(jsonl_path, latest_per_key)

    storage = SqliteStorage(db_path, load_vec=False)
    try:
        for (_c, _t, _n), (fetched_at, payload) in latest_per_key.items():
            try:
                bill = parse_bill(payload)
            except Exception as exc:
                malformed += 1
                _log.warning("skipping bill after parse failure: %s", exc)
                continue
            storage.upsert_bill(bill, fetched_at=fetched_at.isoformat())
            bills_written += 1
            if limit is not None and bills_written >= limit:
                break

        tier2_snapshots_read = 0
        tier2_bills_updated = 0
        tier2_orphans_skipped = 0
        for section in BILL_ENRICHMENT_SECTIONS:
            t2 = _load_tier2_section(storage_dir, section, storage)
            tier2_snapshots_read += t2["snapshots_read"]
            tier2_bills_updated += t2["bills_updated"]
            tier2_orphans_skipped += t2["orphans_skipped"]
            malformed += t2["malformed"]
    finally:
        storage.close()

    return LoadStats(
        bills_written=bills_written,
        snapshots_read=snapshots_read,
        malformed=malformed,
        tier2_snapshots_read=tier2_snapshots_read,
        tier2_bills_updated=tier2_bills_updated,
        tier2_orphans_skipped=tier2_orphans_skipped,
    )


def _ingest_tier1(
    jsonl_path: Path,
    latest_per_key: dict[tuple[int, str, int], tuple[datetime, dict[str, Any]]],
) -> tuple[int, int]:
    """Read bills.jsonl, populate ``latest_per_key`` in place. Return (read, malformed)."""
    snapshots_read = 0
    malformed = 0
    with jsonl_path.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            snapshots_read += 1
            try:
                envelope = json.loads(line)
            except json.JSONDecodeError as exc:
                malformed += 1
                _log.warning("skipping malformed line %d: %s", line_no, exc)
                continue
            try:
                key = envelope["key"]
                congress = int(key["congress"])
                bill_type = str(key["bill_type"]).lower()
                bill_number = int(key["bill_number"])
                fetched_at = datetime.fromisoformat(envelope["fetched_at"])
                payload = envelope["payload"]
            except (KeyError, TypeError, ValueError) as exc:
                malformed += 1
                _log.warning("skipping line %d with bad envelope: %s", line_no, exc)
                continue
            cell = (congress, bill_type, bill_number)
            current = latest_per_key.get(cell)
            if current is None or fetched_at > current[0]:
                latest_per_key[cell] = (fetched_at, payload)
    return snapshots_read, malformed


def _load_tier2_section(
    storage_dir: Path,
    section: str,
    storage: SqliteStorage,
) -> dict[str, int]:
    """Project one tier-2 JSONL file into its child table.

    Returns a counters dict with ``snapshots_read``, ``bills_updated``,
    ``orphans_skipped``, ``malformed``. A missing file is a no-op.
    """
    counters = {
        "snapshots_read": 0,
        "bills_updated": 0,
        "orphans_skipped": 0,
        "malformed": 0,
    }
    path = storage_dir / enrichment_jsonl_name(section)
    if not path.exists():
        return counters

    latest: dict[str, tuple[datetime, dict[str, Any]]] = {}
    with path.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            counters["snapshots_read"] += 1
            try:
                envelope = json.loads(line)
                key = envelope["key"]
                congress = int(key["congress"])
                bill_type = str(key["bill_type"]).lower()
                bill_number = int(key["bill_number"])
                fetched_at = datetime.fromisoformat(envelope["fetched_at"])
                payload = envelope["payload"]
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                counters["malformed"] += 1
                _log.warning("skipping malformed %s line %d: %s", path.name, line_no, exc)
                continue
            bill_id = f"{congress}-{bill_type}-{bill_number}"
            current = latest.get(bill_id)
            if current is None or fetched_at > current[0]:
                latest[bill_id] = (fetched_at, payload)

    for bill_id, (fetched_at, payload) in latest.items():
        if storage.get_bill(bill_id) is None:
            counters["orphans_skipped"] += 1
            _log.info(
                "tier-2 orphan: %s in %s but no parent bill row; skipping",
                bill_id,
                section,
            )
            continue
        _project_section(storage, section, bill_id, payload, fetched_at.isoformat())
        counters["bills_updated"] += 1
    return counters


# Per-section projection: extract the array from the payload, parse each
# row via the model layer, and call the matching storage method.
def _project_section(
    storage: SqliteStorage,
    section: str,
    bill_id: str,
    payload: dict[str, Any],
    fetched_at: str,
) -> None:
    _SECTION_PROJECTORS[section](storage, bill_id, payload, fetched_at)


def _project_cosponsors(
    storage: SqliteStorage, bill_id: str, payload: dict[str, Any], fetched_at: str
) -> None:
    raw = payload.get("cosponsors") or []
    cosponsors: list[Cosponsor] = []
    seen_bioguides: set[str] = set()
    for row in raw:
        if not isinstance(row, dict):
            continue
        parsed = parse_cosponsor(row)
        if parsed is None or parsed.bioguide_id in seen_bioguides:
            continue
        seen_bioguides.add(parsed.bioguide_id)
        cosponsors.append(parsed)
    storage.replace_bill_cosponsors(bill_id, cosponsors, fetched_at=fetched_at)


def _project_actions(
    storage: SqliteStorage, bill_id: str, payload: dict[str, Any], fetched_at: str
) -> None:
    raw = payload.get("actions") or []
    actions: list[BillAction] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        parsed = parse_bill_action(row)
        if parsed is None:
            continue
        actions.append(parsed)
    storage.replace_bill_actions(bill_id, actions, fetched_at=fetched_at)


def _project_subjects(
    storage: SqliteStorage, bill_id: str, payload: dict[str, Any], fetched_at: str
) -> None:
    subjects_obj = payload.get("subjects") or {}
    raw = subjects_obj.get("legislativeSubjects", []) if isinstance(subjects_obj, dict) else []
    subjects: list[BillSubject] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        parsed = parse_bill_subject(row)
        if parsed is None:
            continue
        subjects.append(parsed)
    storage.replace_bill_subjects(bill_id, subjects, fetched_at=fetched_at)


def _project_titles(
    storage: SqliteStorage, bill_id: str, payload: dict[str, Any], fetched_at: str
) -> None:
    raw = payload.get("titles") or []
    titles: list[BillTitle] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        parsed = parse_bill_title(row)
        if parsed is None:
            continue
        titles.append(parsed)
    storage.replace_bill_titles(bill_id, titles, fetched_at=fetched_at)


def _project_summaries(
    storage: SqliteStorage, bill_id: str, payload: dict[str, Any], fetched_at: str
) -> None:
    raw = payload.get("summaries") or []
    summaries: list[BillSummary] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        parsed = parse_bill_summary(row)
        if parsed is None:
            continue
        summaries.append(parsed)
    storage.replace_bill_summaries(bill_id, summaries, fetched_at=fetched_at)


_SECTION_PROJECTORS: dict[str, Callable[[SqliteStorage, str, dict[str, Any], str], None]] = {
    "cosponsors": _project_cosponsors,
    "actions": _project_actions,
    "subjects": _project_subjects,
    "titles": _project_titles,
    "summaries": _project_summaries,
}


__all__ = ["LoadStats", "load"]
