"""Unit tests for ``ValidationFailure.from_exc`` (ADR 0023).

The classmethod owns ``field_path`` extraction: the first Pydantic error
``loc`` rendered as a dotted scalar string, and ``None`` for the loc-less
``ValueError`` / ``KeyError`` cases.
"""

import pytest
from pydantic import BaseModel, ValidationError

from concord.models.validation import ValidationFailure


class _Inner(BaseModel):
    value: int


class _Outer(BaseModel):
    inner: _Inner


def _validation_error() -> ValidationError:
    """A real nested ValidationError whose first loc is ``inner.value``."""
    try:
        _Outer.model_validate({"inner": {"value": "not-an-int"}})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")  # pragma: no cover


class TestFromExc:
    def test_validation_error_yields_dotted_field_path(self) -> None:
        exc = _validation_error()
        failure = ValidationFailure.from_exc(
            entity="bill",
            entity_key="119-hr-1",
            source_file="bills.jsonl",
            exc=exc,
            payload={"inner": {"value": "not-an-int"}},
        )
        assert failure.field_path == "inner.value"
        assert failure.exc_type == "ValidationError"
        assert failure.entity == "bill"
        assert failure.entity_key == "119-hr-1"
        assert failure.source_file == "bills.jsonl"
        assert failure.exc_msg  # non-empty

    @pytest.mark.parametrize("exc", [ValueError("bad"), KeyError("missing")])
    def test_non_pydantic_exc_has_null_field_path(self, exc: Exception) -> None:
        failure = ValidationFailure.from_exc(
            entity="term",
            entity_key="O000172/119",
            source_file="members.jsonl",
            exc=exc,
            payload={"some": "payload"},
        )
        assert failure.field_path is None
        assert failure.exc_type == type(exc).__name__
        assert failure.exc_msg == str(exc)

    def test_payload_is_retained_verbatim(self) -> None:
        payload = {"b": 2, "a": 1}
        failure = ValidationFailure.from_exc(
            entity="cosponsor",
            entity_key="119-hr-1",
            source_file="bill_cosponsors.jsonl",
            exc=ValueError("x"),
            payload=payload,
        )
        assert failure.payload == payload

    def test_string_payload_supported(self) -> None:
        """Senate votes carry the raw XML str as payload, not a dict."""
        failure = ValidationFailure.from_exc(
            entity="vote",
            entity_key="senate-119-1-7",
            source_file="senate_votes.jsonl",
            exc=ValueError("xml broke"),
            payload="<roll_call_vote>…</roll_call_vote>",
        )
        assert failure.payload == "<roll_call_vote>…</roll_call_vote>"
        assert failure.field_path is None
