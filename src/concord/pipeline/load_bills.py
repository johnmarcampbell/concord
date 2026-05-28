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

import json
import logging
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import ValidationError

from concord.models import (
    Bill,
    BillAction,
    BillSubject,
    BillSummary,
    BillTitle,
    Cosponsor,
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
                bill = Bill.from_congress_api(payload)
            except (ValueError, ValidationError) as exc:
                malformed += 1
                _log.warning("skipping bill after parse failure: %s; payload=%r", exc, payload)
                continue
            storage.upsert_bill(bill, fetched_at=fetched_at.isoformat())
            bills_written += 1
            if limit is not None and bills_written >= limit:
                break

        tier2_snapshots_read = 0
        tier2_bills_updated = 0
        tier2_orphans_skipped = 0
        # Collapse the 5N tier-2 per-section transactions into one
        # batch. Each replace_bill_* joins this transaction via
        # storage._maybe_transaction. A bulk load that previously
        # paid the fsync cost per section per bill now pays it once.
        with storage.transaction():
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


def load_one(
    *,
    storage_dir: Path,
    db_path: Path,
    bill_id: str,
) -> LoadStats:
    """Project the latest snapshots for one Bill into SQLite.

    Filters ``bills.jsonl`` and the five tier-2 sibling files down to
    the envelopes whose key matches ``bill_id`` (format
    ``"{congress}-{bill_type}-{bill_number}"``), UPSERTs the parent
    bill row if found, and runs each per-section projector inside one
    transaction. Used by the web-initiated enrichment flow (ADR 0016)
    so the request-side projection cost is O(1) per bill rather than
    O(N).
    """
    try:
        congress_s, bill_type, bill_number_s = bill_id.split("-", 2)
        congress = int(congress_s)
        bill_number = int(bill_number_s)
        bill_type = bill_type.lower()
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"invalid bill_id {bill_id!r}: expected '{{c}}-{{t}}-{{n}}'") from exc
    target_key = (congress, bill_type, bill_number)

    bills_written = 0
    snapshots_read = 0
    malformed = 0

    jsonl_path = storage_dir / BILLS_JSONL_NAME
    latest: tuple[datetime, dict[str, Any]] | None = None
    if jsonl_path.exists():
        latest, snapshots_read, malformed = _scan_tier1_for_key(jsonl_path, target_key)

    storage = SqliteStorage(db_path, load_vec=False)
    try:
        if latest is not None:
            fetched_at, payload = latest
            try:
                bill = Bill.from_congress_api(payload)
                storage.upsert_bill(bill, fetched_at=fetched_at.isoformat())
                bills_written = 1
            except (ValueError, ValidationError) as exc:
                malformed += 1
                _log.warning("skipping bill after parse failure: %s; payload=%r", exc, payload)

        tier2_snapshots_read = 0
        tier2_bills_updated = 0
        tier2_orphans_skipped = 0
        with storage.transaction():
            for section in BILL_ENRICHMENT_SECTIONS:
                t2 = _load_tier2_section_for_bill(storage_dir, section, storage, bill_id)
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


def _scan_tier1_for_key(
    jsonl_path: Path,
    target_key: tuple[int, str, int],
) -> tuple[tuple[datetime, dict[str, Any]] | None, int, int]:
    """Return ``(latest, matched, malformed)`` for one key in bills.jsonl.

    ``matched`` is the count of envelopes whose key equals ``target_key``;
    it gives a per-bill analogue of ``snapshots_read`` from the bulk path
    (envelopes for other bills are ignored, not counted).
    """
    snapshots_read = 0
    malformed = 0
    latest: tuple[datetime, dict[str, Any]] | None = None
    with jsonl_path.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                envelope = json.loads(line)
                key = envelope["key"]
                congress = int(key["congress"])
                bill_type = str(key["bill_type"]).lower()
                bill_number = int(key["bill_number"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                malformed += 1
                _log.warning("skipping malformed bills.jsonl line %d: %s", line_no, exc)
                continue
            if (congress, bill_type, bill_number) != target_key:
                continue
            snapshots_read += 1
            try:
                fetched_at = datetime.fromisoformat(envelope["fetched_at"])
                payload = envelope["payload"]
            except (KeyError, TypeError, ValueError) as exc:
                malformed += 1
                _log.warning("skipping bills.jsonl line %d with bad envelope: %s", line_no, exc)
                continue
            if latest is None or fetched_at > latest[0]:
                latest = (fetched_at, payload)
    return latest, snapshots_read, malformed


def _load_tier2_section_for_bill(
    storage_dir: Path,
    section: str,
    storage: SqliteStorage,
    bill_id: str,
) -> dict[str, int]:
    """Per-bill twin of :func:`_load_tier2_section`."""
    counters = {
        "snapshots_read": 0,
        "bills_updated": 0,
        "orphans_skipped": 0,
        "malformed": 0,
    }
    path = storage_dir / enrichment_jsonl_name(section)
    if not path.exists():
        return counters

    latest: tuple[datetime, dict[str, Any]] | None = None
    with path.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                envelope = json.loads(line)
                key = envelope["key"]
                congress = int(key["congress"])
                bill_type = str(key["bill_type"]).lower()
                bill_number = int(key["bill_number"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                counters["malformed"] += 1
                _log.warning("skipping malformed %s line %d: %s", path.name, line_no, exc)
                continue
            if f"{congress}-{bill_type}-{bill_number}" != bill_id:
                continue
            counters["snapshots_read"] += 1
            try:
                fetched_at = datetime.fromisoformat(envelope["fetched_at"])
                payload = envelope["payload"]
            except (KeyError, TypeError, ValueError) as exc:
                counters["malformed"] += 1
                _log.warning("skipping %s line %d with bad envelope: %s", path.name, line_no, exc)
                continue
            if latest is None or fetched_at > latest[0]:
                latest = (fetched_at, payload)

    if latest is None:
        return counters
    if bill_id not in storage.bill_ids_present([bill_id]):
        counters["orphans_skipped"] = 1
        _log.info(
            "tier-2 orphan: %s in %s but no parent bill row; skipping",
            bill_id,
            section,
        )
        return counters
    fetched_at, payload = latest
    _project_section(storage, section, bill_id, payload, fetched_at.isoformat())
    counters["bills_updated"] = 1
    return counters


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

    # One IN-list query instead of N point-lookups: at full-Congress
    # scale the orphan check used to dominate the loader.
    present = storage.bill_ids_present(list(latest.keys()))
    for bill_id, (fetched_at, payload) in latest.items():
        if bill_id not in present:
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


def _parsed_rows[T](
    rows: Any,
    parse: Callable[[dict[str, Any]], T],
    bill_id: str,
    row_label: str,
) -> Iterator[T]:
    """Yield parsed rows; log + skip any that raise on ``parse``."""
    if not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            yield parse(row)
        except (ValueError, ValidationError) as exc:
            _log.warning("skipping %s row for %s: %s; payload=%r", row_label, bill_id, exc, row)


def _project_cosponsors(
    storage: SqliteStorage, bill_id: str, payload: dict[str, Any], fetched_at: str
) -> None:
    cosponsors: list[Cosponsor] = []
    seen_bioguides: set[str] = set()
    for parsed in _parsed_rows(
        payload.get("cosponsors") or [], Cosponsor.from_congress_api, bill_id, "cosponsor"
    ):
        if parsed.bioguide_id in seen_bioguides:
            continue
        seen_bioguides.add(parsed.bioguide_id)
        cosponsors.append(parsed)
    storage.replace_bill_cosponsors(bill_id, cosponsors, fetched_at=fetched_at)


def _project_actions(
    storage: SqliteStorage, bill_id: str, payload: dict[str, Any], fetched_at: str
) -> None:
    actions = list(
        _parsed_rows(payload.get("actions") or [], BillAction.from_congress_api, bill_id, "action")
    )
    storage.replace_bill_actions(bill_id, actions, fetched_at=fetched_at)


def _project_subjects(
    storage: SqliteStorage, bill_id: str, payload: dict[str, Any], fetched_at: str
) -> None:
    subjects_obj = payload.get("subjects") or {}
    raw = subjects_obj.get("legislativeSubjects", []) if isinstance(subjects_obj, dict) else []
    subjects = list(_parsed_rows(raw, BillSubject.from_congress_api, bill_id, "subject"))
    storage.replace_bill_subjects(bill_id, subjects, fetched_at=fetched_at)


def _project_titles(
    storage: SqliteStorage, bill_id: str, payload: dict[str, Any], fetched_at: str
) -> None:
    titles = list(
        _parsed_rows(payload.get("titles") or [], BillTitle.from_congress_api, bill_id, "title")
    )
    storage.replace_bill_titles(bill_id, titles, fetched_at=fetched_at)


def _project_summaries(
    storage: SqliteStorage, bill_id: str, payload: dict[str, Any], fetched_at: str
) -> None:
    summaries = list(
        _parsed_rows(
            payload.get("summaries") or [], BillSummary.from_congress_api, bill_id, "summary"
        )
    )
    storage.replace_bill_summaries(bill_id, summaries, fetched_at=fetched_at)


_SECTION_PROJECTORS: dict[str, Callable[[SqliteStorage, str, dict[str, Any], str], None]] = {
    "cosponsors": _project_cosponsors,
    "actions": _project_actions,
    "subjects": _project_subjects,
    "titles": _project_titles,
    "summaries": _project_summaries,
}


__all__ = ["LoadStats", "load", "load_one"]
