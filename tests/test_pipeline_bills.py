"""Tests for the Bills Stage 1 (load) and Stage 2 (index) pipelines."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from concord.pipeline.index_bills import index as index_bills
from concord.pipeline.index_bills import reindex_one
from concord.pipeline.load_bills import load as load_bills
from concord.pipeline.load_bills import load_one
from concord.scraper.bills import BILLS_JSONL_NAME, enrichment_jsonl_name
from concord.storage.sqlite import SqliteStorage
from tests._snapshots import wrap_snapshot

FIXED_FETCHED_AT = datetime(2026, 5, 25, 14, 2, 11, tzinfo=UTC)


def _fixture(name: str) -> dict[str, Any]:
    here = Path(__file__).parent / "fixtures" / "api" / "bills"
    return json.loads((here / name).read_text())


def _detail_payload(name: str) -> dict[str, Any]:
    """Return just the unwrapped ``bill`` object the scraper persists."""
    return _fixture(name)["bill"]


def _envelope_for(
    payload: dict[str, Any],
    *,
    fetched_at: datetime = FIXED_FETCHED_AT,
) -> dict[str, Any]:
    return wrap_snapshot(
        payload,
        fetched_at=fetched_at,
        key={
            "congress": int(payload["congress"]),
            "bill_type": str(payload["type"]).lower(),
            "bill_number": int(payload["number"]),
        },
    )


def _write_jsonl(storage_dir: Path, envelopes: list[dict[str, Any]]) -> None:
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / BILLS_JSONL_NAME).write_text(
        "\n".join(json.dumps(e) for e in envelopes) + "\n",
        encoding="utf-8",
    )


def _envelope_for_section(
    payload: dict[str, Any],
    *,
    bill_key: tuple[int, str, int],
    fetched_at: datetime = FIXED_FETCHED_AT,
) -> dict[str, Any]:
    congress, bill_type, bill_number = bill_key
    return wrap_snapshot(
        payload,
        fetched_at=fetched_at,
        key={
            "congress": congress,
            "bill_type": bill_type.lower(),
            "bill_number": bill_number,
        },
    )


def _failure_rows(db: Path) -> list[tuple[str, str]]:
    """Read ``(entity, entity_key)`` from the production validation_failures table."""
    with SqliteStorage(db, load_vec=False) as storage:
        cursor = storage.connection.execute(
            "SELECT entity, entity_key FROM validation_failures ORDER BY entity, entity_key"
        )
        return [(r["entity"], r["entity_key"]) for r in cursor.fetchall()]


class TestLoadBills:
    def test_basic_load(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        _write_jsonl(
            storage_dir,
            [
                _envelope_for(_detail_payload("detail_119_hr_1.json")),
                _envelope_for(_detail_payload("detail_119_hr_22.json")),
            ],
        )

        stats = load_bills(storage_dir=storage_dir, db_path=db)
        assert stats.bills_written == 2
        assert stats.snapshots_read == 2
        assert stats.malformed == 0

        with SqliteStorage(db, load_vec=False) as storage:
            row = storage.get_bill("119-hr-1")
            assert row is not None
            assert row["title"] == "Lower Energy Costs Act"
            assert row["sponsor_bioguide_id"] == "S001176"
            assert row["policy_area"] == "Energy"

    def test_latest_snapshot_wins(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        early = _detail_payload("detail_119_hr_1.json")
        late = {**early, "title": "Renamed Bill"}
        _write_jsonl(
            storage_dir,
            [
                _envelope_for(early, fetched_at=FIXED_FETCHED_AT),
                _envelope_for(late, fetched_at=FIXED_FETCHED_AT + timedelta(hours=1)),
            ],
        )
        load_bills(storage_dir=storage_dir, db_path=db)
        with SqliteStorage(db, load_vec=False) as storage:
            row = storage.get_bill("119-hr-1")
            assert row is not None
            assert row["title"] == "Renamed Bill"

    def test_limit_caps_writes(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        _write_jsonl(
            storage_dir,
            [
                _envelope_for(_detail_payload("detail_119_hr_1.json")),
                _envelope_for(_detail_payload("detail_119_hr_22.json")),
            ],
        )
        stats = load_bills(storage_dir=storage_dir, db_path=db, limit=1)
        assert stats.bills_written == 1

    def test_missing_file_is_noop(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "no_such_dir"
        db = tmp_path / "test.db"
        stats = load_bills(storage_dir=storage_dir, db_path=db)
        assert stats.bills_written == 0
        assert stats.snapshots_read == 0
        assert stats.malformed == 0
        assert stats.tier2_snapshots_read == 0
        assert stats.tier2_bills_updated == 0
        assert stats.tier2_orphans_skipped == 0

    def test_idempotent_rerun(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        _write_jsonl(
            storage_dir,
            [_envelope_for(_detail_payload("detail_119_hr_1.json"))],
        )
        load_bills(storage_dir=storage_dir, db_path=db)
        load_bills(storage_dir=storage_dir, db_path=db)
        with SqliteStorage(db, load_vec=False) as storage:
            count = storage.connection.execute("SELECT COUNT(*) FROM bills").fetchone()[0]
            assert count == 1


class TestLoadBillsTier2:
    def _write_tier1(self, storage_dir: Path) -> None:
        _write_jsonl(
            storage_dir,
            [
                _envelope_for(_detail_payload("detail_119_hr_1.json")),
                _envelope_for(_detail_payload("detail_119_hr_22.json")),
            ],
        )

    def test_tier1_only_leaves_fetched_at_columns_null(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        self._write_tier1(storage_dir)
        load_bills(storage_dir=storage_dir, db_path=db)
        with SqliteStorage(db, load_vec=False) as storage:
            row = storage.get_bill("119-hr-1")
            assert row is not None
            assert row["cosponsors_fetched_at"] is None
            assert row["actions_fetched_at"] is None
            assert row["subjects_fetched_at"] is None
            assert row["titles_fetched_at"] is None
            assert row["summaries_fetched_at"] is None

    def test_loads_all_five_sections_when_present(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        self._write_tier1(storage_dir)

        # Write tier-2 files for 119-hr-1.
        section_to_fixture = {
            "cosponsors": "cosponsors_119_hr_22.json",
            "actions": "actions_119_hr_1.json",
            "subjects": "subjects_119_hr_1.json",
            "titles": "titles_119_hr_1.json",
            "summaries": "summaries_119_hr_1.json",
        }
        for section, name in section_to_fixture.items():
            (storage_dir / enrichment_jsonl_name(section)).write_text(
                json.dumps(_envelope_for_section(_fixture(name), bill_key=(119, "hr", 1))) + "\n",
                encoding="utf-8",
            )

        stats = load_bills(storage_dir=storage_dir, db_path=db)
        assert stats.tier2_bills_updated == 5  # one bill, five sections
        assert stats.tier2_orphans_skipped == 0

        with SqliteStorage(db, load_vec=False) as storage:
            row = storage.get_bill("119-hr-1")
            assert row is not None
            assert row["cosponsors_fetched_at"] is not None
            assert row["actions_fetched_at"] is not None
            cosponsors = storage.cosponsors_for_bill("119-hr-1")
            assert len(cosponsors) == 3
            actions = storage.actions_for_bill("119-hr-1")
            assert len(actions) == 6
            subjects = storage.subjects_for_bill("119-hr-1")
            assert len(subjects) == 10
            titles = storage.titles_for_bill("119-hr-1")
            assert len(titles) == 4
            summaries = storage.summaries_for_bill("119-hr-1")
            assert len(summaries) == 3
            # The other bill (119-hr-22) stays tier-1-only.
            other = storage.get_bill("119-hr-22")
            assert other is not None
            assert other["cosponsors_fetched_at"] is None

    def test_tier2_orphan_skipped(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        storage_dir.mkdir(parents=True, exist_ok=True)
        # Tier-2 only, with a bill_id that isn't in bills.jsonl (because
        # bills.jsonl doesn't exist).
        (storage_dir / enrichment_jsonl_name("cosponsors")).write_text(
            json.dumps(
                _envelope_for_section(
                    _fixture("cosponsors_119_hr_22.json"), bill_key=(119, "hr", 22)
                )
            )
            + "\n",
            encoding="utf-8",
        )
        stats = load_bills(storage_dir=storage_dir, db_path=db)
        assert stats.bills_written == 0
        assert stats.tier2_orphans_skipped == 1

    def test_rerunning_load_preserves_fetched_at(self, tmp_path: Path) -> None:
        """Re-running tier-1 load after enrichment must NOT clear *_fetched_at.

        Guards against a regression where the parent UPSERT clobbers the
        tier-2 stamp columns. The plan's DDL listed them on the bills
        row, but the upsert SQL deliberately omits them — this test
        pins that.
        """
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        self._write_tier1(storage_dir)
        (storage_dir / enrichment_jsonl_name("cosponsors")).write_text(
            json.dumps(
                _envelope_for_section(
                    _fixture("cosponsors_119_hr_22.json"), bill_key=(119, "hr", 1)
                )
            )
            + "\n",
            encoding="utf-8",
        )
        load_bills(storage_dir=storage_dir, db_path=db)
        with SqliteStorage(db, load_vec=False) as storage:
            initial = storage.get_bill("119-hr-1")
            assert initial is not None
            assert initial["cosponsors_fetched_at"] is not None
            initial_stamp = initial["cosponsors_fetched_at"]

        # Re-run the loader; the parent tier-1 row gets UPSERTed again.
        load_bills(storage_dir=storage_dir, db_path=db)
        with SqliteStorage(db, load_vec=False) as storage:
            row = storage.get_bill("119-hr-1")
            assert row is not None
            assert row["cosponsors_fetched_at"] == initial_stamp

    def test_idempotent_rerun_tier2(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        self._write_tier1(storage_dir)
        (storage_dir / enrichment_jsonl_name("cosponsors")).write_text(
            json.dumps(
                _envelope_for_section(
                    _fixture("cosponsors_119_hr_22.json"), bill_key=(119, "hr", 1)
                )
            )
            + "\n",
            encoding="utf-8",
        )
        load_bills(storage_dir=storage_dir, db_path=db)
        load_bills(storage_dir=storage_dir, db_path=db)
        with SqliteStorage(db, load_vec=False) as storage:
            cosponsors = storage.cosponsors_for_bill("119-hr-1")
            assert len(cosponsors) == 3


class TestLoadBillsValidationFailures:
    """Class-(b) model-parse failures land in validation_failures (ADR 0023)."""

    def _write_cosponsors(
        self, storage_dir: Path, bill_key: tuple[int, str, int], rows: list[dict[str, Any]]
    ) -> None:
        (storage_dir / enrichment_jsonl_name("cosponsors")).write_text(
            json.dumps(_envelope_for_section({"cosponsors": rows}, bill_key=bill_key)) + "\n",
            encoding="utf-8",
        )

    def test_tier1_and_tier2_failures_recorded(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        # hr-1 is valid (so its tier-2 parent exists); hr-22 has an invalid
        # originChamber so BillDetail.from_congress_api raises ValidationError.
        good = _detail_payload("detail_119_hr_1.json")
        broken = {**_detail_payload("detail_119_hr_22.json"), "originChamber": "Moon"}
        _write_jsonl(storage_dir, [_envelope_for(good), _envelope_for(broken)])
        # A cosponsor row missing bioguideId → BillCosponsor.from_congress_api raises.
        self._write_cosponsors(
            storage_dir,
            (119, "hr", 1),
            [{"sponsorshipDate": "2025-01-03", "isOriginalCosponsor": True}],
        )

        stats = load_bills(storage_dir=storage_dir, db_path=db)
        assert stats.bills_written == 1  # only hr-1
        # malformed counts both class-(b) failures (the tier-2 cosponsor was
        # silently uncounted before ADR 0023 — option C closes that gap).
        assert stats.malformed == 2

        with SqliteStorage(db, load_vec=False) as storage:
            rows = storage.connection.execute(
                "SELECT entity, entity_key, source_file, field_path FROM validation_failures "
                "ORDER BY entity"
            ).fetchall()
        assert [(r["entity"], r["entity_key"]) for r in rows] == [
            ("bill", "119-hr-22"),
            ("cosponsor", "119-hr-1"),
        ]
        by_entity = {r["entity"]: r for r in rows}
        # Pydantic Literal rejection on origin_chamber yields a dotted field_path.
        assert by_entity["bill"]["field_path"] == "origin_chamber"
        assert by_entity["bill"]["source_file"] == BILLS_JSONL_NAME
        # The cosponsor row failed on a ValueError (no Pydantic loc).
        assert by_entity["cosponsor"]["field_path"] is None
        assert by_entity["cosponsor"]["source_file"] == enrichment_jsonl_name("cosponsors")

    def test_failures_are_idempotent(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        broken = {**_detail_payload("detail_119_hr_1.json"), "originChamber": "Moon"}
        _write_jsonl(storage_dir, [_envelope_for(broken)])
        load_bills(storage_dir=storage_dir, db_path=db)
        load_bills(storage_dir=storage_dir, db_path=db)
        # Re-running does not double the row (replace-on-load, not append).
        assert _failure_rows(db) == [("bill", "119-hr-1")]

    def test_clean_reload_converges_away_stale_rows(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        broken = {**_detail_payload("detail_119_hr_1.json"), "originChamber": "Moon"}
        _write_jsonl(storage_dir, [_envelope_for(broken)])
        load_bills(storage_dir=storage_dir, db_path=db)
        assert _failure_rows(db) == [("bill", "119-hr-1")]
        # Re-scrape fixed the payload; the mirror table must drop the stale row.
        _write_jsonl(storage_dir, [_envelope_for(_detail_payload("detail_119_hr_1.json"))])
        load_bills(storage_dir=storage_dir, db_path=db)
        assert _failure_rows(db) == []

    def test_load_one_scopes_the_delete_to_one_bill(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        good = _detail_payload("detail_119_hr_1.json")
        broken = {**_detail_payload("detail_119_hr_22.json"), "originChamber": "Moon"}
        _write_jsonl(storage_dir, [_envelope_for(good), _envelope_for(broken)])
        self._write_cosponsors(
            storage_dir,
            (119, "hr", 1),
            [{"sponsorshipDate": "2025-01-03", "isOriginalCosponsor": True}],
        )
        load_bills(storage_dir=storage_dir, db_path=db)
        assert _failure_rows(db) == [("bill", "119-hr-22"), ("cosponsor", "119-hr-1")]

        # Fix hr-1's cosponsors and re-load just that bill. load_one narrows the
        # delete to entity_key=119-hr-1, so hr-22's bill failure must survive.
        self._write_cosponsors(
            storage_dir,
            (119, "hr", 1),
            [
                {
                    "bioguideId": "S001176",
                    "sponsorshipDate": "2025-01-03",
                    "isOriginalCosponsor": True,
                }
            ],
        )
        load_one(storage_dir=storage_dir, db_path=db, bill_id="119-hr-1")

        assert _failure_rows(db) == [("bill", "119-hr-22")]

    def test_limit_load_does_not_touch_failure_table(self, tmp_path: Path) -> None:
        """A ``--limit`` load is non-converging: it must not erase rows for bills
        it never (fully) processed (ADR 0023 / review task 4)."""
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        # A full load records a bill failure.
        broken = {**_detail_payload("detail_119_hr_1.json"), "originChamber": "Moon"}
        _write_jsonl(storage_dir, [_envelope_for(broken)])
        load_bills(storage_dir=storage_dir, db_path=db)
        assert _failure_rows(db) == [("bill", "119-hr-1")]

        # The JSONL is now clean. A LIMITED load would, if it touched the table,
        # converge it to empty (no failures) — the guard keeps the stale row.
        _write_jsonl(storage_dir, [_envelope_for(_detail_payload("detail_119_hr_22.json"))])
        stats = load_bills(storage_dir=storage_dir, db_path=db, limit=1)
        assert stats.bills_written == 1
        assert _failure_rows(db) == [("bill", "119-hr-1")]  # untouched

        # The contrast: a full (non-limited) load over the same clean JSONL
        # *does* converge the stale row away.
        load_bills(storage_dir=storage_dir, db_path=db)
        assert _failure_rows(db) == []


class TestIndexBills:
    def test_repopulates_fts(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        _write_jsonl(
            storage_dir,
            [
                _envelope_for(_detail_payload("detail_119_hr_1.json")),
                _envelope_for(_detail_payload("detail_119_hr_22.json")),
            ],
        )
        load_bills(storage_dir=storage_dir, db_path=db)
        stats = index_bills(db_path=db)
        assert stats.indexed_bills == 2
        with SqliteStorage(db, load_vec=False) as storage:
            rows = storage.connection.execute(
                "SELECT bill_id, identifier FROM bills_fts ORDER BY bill_id"
            ).fetchall()
            assert [r["bill_id"] for r in rows] == ["119-hr-1", "119-hr-22"]
            assert rows[0]["identifier"] == "hr 1"

    def test_index_is_idempotent(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        _write_jsonl(
            storage_dir,
            [_envelope_for(_detail_payload("detail_119_hr_1.json"))],
        )
        load_bills(storage_dir=storage_dir, db_path=db)
        index_bills(db_path=db)
        index_bills(db_path=db)
        with SqliteStorage(db, load_vec=False) as storage:
            (count,) = storage.connection.execute("SELECT COUNT(*) FROM bills_fts").fetchone()
            assert count == 1

    def test_subjects_indexed_alphabetically(self, tmp_path: Path) -> None:
        """The bills_fts.subjects column must be byte-stable across re-runs."""
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        _write_jsonl(
            storage_dir,
            [_envelope_for(_detail_payload("detail_119_hr_1.json"))],
        )
        # Subjects in deliberately non-alphabetical order.
        subjects_payload = {
            "subjects": {
                "legislativeSubjects": [
                    {"name": "Zebra studies"},
                    {"name": "Apples"},
                    {"name": "Mangoes"},
                ],
                "policyArea": {"name": "Energy"},
            },
            "pagination": {"count": 3},
        }
        (storage_dir / enrichment_jsonl_name("subjects")).write_text(
            json.dumps(_envelope_for_section(subjects_payload, bill_key=(119, "hr", 1))) + "\n",
            encoding="utf-8",
        )
        load_bills(storage_dir=storage_dir, db_path=db)
        index_bills(db_path=db)
        with SqliteStorage(db, load_vec=False) as storage:
            row = storage.connection.execute(
                "SELECT subjects FROM bills_fts WHERE bill_id = ?", ("119-hr-1",)
            ).fetchone()
            assert row["subjects"] == "Apples | Mangoes | Zebra studies"

    def test_short_title_and_subjects_indexed_when_present(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        _write_jsonl(
            storage_dir,
            [_envelope_for(_detail_payload("detail_119_hr_1.json"))],
        )
        # Tier-2: titles + subjects.
        titles_payload = _fixture("titles_119_hr_1.json")
        subjects_payload = _fixture("subjects_119_hr_1.json")
        (storage_dir / enrichment_jsonl_name("titles")).write_text(
            json.dumps(_envelope_for_section(titles_payload, bill_key=(119, "hr", 1))) + "\n",
            encoding="utf-8",
        )
        (storage_dir / enrichment_jsonl_name("subjects")).write_text(
            json.dumps(_envelope_for_section(subjects_payload, bill_key=(119, "hr", 1))) + "\n",
            encoding="utf-8",
        )
        load_bills(storage_dir=storage_dir, db_path=db)
        index_bills(db_path=db)
        with SqliteStorage(db, load_vec=False) as storage:
            # Short title is "Lower Energy Costs Act" — matches "lower".
            rows = storage.connection.execute(
                "SELECT bill_id FROM bills_fts WHERE bills_fts MATCH ?",
                ("lower",),
            ).fetchall()
            assert [r["bill_id"] for r in rows] == ["119-hr-1"]
            # Subjects column contains "Pipelines".
            rows = storage.connection.execute(
                "SELECT bill_id FROM bills_fts WHERE bills_fts MATCH ?",
                ("pipelines",),
            ).fetchall()
            assert [r["bill_id"] for r in rows] == ["119-hr-1"]

    def test_fts_matches_title_and_policy_area(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        _write_jsonl(
            storage_dir,
            [
                _envelope_for(_detail_payload("detail_119_hr_1.json")),
                _envelope_for(_detail_payload("detail_119_hr_22.json")),
            ],
        )
        load_bills(storage_dir=storage_dir, db_path=db)
        index_bills(db_path=db)
        with SqliteStorage(db, load_vec=False) as storage:
            rows = storage.connection.execute(
                "SELECT bill_id FROM bills_fts WHERE bills_fts MATCH ?",
                ("energy",),
            ).fetchall()
            assert [r["bill_id"] for r in rows] == ["119-hr-1"]


class TestLoadOne:
    """Per-bill projection used by the web-initiated enrichment flow (ADR 0016)."""

    def _seed(self, tmp_path: Path) -> tuple[Path, Path]:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        _write_jsonl(
            storage_dir,
            [
                _envelope_for(_detail_payload("detail_119_hr_1.json")),
                _envelope_for(_detail_payload("detail_119_hr_22.json")),
            ],
        )
        return storage_dir, db

    def test_load_one_projects_only_the_named_bill(self, tmp_path: Path) -> None:
        storage_dir, db = self._seed(tmp_path)
        # Tier-2 cosponsors for *both* bills.
        for bill_key in [(119, "hr", 1), (119, "hr", 22)]:
            with (storage_dir / enrichment_jsonl_name("cosponsors")).open(
                "a", encoding="utf-8"
            ) as fh:
                fh.write(
                    json.dumps(
                        _envelope_for_section(
                            _fixture("cosponsors_119_hr_22.json"), bill_key=bill_key
                        )
                    )
                    + "\n"
                )

        # Project hr-1 only via load_one — hr-22 should not appear at all.
        stats = load_one(storage_dir=storage_dir, db_path=db, bill_id="119-hr-1")
        assert stats.bills_written == 1
        assert stats.tier2_bills_updated == 1  # cosponsors for hr-1 only

        with SqliteStorage(db, load_vec=False) as storage:
            assert storage.get_bill("119-hr-1") is not None
            assert storage.get_bill("119-hr-22") is None
            assert len(storage.cosponsors_for_bill("119-hr-1")) == 3

    def test_load_one_idempotent(self, tmp_path: Path) -> None:
        storage_dir, db = self._seed(tmp_path)
        load_one(storage_dir=storage_dir, db_path=db, bill_id="119-hr-1")
        # Second run is a no-op against unchanged JSONL.
        stats = load_one(storage_dir=storage_dir, db_path=db, bill_id="119-hr-1")
        assert stats.bills_written == 1  # UPSERT still touches the row
        with SqliteStorage(db, load_vec=False) as storage:
            (count,) = storage.connection.execute("SELECT COUNT(*) FROM bills").fetchone()
            assert count == 1

    def test_load_one_unknown_bill_is_noop(self, tmp_path: Path) -> None:
        storage_dir, db = self._seed(tmp_path)
        # bill_id not present in bills.jsonl: nothing to project.
        stats = load_one(storage_dir=storage_dir, db_path=db, bill_id="119-hr-9999")
        assert stats.bills_written == 0
        assert stats.tier2_bills_updated == 0

    def test_load_one_invalid_bill_id_raises(self, tmp_path: Path) -> None:
        storage_dir, db = self._seed(tmp_path)
        with pytest.raises(ValueError, match="invalid bill_id"):
            load_one(storage_dir=storage_dir, db_path=db, bill_id="not-a-bill")

    def test_load_one_preserves_other_bill_fetched_at(self, tmp_path: Path) -> None:
        """Loading hr-1 must not touch the cosponsors_fetched_at on hr-22."""
        storage_dir, db = self._seed(tmp_path)
        # Bulk-load both bills + tier-2 for hr-22.
        (storage_dir / enrichment_jsonl_name("cosponsors")).write_text(
            json.dumps(
                _envelope_for_section(
                    _fixture("cosponsors_119_hr_22.json"), bill_key=(119, "hr", 22)
                )
            )
            + "\n",
            encoding="utf-8",
        )
        load_bills(storage_dir=storage_dir, db_path=db)
        with SqliteStorage(db, load_vec=False) as storage:
            before = storage.get_bill("119-hr-22")
            assert before is not None
            stamp = before["cosponsors_fetched_at"]
            assert stamp is not None

        load_one(storage_dir=storage_dir, db_path=db, bill_id="119-hr-1")

        with SqliteStorage(db, load_vec=False) as storage:
            after = storage.get_bill("119-hr-22")
            assert after is not None
            assert after["cosponsors_fetched_at"] == stamp


class TestReindexOne:
    def test_reindex_one_updates_only_the_matching_row(self, tmp_path: Path) -> None:
        storage_dir = tmp_path / "data"
        db = tmp_path / "test.db"
        _write_jsonl(
            storage_dir,
            [
                _envelope_for(_detail_payload("detail_119_hr_1.json")),
                _envelope_for(_detail_payload("detail_119_hr_22.json")),
            ],
        )
        load_bills(storage_dir=storage_dir, db_path=db)
        index_bills(db_path=db)

        # Mutate the bills row directly, then re-index *only* that bill.
        with SqliteStorage(db, load_vec=False) as storage:
            storage.connection.execute(
                "UPDATE bills SET title = ? WHERE bill_id = ?",
                ("Renamed Energy Act", "119-hr-1"),
            )
            storage.connection.commit()

        reindex_one(db_path=db, bill_id="119-hr-1")

        with SqliteStorage(db, load_vec=False) as storage:
            # Updated row appears with the new title.
            rows = storage.connection.execute(
                "SELECT bill_id FROM bills_fts WHERE bills_fts MATCH ?",
                ("renamed",),
            ).fetchall()
            assert [r["bill_id"] for r in rows] == ["119-hr-1"]
            # The untouched row is still present (only one FTS row was touched).
            rows = storage.connection.execute(
                "SELECT bill_id FROM bills_fts ORDER BY bill_id"
            ).fetchall()
            assert [r["bill_id"] for r in rows] == ["119-hr-1", "119-hr-22"]

    def test_reindex_one_missing_bill_is_noop(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        # Bare schema, no rows.
        SqliteStorage(db, load_vec=False).close()
        # No exception; FTS stays empty.
        reindex_one(db_path=db, bill_id="119-hr-9999")
        with SqliteStorage(db, load_vec=False) as storage:
            (count,) = storage.connection.execute("SELECT COUNT(*) FROM bills_fts").fetchone()
            assert count == 0
