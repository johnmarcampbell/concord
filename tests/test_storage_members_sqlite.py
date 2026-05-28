"""Tests for the Members/member_terms SQLite storage layer.

Covers the projection of typed :class:`Member` + :class:`Term` records
into the new schema, plus the UPSERT-on-bioguide + DELETE-then-INSERT
contract that keeps the SQL state consistent with the latest snapshot.
"""

import sqlite3
from pathlib import Path

import pytest

from concord.models import Member, Term
from concord.storage.sqlite import SqliteStorage


def _member(bioguide_id: str = "O000172", **overrides: object) -> Member:
    defaults: dict[str, object] = {
        "bioguide_id": bioguide_id,
        "first_name": "Alexandria",
        "last_name": "Ocasio-Cortez",
        "display_name": "Alexandria Ocasio-Cortez",
        "photo_url": "https://example.invalid/o000172.jpg",
        "birth_year": 1989,
    }
    defaults.update(overrides)
    return Member(**defaults)  # type: ignore[arg-type]


def _term(
    bioguide_id: str = "O000172",
    *,
    congress: int = 119,
    chamber: str = "house",
    state: str = "NY",
    district: int | None = 14,
    party: str | None = "Democratic",
    start_date: str | None = "2025-01-01",
    end_date: str | None = None,
) -> Term:
    return Term(
        bioguide_id=bioguide_id,
        congress=congress,
        chamber=chamber,  # type: ignore[arg-type]
        state=state,
        district=district,
        party=party,
        start_date=start_date,
        end_date=end_date,
    )


@pytest.fixture
def storage(tmp_path: Path) -> SqliteStorage:
    s = SqliteStorage(tmp_path / "test.db", load_vec=False)
    yield s
    s.close()


class TestUpsertMember:
    def test_inserts_member_and_terms(self, storage: SqliteStorage) -> None:
        storage.upsert_member(
            _member(),
            [
                _term(congress=117, end_date="2023-01-01"),
                _term(congress=118, end_date="2025-01-01"),
                _term(congress=119),  # current
            ],
            fetched_at="2026-05-25T14:02:11+00:00",
        )

        row = storage.get_member("O000172")
        assert row is not None
        assert row["display_name"] == "Alexandria Ocasio-Cortez"
        assert row["fetched_at"] == "2026-05-25T14:02:11+00:00"

        terms = storage.terms_for_member("O000172")
        assert {t["congress"] for t in terms} == {117, 118, 119}
        for t in terms:
            assert t["chamber"] == "house"
            assert t["state"] == "NY"
            assert t["district"] == 14

    def test_upsert_replaces_member_row(self, storage: SqliteStorage) -> None:
        storage.upsert_member(
            _member(display_name="Old Name"),
            [_term()],
            fetched_at="2026-01-01T00:00:00+00:00",
        )
        storage.upsert_member(
            _member(display_name="New Name"),
            [_term()],
            fetched_at="2026-05-25T00:00:00+00:00",
        )
        row = storage.get_member("O000172")
        assert row is not None
        assert row["display_name"] == "New Name"
        assert row["fetched_at"] == "2026-05-25T00:00:00+00:00"

    def test_terms_are_replaced_not_appended(self, storage: SqliteStorage) -> None:
        """A second load must not duplicate Term rows for the same Member."""
        storage.upsert_member(
            _member(),
            [_term(congress=118, end_date="2025-01-01"), _term(congress=119)],
            fetched_at="2026-05-01T00:00:00+00:00",
        )
        # Second load: API now reports only the current Congress.
        storage.upsert_member(
            _member(),
            [_term(congress=119)],
            fetched_at="2026-05-25T00:00:00+00:00",
        )
        terms = storage.terms_for_member("O000172")
        assert [t["congress"] for t in terms] == [119]

    def test_senator_has_null_district(self, storage: SqliteStorage) -> None:
        storage.upsert_member(
            _member(bioguide_id="S000033", last_name="Sanders", display_name="Bernard Sanders"),
            [_term(bioguide_id="S000033", chamber="senate", state="VT", district=None)],
            fetched_at="2026-05-25T00:00:00+00:00",
        )
        terms = storage.terms_for_member("S000033")
        assert terms[0]["district"] is None

    def test_chamber_check_constraint(self, storage: SqliteStorage) -> None:
        """SQLite CHECK constraint rejects unknown chamber values.

        Bypasses the pydantic validator (which would normalize ``"foo"``)
        by writing the bad value via raw SQL.
        """
        storage.connection.execute(
            "INSERT INTO members (bioguide_id, first_name, last_name, display_name, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("X000001", "X", "Y", "X Y", "2026-05-25T00:00:00+00:00"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            storage.connection.execute(
                "INSERT INTO member_terms (bioguide_id, congress, chamber, state) "
                "VALUES (?, ?, ?, ?)",
                ("X000001", 119, "bogus", "VT"),
            )


class TestMembersFtsTable:
    def test_table_exists_and_is_writeable(self, storage: SqliteStorage) -> None:
        """The ``index members`` stage will populate this; here we just
        sanity-check the schema is queryable."""
        storage.connection.execute(
            "INSERT INTO members_fts"
            "(bioguide_id, direct_order_name, inverted_order_name, last_name) "
            "VALUES (?, ?, ?, ?)",
            ("O000172", "Alexandria Ocasio-Cortez", "Ocasio-Cortez, Alexandria", "Ocasio-Cortez"),
        )
        rows = storage.connection.execute(
            "SELECT bioguide_id FROM members_fts WHERE members_fts MATCH ?",
            ("Ocasio",),
        ).fetchall()
        assert [r["bioguide_id"] for r in rows] == ["O000172"]
