"""Shared Stage 1 parse-or-record helper for the validation_failures table (ADR 0023)."""

import logging
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from concord.models.validation import ValidationFailure

#: The model-contract exception set for the JSON entities. A parser raises one
#: of these exactly when an upstream payload violates its contract — the
#: invariant a ``validation_failures`` row stands for (ADR 0023). The Senate XML
#: branch overrides ``catching`` with its own ``SenateXmlError`` so an unexpected
#: internal defect surfaces instead of being mislabelled upstream drift.
_PARSE_EXCEPTIONS: tuple[type[Exception], ...] = (KeyError, ValueError, ValidationError)


def parse_or_record[T](
    failures: list[ValidationFailure],
    parse: Callable[[], T],
    *,
    entity: str,
    entity_key: str,
    source_file: str,
    payload: Any,
    log: logging.Logger,
    catching: tuple[type[Exception], ...] = _PARSE_EXCEPTIONS,
) -> T | None:
    """Run ``parse``; on a contract violation record a ValidationFailure and return None.

    The single home for the Stage 1 "model-parse rejection" protocol (ADR 0023):
    append a :class:`ValidationFailure` keyed on ``entity`` / ``entity_key`` and
    keep a one-line warning heartbeat. ``catching`` is the model-contract
    exception set — the JSON entities pass the default; the Senate XML branch
    passes ``(SenateXmlError,)`` so an unexpected internal bug still surfaces
    instead of being mislabelled upstream drift.
    """
    try:
        return parse()
    except catching as exc:
        failures.append(
            ValidationFailure.from_exc(
                entity=entity,
                entity_key=entity_key,
                source_file=source_file,
                exc=exc,
                payload=payload,
            )
        )
        log.warning("skipping %s %s after parse failure: %s", entity, entity_key, exc)
        return None


__all__ = ["parse_or_record"]
