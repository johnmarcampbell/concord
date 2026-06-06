"""Shared scraper helpers — ADR 0006 snapshot-envelope read/write mechanics.

This is a thin utility module — *not* a base class. Per
[ADR 0007](../../../docs/adr/0007-parallel-pipelines-per-entity.md), each
entity's scraper stays a standalone module; the helpers here only encode
the snapshot-envelope mechanics that every entity needs identically — both
the *write* side (serialize one envelope line) and the ADR 0015 freshness-map
*read* side (decide whether a record is already fresh enough to skip).

The exports are:

* :func:`append_snapshot` — serialize one ADR 0006 ``{fetched_at, key,
  payload}`` envelope line through :class:`~concord.models.Snapshot`, the
  same model the loaders parse with. Confining the ``concord.models`` import
  to this module keeps the entity scrapers model-free (ADR 0018 Rule 5).
* :func:`load_freshness_map` — read a JSONL file once and return
  ``{key_tuple: latest fetched_at}``.
* :func:`parse_signal_timestamp` — parse the per-record ``updateDate``
  found on api.congress.gov list stubs (or detail payloads).
* :func:`is_stub_unchanged` — the per-record skip decision.
* :func:`load_bill_signal_map` — Bills-specific helper that reads
  ``bills.jsonl`` and returns ``{key: max(updateDate, updateDateIncludingText)}``
  off each line's ``payload``; used to gate enrichment fetches.
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from concord.models import Snapshot

_log = logging.getLogger("concord.scraper._common")


def append_snapshot(
    fh: IO[str],
    *,
    fetched_at: datetime,
    key: dict[str, str | int],
    payload: Any,
) -> None:
    """Append one ADR 0006 snapshot envelope line to an open handle.

    Serializes through :class:`~concord.models.Snapshot` so the envelope
    shape is single-sourced across the scraper (write) and loader (read)
    sides. ``payload`` is left as ``Any`` — written verbatim, never
    schema-validated (ADR 0018 Rule 1). A ``key`` value that is not
    ``str | int`` raises ``pydantic.ValidationError`` here rather than
    serializing a malformed envelope silently (ADR 0018 Rule 5).
    """
    fh.write(Snapshot[Any](fetched_at=fetched_at, key=key, payload=payload).model_dump_json())
    fh.write("\n")


def _coerce_utc(dt: datetime) -> datetime:
    """Ensure ``dt`` is timezone-aware; assume UTC if naive."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def parse_signal_timestamp(raw: str | None) -> datetime | None:
    """Parse a ``updateDate``-style string into a timezone-aware datetime.

    Accepts both date-only (``"2026-04-01"``) and full ISO-8601 forms
    (``"2026-04-10T08:00:00Z"``, ``"2025-09-09T18:53:19-04:00"``).
    Date-only values are coerced to midnight UTC. Naive datetimes are
    coerced to UTC. ``None`` input or parse failure returns ``None`` —
    the caller treats that as "stale, don't skip."
    """
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return _coerce_utc(parsed)


def load_freshness_map(
    path: Path,
    key_fields: tuple[str, ...],
) -> dict[tuple[Any, ...], datetime]:
    """Return ``{tuple(envelope.key[k] for k in key_fields): latest fetched_at}``.

    Reads ``path`` line-by-line, parses each ADR 0006 envelope, and
    retains the latest ``fetched_at`` per key. Malformed lines are
    logged and skipped — the failure mode for a corrupt JSONL line is
    "treat the record as stale," never crash. Missing file returns ``{}``.

    The returned datetimes are always timezone-aware (UTC if the line
    stored a naive value).
    """
    if not path.exists():
        return {}

    out: dict[tuple[Any, ...], datetime] = {}
    with path.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                envelope = json.loads(line)
                key = tuple(envelope["key"][k] for k in key_fields)
                fetched_at = datetime.fromisoformat(envelope["fetched_at"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                _log.warning(
                    "freshness map: skipping malformed line %d of %s: %s",
                    lineno,
                    path,
                    exc,
                )
                continue
            fetched_at = _coerce_utc(fetched_at)
            prior = out.get(key)
            if prior is None or fetched_at > prior:
                out[key] = fetched_at
    return out


def is_stub_unchanged(
    *,
    freshness: dict[tuple[Any, ...], datetime],
    key: tuple[Any, ...],
    signal: datetime | None,
) -> bool:
    """Return True iff we already have a snapshot at or after ``signal``.

    Skip rule: ``key`` must be present in ``freshness`` *and* ``signal``
    must be a parsed datetime *and* ``signal <= freshness[key]``. Any
    other condition returns False — i.e. fail-safe, fetch the record.
    Equality counts as "skip" because the upstream advertised change
    landed at or before our last snapshot.
    """
    if signal is None:
        return False
    prior = freshness.get(key)
    if prior is None:
        return False
    return signal <= prior


def load_bill_signal_map(
    bills_jsonl_path: Path,
) -> dict[tuple[int, str, int], datetime]:
    """Return ``{(congress, bill_type, bill_number): max(updateDate, updateDateIncludingText)}``.

    Drives the enrichment-fetch decision: the signal lives on the
    *bill* (from ``bills.jsonl``'s payload), but is compared against
    per-section ``bill_<section>.jsonl`` freshness maps. ``None`` payload
    fields are ignored; only the parseable values participate in the
    ``max``. A bill with no parseable signal at all is omitted from the
    returned map (the caller falls through to "fetch").
    """
    if not bills_jsonl_path.exists():
        return {}

    out: dict[tuple[int, str, int], datetime] = {}
    with bills_jsonl_path.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                envelope = json.loads(line)
                key_obj = envelope["key"]
                key = (
                    int(key_obj["congress"]),
                    str(key_obj["bill_type"]),
                    int(key_obj["bill_number"]),
                )
                payload = envelope.get("payload") or {}
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                _log.warning(
                    "bill signal map: skipping malformed line %d of %s: %s",
                    lineno,
                    bills_jsonl_path,
                    exc,
                )
                continue
            candidates = (
                parse_signal_timestamp(payload.get("updateDate")),
                parse_signal_timestamp(payload.get("updateDateIncludingText")),
            )
            parsed = [c for c in candidates if c is not None]
            if not parsed:
                continue
            signal = max(parsed)
            prior = out.get(key)
            if prior is None or signal > prior:
                out[key] = signal
    return out


__all__ = [
    "append_snapshot",
    "is_stub_unchanged",
    "load_bill_signal_map",
    "load_freshness_map",
    "parse_signal_timestamp",
]
