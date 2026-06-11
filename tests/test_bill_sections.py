"""Drift tests for the Bill section catalogue (ADR 0025).

The catalogue in ``concord.models.bills`` is data only; each stage keeps
its own per-section behavior map keyed by section name. These tests are
the enforcement that makes that split safe: every per-stage map must
cover the catalogue exactly, and the ``bills`` DDL must carry one
``*_fetched_at`` column per section. Adding a section to the catalogue
without wiring a stage fails here, loudly, instead of at runtime.
"""

from pathlib import Path

from concord.models.bills import BILL_SECTION_NAMES, BILL_SECTIONS, BILL_SECTIONS_BY_NAME
from concord.pipeline.load_bills import _SECTION_PROJECTORS
from concord.scraper.bills import _ENRICHMENT_FETCHERS
from concord.storage import bills as bills_storage
from concord.storage.sqlite import SqliteStorage


class TestCatalogueInternalConsistency:
    """The literal strings in each BillSection record stay self-consistent."""

    def test_names_are_unique(self) -> None:
        assert len(set(BILL_SECTION_NAMES)) == len(BILL_SECTIONS)

    def test_entities_are_unique(self) -> None:
        entities = [s.entity for s in BILL_SECTIONS]
        assert len(set(entities)) == len(entities)

    def test_jsonl_name_derives_from_name(self) -> None:
        for section in BILL_SECTIONS:
            assert section.jsonl_name == f"bill_{section.name}.jsonl"

    def test_fetched_at_column_derives_from_name(self) -> None:
        for section in BILL_SECTIONS:
            assert section.fetched_at_column == f"{section.name}_fetched_at"

    def test_by_name_lookup_covers_all(self) -> None:
        assert set(BILL_SECTIONS_BY_NAME) == set(BILL_SECTION_NAMES)


class TestPerStageMapsCoverCatalogue:
    """Each stage's private per-section map matches the catalogue exactly."""

    def test_scraper_fetchers_match(self) -> None:
        assert set(_ENRICHMENT_FETCHERS) == set(BILL_SECTION_NAMES)

    def test_loader_projectors_match(self) -> None:
        assert set(_SECTION_PROJECTORS) == set(BILL_SECTION_NAMES)

    def test_storage_writer_exists_per_section(self) -> None:
        for section in BILL_SECTIONS:
            assert hasattr(bills_storage, f"replace_{section.name}")

    def test_sqlite_storage_method_exists_per_section(self) -> None:
        for section in BILL_SECTIONS:
            assert hasattr(SqliteStorage, f"replace_bill_{section.name}")


class TestSchemaCoversCatalogue:
    def test_bills_ddl_has_one_fetched_at_column_per_section(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        with SqliteStorage(db, load_vec=False) as storage:
            columns = {row[1] for row in storage.connection.execute("PRAGMA table_info(bills)")}
        for section in BILL_SECTIONS:
            assert section.fetched_at_column in columns
