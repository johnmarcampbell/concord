"""Tests for the validation_failures mirror-table storage layer (ADR 0023).

Covers the replace-on-load contract: a full-family replace clears the whole
``entity IN (...)`` scope and re-inserts; a ``load_one``-style replace narrows
the delete to one ``entity_key``; an empty ``failures`` list still clears stale
rows (convergence); and the payload round-trips as sorted JSON.
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from concord.models.validation import ValidationFailure
from concord.storage.sqlite import SqliteStorage

_BILL_ENTITIES = ("bill", "cosponsor", "action", "subject", "title", "summary")


def _failure(
    *,
    entity: str = "cosponsor",
    entity_key: str = "119-hr-1",
    source_file: str = "bill_cosponsors.jsonl",
    field_path: str | None = "bioguideId",
    payload: object | None = None,
) -> ValidationFailure:
    return ValidationFailure(
        entity=entity,
        entity_key=entity_key,
        source_file=source_file,
        exc_type="ValidationError",
        exc_msg="boom",
        field_path=field_path,
        payload={"b": 2, "a": 1} if payload is None else payload,
    )


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[SqliteStorage]:
    s = SqliteStorage(tmp_path / "test.db", load_vec=False)
    yield s
    s.close()


class TestReplaceValidationFailures:
    def test_insert_and_count(self, storage: SqliteStorage) -> None:
        storage.replace_validation_failures(
            [_failure(), _failure(entity="action", field_path="actionDate")],
            entities=_BILL_ENTITIES,
        )
        assert storage.count_validation_failures() == 2
        assert storage.count_validation_failures(entity="cosponsor") == 1

    def test_replace_clears_whole_family_then_reinserts(self, storage: SqliteStorage) -> None:
        storage.replace_validation_failures(
            [_failure(entity="cosponsor"), _failure(entity="action")],
            entities=_BILL_ENTITIES,
        )
        # A second full-family load with a single failure must not accumulate.
        storage.replace_validation_failures([_failure(entity="title")], entities=_BILL_ENTITIES)
        assert storage.count_validation_failures() == 1
        assert storage.count_validation_failures(entity="title") == 1
        assert storage.count_validation_failures(entity="cosponsor") == 0

    def test_empty_failures_clears_stale_rows(self, storage: SqliteStorage) -> None:
        storage.replace_validation_failures([_failure()], entities=_BILL_ENTITIES)
        assert storage.count_validation_failures() == 1
        # A re-load that now parses cleanly passes an empty list — convergence.
        storage.replace_validation_failures([], entities=_BILL_ENTITIES)
        assert storage.count_validation_failures() == 0

    def test_entity_key_narrows_the_delete(self, storage: SqliteStorage) -> None:
        # Two bills' failures present.
        storage.replace_validation_failures(
            [
                _failure(entity_key="119-hr-1"),
                _failure(entity_key="119-hr-2"),
            ],
            entities=_BILL_ENTITIES,
        )
        # load_one for bill 1 re-loads cleanly: only bill 1's rows clear.
        storage.replace_validation_failures([], entities=_BILL_ENTITIES, entity_key="119-hr-1")
        rows = storage.connection.execute("SELECT entity_key FROM validation_failures").fetchall()
        assert [r["entity_key"] for r in rows] == ["119-hr-2"]

    def test_replace_does_not_touch_other_families(self, storage: SqliteStorage) -> None:
        storage.replace_validation_failures(
            [_failure(entity="member", entity_key="O000172", source_file="members.jsonl")],
            entities=("member", "term"),
        )
        # A bill-family replace must leave the member-family row intact.
        storage.replace_validation_failures([_failure()], entities=_BILL_ENTITIES)
        assert storage.count_validation_failures(entity="member") == 1
        assert storage.count_validation_failures(entity="cosponsor") == 1

    def test_payload_round_trips_as_sorted_json(self, storage: SqliteStorage) -> None:
        storage.replace_validation_failures(
            [_failure(payload={"b": 2, "a": 1})], entities=_BILL_ENTITIES
        )
        (stored,) = storage.connection.execute("SELECT payload FROM validation_failures").fetchone()
        # Sorted keys → byte-stable serialization.
        assert stored == '{"a": 1, "b": 2}'
        assert json.loads(stored) == {"a": 1, "b": 2}

    def test_null_field_path_persists(self, storage: SqliteStorage) -> None:
        storage.replace_validation_failures([_failure(field_path=None)], entities=_BILL_ENTITIES)
        (field_path,) = storage.connection.execute(
            "SELECT field_path FROM validation_failures"
        ).fetchone()
        assert field_path is None
