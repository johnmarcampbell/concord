"""Stage 1 — Bills loader.

Projects the ADR 0006 snapshot stream in ``<storage_dir>/bills.jsonl``
into the ``bills`` SQLite table. The natural key is the composite
``(congress, bill_type, bill_number)``; the loader keeps the latest
snapshot per key and UPSERTs one row each.

Phase 2b adds one sibling JSONL file per Bill section (per ADR 0009;
the catalogue is :data:`concord.models.bills.BILL_SECTIONS`) — the
loader reads each when present and projects to its child table,
stamping the corresponding ``bills.<section>_fetched_at`` column. A
Bill present in section snapshots but absent from ``bills.jsonl`` is
counted as a tier-2 orphan and skipped (it would violate the FK).

Re-running the loader over an unchanged JSONL is a no-op. Re-running
over a JSONL that gained new snapshots converges the SQL projection to
the latest-snapshot view.
"""

import logging
from collections.abc import Callable, Iterator
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import ValidationError

from concord.models._common import Snapshot
from concord.models.bills import (
    BILL_SECTIONS,
    BillAction,
    BillCosponsor,
    BillDetail,
    BillSection,
    BillSubject,
    BillSummary,
    BillTitle,
)
from concord.models.validation import ValidationFailure
from concord.pipeline._failures import parse_or_record
from concord.scraper.bills import BILLS_JSONL_NAME
from concord.storage.sqlite import SqliteStorage

_log = logging.getLogger("concord.pipeline.load_bills")

#: The Bill entity family for the validation_failures mirror table (ADR 0023):
#: the tier-1 bill plus each Bill section's singular entity name from the
#: catalogue. A full ``load`` clears this whole family before re-inserting;
#: ``load_one`` narrows by ``bill_id``.
_BILL_ENTITIES: tuple[str, ...] = ("bill", *(s.entity for s in BILL_SECTIONS))


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

    ``--limit`` is a non-converging smoke-test mode: it does **not** touch the
    ``validation_failures`` mirror table (a partial load would erase real
    failure rows for bills it never looked at — ADR 0023). The per-run
    ``malformed`` diagnostic is still reported.
    """
    bills_written = 0
    snapshots_read = 0
    envelope_failures = 0
    failures: list[ValidationFailure] = []

    jsonl_path = storage_dir / BILLS_JSONL_NAME
    latest_per_key: dict[tuple[int, str, int], tuple[datetime, dict[str, Any]]] = {}
    if jsonl_path.exists():
        snapshots_read, envelope_failures = _ingest_tier1(jsonl_path, latest_per_key)

    storage = SqliteStorage(db_path, load_vec=False)
    tier2_snapshots_read = 0
    tier2_bills_updated = 0
    tier2_orphans_skipped = 0
    try:
        # Whole-load-atomic: one transaction around tier-1, all five tier-2
        # sections, and the failures convergence (ADR 0028). Tier-1 now joins
        # the same batch tier-2 already used (tier-2's read of tier-1 rows sees
        # them via read-your-own-writes within the transaction), so a bulk load
        # pays one commit instead of one-per-bill plus one-per-section-per-bill,
        # and a mid-load crash never lands a bill with only some of its sections.
        with storage.transaction():
            for (_c, _t, _n), (fetched_at, payload) in latest_per_key.items():
                bill = parse_or_record(
                    failures,
                    partial(BillDetail.from_congress_api, payload),
                    entity="bill",
                    entity_key=f"{_c}-{_t}-{_n}",
                    source_file=BILLS_JSONL_NAME,
                    payload=payload,
                    log=_log,
                )
                if bill is None:
                    continue
                storage.upsert_bill(bill, fetched_at=fetched_at.isoformat())
                bills_written += 1
                if limit is not None and bills_written >= limit:
                    break

            for section in BILL_SECTIONS:
                t2 = _load_tier2_section(storage_dir, section, storage, failures)
                tier2_snapshots_read += t2["snapshots_read"]
                tier2_bills_updated += t2["bills_updated"]
                tier2_orphans_skipped += t2["orphans_skipped"]
                envelope_failures += t2["envelope_failures"]

            # A full load converges the family; a limited load must not (it only
            # processed a subset, so a family-wide replace would drop real rows).
            if limit is None:
                storage.replace_validation_failures(failures, entities=_BILL_ENTITIES)
    finally:
        storage.close()

    # malformed is derived, not assembled: envelope/JSONL corruption (class (a))
    # plus every model-parse failure (class (b), the failures list). See ADR 0023.
    return LoadStats(
        bills_written=bills_written,
        snapshots_read=snapshots_read,
        malformed=envelope_failures + len(failures),
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
    envelope_failures = 0
    failures: list[ValidationFailure] = []

    jsonl_path = storage_dir / BILLS_JSONL_NAME
    latest: tuple[datetime, dict[str, Any]] | None = None
    if jsonl_path.exists():
        latest, snapshots_read, envelope_failures = _scan_tier1_for_key(jsonl_path, target_key)

    storage = SqliteStorage(db_path, load_vec=False)
    tier2_snapshots_read = 0
    tier2_bills_updated = 0
    tier2_orphans_skipped = 0
    try:
        # Whole-load-atomic, per-bill: tier-1, every tier-2 section, and the
        # narrowed failures convergence land as one transaction (ADR 0028), so
        # a crash mid-enrichment never leaves this bill's sections half-applied.
        with storage.transaction():
            if latest is not None:
                fetched_at, payload = latest
                bill = parse_or_record(
                    failures,
                    partial(BillDetail.from_congress_api, payload),
                    entity="bill",
                    entity_key=bill_id,
                    source_file=BILLS_JSONL_NAME,
                    payload=payload,
                    log=_log,
                )
                if bill is not None:
                    storage.upsert_bill(bill, fetched_at=fetched_at.isoformat())
                    bills_written = 1

            for section in BILL_SECTIONS:
                t2 = _load_tier2_section_for_bill(storage_dir, section, storage, bill_id, failures)
                tier2_snapshots_read += t2["snapshots_read"]
                tier2_bills_updated += t2["bills_updated"]
                tier2_orphans_skipped += t2["orphans_skipped"]
                envelope_failures += t2["envelope_failures"]

            # Narrow the replace to this one bill (ADR 0016/0023): the per-bill
            # enrichment flow converges only this bill's rows, never disturbing
            # other bills' failures — so unlike the bulk ``--limit`` path it always
            # runs.
            storage.replace_validation_failures(
                failures, entities=_BILL_ENTITIES, entity_key=bill_id
            )
    finally:
        storage.close()

    # malformed is derived, not assembled: envelope/JSONL corruption (class (a))
    # plus every model-parse failure (class (b), the failures list). See ADR 0023.
    return LoadStats(
        bills_written=bills_written,
        snapshots_read=snapshots_read,
        malformed=envelope_failures + len(failures),
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
                snap = Snapshot[dict[str, Any]].model_validate_json(line)
                congress = int(snap.key["congress"])
                bill_type = str(snap.key["bill_type"]).lower()
                bill_number = int(snap.key["bill_number"])
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                malformed += 1
                _log.warning("skipping malformed bills.jsonl line %d: %s", line_no, exc)
                continue
            if (congress, bill_type, bill_number) != target_key:
                continue
            snapshots_read += 1
            if latest is None or snap.fetched_at > latest[0]:
                latest = (snap.fetched_at, snap.payload)
    return latest, snapshots_read, malformed


def _load_tier2_section_for_bill(
    storage_dir: Path,
    section: BillSection,
    storage: SqliteStorage,
    bill_id: str,
    failures: list[ValidationFailure],
) -> dict[str, int]:
    """Per-bill twin of :func:`_load_tier2_section`."""
    counters = {
        "snapshots_read": 0,
        "bills_updated": 0,
        "orphans_skipped": 0,
        "envelope_failures": 0,
    }
    path = storage_dir / section.jsonl_name
    if not path.exists():
        return counters

    latest: tuple[datetime, dict[str, Any]] | None = None
    with path.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                snap = Snapshot[dict[str, Any]].model_validate_json(line)
                congress = int(snap.key["congress"])
                bill_type = str(snap.key["bill_type"]).lower()
                bill_number = int(snap.key["bill_number"])
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                counters["envelope_failures"] += 1
                _log.warning("skipping malformed %s line %d: %s", path.name, line_no, exc)
                continue
            if f"{congress}-{bill_type}-{bill_number}" != bill_id:
                continue
            counters["snapshots_read"] += 1
            if latest is None or snap.fetched_at > latest[0]:
                latest = (snap.fetched_at, snap.payload)

    if latest is None:
        return counters
    if bill_id not in storage.bill_ids_present([bill_id]):
        counters["orphans_skipped"] = 1
        _log.info(
            "tier-2 orphan: %s in %s but no parent bill row; skipping",
            bill_id,
            section.name,
        )
        return counters
    fetched_at, payload = latest
    _project_section(storage, section, bill_id, payload, fetched_at.isoformat(), failures)
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
                snap = Snapshot[dict[str, Any]].model_validate_json(line)
                congress = int(snap.key["congress"])
                bill_type = str(snap.key["bill_type"]).lower()
                bill_number = int(snap.key["bill_number"])
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                malformed += 1
                _log.warning("skipping malformed line %d: %s", line_no, exc)
                continue
            cell = (congress, bill_type, bill_number)
            current = latest_per_key.get(cell)
            if current is None or snap.fetched_at > current[0]:
                latest_per_key[cell] = (snap.fetched_at, snap.payload)
    return snapshots_read, malformed


def _load_tier2_section(
    storage_dir: Path,
    section: BillSection,
    storage: SqliteStorage,
    failures: list[ValidationFailure],
) -> dict[str, int]:
    """Project one Bill section's JSONL file into its child table.

    Returns a counters dict with ``snapshots_read``, ``bills_updated``,
    ``orphans_skipped``, ``envelope_failures``. A missing file is a no-op.
    """
    counters = {
        "snapshots_read": 0,
        "bills_updated": 0,
        "orphans_skipped": 0,
        "envelope_failures": 0,
    }
    path = storage_dir / section.jsonl_name
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
                snap = Snapshot[dict[str, Any]].model_validate_json(line)
                congress = int(snap.key["congress"])
                bill_type = str(snap.key["bill_type"]).lower()
                bill_number = int(snap.key["bill_number"])
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                counters["envelope_failures"] += 1
                _log.warning("skipping malformed %s line %d: %s", path.name, line_no, exc)
                continue
            bill_id = f"{congress}-{bill_type}-{bill_number}"
            current = latest.get(bill_id)
            if current is None or snap.fetched_at > current[0]:
                latest[bill_id] = (snap.fetched_at, snap.payload)

    # One IN-list query instead of N point-lookups: at full-Congress
    # scale the orphan check used to dominate the loader.
    present = storage.bill_ids_present(list(latest.keys()))
    for bill_id, (fetched_at, payload) in latest.items():
        if bill_id not in present:
            counters["orphans_skipped"] += 1
            _log.info(
                "tier-2 orphan: %s in %s but no parent bill row; skipping",
                bill_id,
                section.name,
            )
            continue
        _project_section(storage, section, bill_id, payload, fetched_at.isoformat(), failures)
        counters["bills_updated"] += 1
    return counters


# Per-section projection: extract the array from the payload, parse each
# row via the model layer, and call the matching storage method. ``source_file``
# (the section's JSONL the rows came from) and ``entity`` (the section's
# singular name, both from the Bill section catalogue) thread down to
# ``_parsed_rows`` so each dropped child row becomes a validation_failures row
# keyed on the parent ``bill_id`` (ADR 0023).
def _project_section(
    storage: SqliteStorage,
    section: BillSection,
    bill_id: str,
    payload: dict[str, Any],
    fetched_at: str,
    failures: list[ValidationFailure],
) -> None:
    _SECTION_PROJECTORS[section.name](
        storage, bill_id, payload, fetched_at, section.jsonl_name, section.entity, failures
    )


def _parsed_rows[T](
    rows: Any,
    parse: Callable[[dict[str, Any]], T],
    bill_id: str,
    entity: str,
    source_file: str,
    failures: list[ValidationFailure],
) -> Iterator[T]:
    """Yield parsed rows; record + skip any that raise on ``parse``.

    ``entity`` is the section's singular name (``"cosponsor"`` / ``"action"`` /
    …, from the Bill section catalogue); each failed row appends a
    :class:`ValidationFailure` keyed on the parent ``bill_id`` (ADR 0023).
    """
    if not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, dict):
            continue
        parsed = parse_or_record(
            failures,
            partial(parse, row),
            entity=entity,
            entity_key=bill_id,
            source_file=source_file,
            payload=row,
            log=_log,
        )
        if parsed is not None:
            yield parsed


def _project_cosponsors(
    storage: SqliteStorage,
    bill_id: str,
    payload: dict[str, Any],
    fetched_at: str,
    source_file: str,
    entity: str,
    failures: list[ValidationFailure],
) -> None:
    cosponsors: list[BillCosponsor] = []
    seen_bioguides: set[str] = set()
    for parsed in _parsed_rows(
        payload.get("cosponsors") or [],
        BillCosponsor.from_congress_api,
        bill_id,
        entity,
        source_file,
        failures,
    ):
        if parsed.bioguide_id in seen_bioguides:
            continue
        seen_bioguides.add(parsed.bioguide_id)
        cosponsors.append(parsed)
    storage.replace_bill_cosponsors(bill_id, cosponsors, fetched_at=fetched_at)


def _project_actions(
    storage: SqliteStorage,
    bill_id: str,
    payload: dict[str, Any],
    fetched_at: str,
    source_file: str,
    entity: str,
    failures: list[ValidationFailure],
) -> None:
    actions = list(
        _parsed_rows(
            payload.get("actions") or [],
            BillAction.from_congress_api,
            bill_id,
            entity,
            source_file,
            failures,
        )
    )
    storage.replace_bill_actions(bill_id, actions, fetched_at=fetched_at)


def _project_subjects(
    storage: SqliteStorage,
    bill_id: str,
    payload: dict[str, Any],
    fetched_at: str,
    source_file: str,
    entity: str,
    failures: list[ValidationFailure],
) -> None:
    subjects_obj = payload.get("subjects") or {}
    raw = subjects_obj.get("legislativeSubjects", []) if isinstance(subjects_obj, dict) else []
    subjects = list(
        _parsed_rows(raw, BillSubject.from_congress_api, bill_id, entity, source_file, failures)
    )
    storage.replace_bill_subjects(bill_id, subjects, fetched_at=fetched_at)


def _project_titles(
    storage: SqliteStorage,
    bill_id: str,
    payload: dict[str, Any],
    fetched_at: str,
    source_file: str,
    entity: str,
    failures: list[ValidationFailure],
) -> None:
    titles = list(
        _parsed_rows(
            payload.get("titles") or [],
            BillTitle.from_congress_api,
            bill_id,
            entity,
            source_file,
            failures,
        )
    )
    storage.replace_bill_titles(bill_id, titles, fetched_at=fetched_at)


def _project_summaries(
    storage: SqliteStorage,
    bill_id: str,
    payload: dict[str, Any],
    fetched_at: str,
    source_file: str,
    entity: str,
    failures: list[ValidationFailure],
) -> None:
    summaries = list(
        _parsed_rows(
            payload.get("summaries") or [],
            BillSummary.from_congress_api,
            bill_id,
            entity,
            source_file,
            failures,
        )
    )
    storage.replace_bill_summaries(bill_id, summaries, fetched_at=fetched_at)


_SectionProjector = Callable[
    [SqliteStorage, str, dict[str, Any], str, str, str, list[ValidationFailure]], None
]

_SECTION_PROJECTORS: dict[str, _SectionProjector] = {
    "cosponsors": _project_cosponsors,
    "actions": _project_actions,
    "subjects": _project_subjects,
    "titles": _project_titles,
    "summaries": _project_summaries,
}


__all__ = ["LoadStats", "load", "load_one"]
