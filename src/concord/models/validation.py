"""Domain model for the Load Validation Failure mirror table (ADR 0023)."""

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, ValidationError


class ValidationFailure(BaseModel):
    """One upstream payload that violated a model contract at the Stage 1 load
    boundary (ADR 0023). Concord-originated — no ``from_congress_api`` factory.
    Field names match the ``validation_failures`` columns one-for-one. ``payload``
    is the offending value (a dict for JSON entities, the raw XML str for Senate
    votes); the storage layer serializes it with sorted keys."""

    model_config = ConfigDict(extra="ignore")

    entity: str
    entity_key: str
    source_file: str
    exc_type: str
    exc_msg: str
    field_path: str | None
    payload: Any

    @classmethod
    def from_exc(
        cls,
        *,
        entity: str,
        entity_key: str,
        source_file: str,
        exc: Exception,
        payload: Any,
    ) -> Self:
        """Build a failure record from a caught parse exception.

        ``field_path`` is the first Pydantic error location as a dotted string
        (e.g. ``sponsors.0.bioguideId``); ``None`` for ``ValueError`` / ``KeyError``,
        which carry no Pydantic loc.
        """
        field_path: str | None = None
        if isinstance(exc, ValidationError):
            errors = exc.errors()
            if errors:
                field_path = ".".join(str(part) for part in errors[0]["loc"])
        return cls(
            entity=entity,
            entity_key=entity_key,
            source_file=source_file,
            exc_type=type(exc).__name__,
            exc_msg=str(exc),
            field_path=field_path,
            payload=payload,
        )
