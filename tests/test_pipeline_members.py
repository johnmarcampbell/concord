"""Tests for the Members Stage 1 (load) and Stage 2 (index) pipelines.

The natural key for a Member snapshot is the composite
``(bioguide_id, congress)``. Each test writes envelopes whose ``key``
carries both, then asserts that the loader produces one Term row per
(bioguide_id, congress) cell — even when the underlying payload is
identical across Congresses (the central scrape-time bug this test
module exists to lock in).
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from concord.pipeline.index_members import index as index_members
from concord.pipeline.load_members import load as load_members
from concord.storage.sqlite import SqliteStorage
from tests._snapshots import wrap_snapshot

FIXED_FETCHED_AT = datetime(2026, 5, 25, 14, 2, 11, tzinfo=UTC)


def _fixture(name: str) -> dict[str, Any]:
    here = Path(__file__).parent / "fixtures" / "api" / "members"
    return json.loads((here / name).read_text())


def _write_jsonl(path: Path, envelopes: list[dict[str, Any]]) -> None:
    path.write_text(
        "\n".join(json.dumps(e) for e in envelopes) + "\n",
        encoding="utf-8",
    )


def _envelope_for(
    payload: dict[str, Any],
    *,
    congress: int,
    fetched_at: datetime = FIXED_FETCHED_AT,
) -> dict[str, Any]:
    return wrap_snapshot(
        payload,
        fetched_at=fetched_at,
        key={"bioguide_id": payload["bioguideId"], "congress": congress},
    )


def _failure_rows(db: Path) -> list[tuple[str, str]]:
    """Read ``(entity, entity_key)`` from the production validation_failures table."""
    with SqliteStorage(db, load_vec=False) as storage:
        cursor = storage.connection.execute(
            "SELECT entity, entity_key FROM validation_failures ORDER BY entity, entity_key"
        )
        return [(r["entity"], r["entity_key"]) for r in cursor.fetchall()]


class TestLoadMembers:
    def test_one_snapshot_yields_one_term(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "members.jsonl"
        db = tmp_path / "test.db"
        payload = _fixture("current_house.json")["members"][0]
        _write_jsonl(jsonl, [_envelope_for(payload, congress=119)])

        stats = load_members(jsonl_path=jsonl, db_path=db)
        assert stats.members_written == 1
        assert stats.terms_written == 1

        with SqliteStorage(db, load_vec=False) as storage:
            row = storage.get_member("O000172")
            assert row is not None
            assert row["display_name"] == "Alexandria Ocasio-Cortez"
            terms = storage.terms_for_member("O000172")
            assert [t["congress"] for t in terms] == [119]

    def test_three_congresses_yield_three_terms(self, tmp_path: Path) -> None:
        """The central regression: same payload, different Congresses,
        three distinct Term rows."""
        jsonl = tmp_path / "members.jsonl"
        db = tmp_path / "test.db"
        payload = _fixture("current_house.json")["members"][0]
        _write_jsonl(
            jsonl,
            [
                _envelope_for(payload, congress=117),
                _envelope_for(payload, congress=118),
                _envelope_for(payload, congress=119),
            ],
        )

        stats = load_members(jsonl_path=jsonl, db_path=db)
        assert stats.members_written == 1
        assert stats.terms_written == 3

        with SqliteStorage(db, load_vec=False) as storage:
            terms = storage.terms_for_member("O000172")
            assert {t["congress"] for t in terms} == {117, 118, 119}
            for t in terms:
                assert t["chamber"] == "house"
                assert t["state"] == "NY"
                assert t["district"] == 14

    def test_chamber_switch_resolves_by_congress(self, tmp_path: Path) -> None:
        """Sanders' payload has both House (1991-2007) and Senate (2007-now)
        items. The right chamber is picked by year range per Congress."""
        jsonl = tmp_path / "members.jsonl"
        db = tmp_path / "test.db"
        payload = _fixture("current_senate.json")["members"][0]
        _write_jsonl(
            jsonl,
            [
                _envelope_for(payload, congress=102),  # in House
                _envelope_for(payload, congress=119),  # in Senate
            ],
        )

        load_members(jsonl_path=jsonl, db_path=db)
        with SqliteStorage(db, load_vec=False) as storage:
            terms = storage.terms_for_member("S000033")
            chambers_by_congress = {t["congress"]: t["chamber"] for t in terms}
            assert chambers_by_congress == {102: "house", 119: "senate"}

    def test_latest_snapshot_wins_per_cell(self, tmp_path: Path) -> None:
        """Two snapshots for the same (member, congress) — newer fetched_at
        wins for that cell."""
        jsonl = tmp_path / "members.jsonl"
        db = tmp_path / "test.db"
        payload = _fixture("current_house.json")["members"][0]
        older = _envelope_for(
            {**payload, "directOrderName": "OLD NAME"},
            congress=119,
            fetched_at=FIXED_FETCHED_AT - timedelta(days=30),
        )
        newer = _envelope_for(payload, congress=119)
        _write_jsonl(jsonl, [older, newer])

        load_members(jsonl_path=jsonl, db_path=db)
        with SqliteStorage(db, load_vec=False) as storage:
            row = storage.get_member("O000172")
            assert row is not None
            assert row["display_name"] == "Alexandria Ocasio-Cortez"

    def test_terms_are_not_duplicated_on_reload(self, tmp_path: Path) -> None:
        """Re-loading the same envelopes produces the same final state."""
        jsonl = tmp_path / "members.jsonl"
        db = tmp_path / "test.db"
        payload = _fixture("current_house.json")["members"][0]
        envelope = _envelope_for(payload, congress=119)
        _write_jsonl(jsonl, [envelope, envelope, envelope])

        load_members(jsonl_path=jsonl, db_path=db)

        with SqliteStorage(db, load_vec=False) as storage:
            terms = storage.terms_for_member("O000172")
            assert len(terms) == 1  # one (member, congress) cell

    def test_legacy_envelope_without_congress_is_malformed(self, tmp_path: Path) -> None:
        """An envelope from before the composite-key migration lacks
        ``congress`` in the key. Count it as malformed (and warn) rather
        than guess the queried Congress from the payload."""
        jsonl = tmp_path / "members.jsonl"
        db = tmp_path / "test.db"
        payload = _fixture("current_house.json")["members"][0]
        legacy = {
            "fetched_at": FIXED_FETCHED_AT.isoformat(),
            "key": {"bioguide_id": "O000172"},  # no congress
            "payload": payload,
        }
        jsonl.write_text(json.dumps(legacy) + "\n", encoding="utf-8")

        stats = load_members(jsonl_path=jsonl, db_path=db)
        assert stats.malformed == 1
        assert stats.members_written == 0

    def test_malformed_lines_counted_not_fatal(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "members.jsonl"
        db = tmp_path / "test.db"
        payload = _fixture("current_senate.json")["members"][0]
        good = json.dumps(_envelope_for(payload, congress=119))
        jsonl.write_text(
            "\n".join(
                [
                    "{not valid json",
                    '{"missing": "envelope-keys"}',
                    good,
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        stats = load_members(jsonl_path=jsonl, db_path=db)
        assert stats.malformed == 2
        assert stats.members_written == 1

    def test_multiple_members_loaded(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "members.jsonl"
        db = tmp_path / "test.db"
        house = _fixture("current_house.json")["members"][0]
        senate = _fixture("current_senate.json")["members"][0]
        _write_jsonl(
            jsonl,
            [
                _envelope_for(house, congress=119),
                _envelope_for(senate, congress=119),
            ],
        )

        stats = load_members(jsonl_path=jsonl, db_path=db)
        assert stats.members_written == 2

        with SqliteStorage(db, load_vec=False) as storage:
            assert storage.get_member("O000172") is not None
            assert storage.get_member("S000033") is not None

    def test_payload_without_matching_term_skips_only_that_cell(self, tmp_path: Path) -> None:
        """If the API listed a Member for Congress N but their terms.item
        doesn't cover N, log + skip the Term row (don't 500)."""
        jsonl = tmp_path / "members.jsonl"
        db = tmp_path / "test.db"
        payload = _fixture("historical.json")["members"][0]
        # Jeffords' terms end in 2007 — Congress 119 (2025-2027) falls
        # outside any term's range. Member identity still gets stored.
        _write_jsonl(jsonl, [_envelope_for(payload, congress=119)])

        stats = load_members(jsonl_path=jsonl, db_path=db)
        assert stats.members_written == 1
        assert stats.terms_written == 0


class TestLoadValidationFailures:
    """Class-(b) model-parse failures land in validation_failures (ADR 0023)."""

    def test_term_and_member_failures_recorded(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "members.jsonl"
        db = tmp_path / "test.db"
        aoc = _fixture("current_house.json")["members"][0]  # O000172
        sanders = _fixture("current_senate.json")["members"][0]  # S000033
        # AOC queried for Congress 102 (1991) — no terms.item covers it, so
        # Term.from_congress_api raises ValueError; her Member identity still parses.
        term_fail = _envelope_for(aoc, congress=102)
        # Sanders payload with directOrderName stripped — Member.from_congress_api
        # raises KeyError; the Term half still parses (and is dropped with the member).
        sanders_broken = {k: v for k, v in sanders.items() if k != "directOrderName"}
        member_fail = _envelope_for(sanders_broken, congress=119)
        _write_jsonl(jsonl, [term_fail, member_fail])

        stats = load_members(jsonl_path=jsonl, db_path=db)
        # AOC's identity is written; Sanders is dropped on the member failure.
        assert stats.members_written == 1
        # Both class-(b) failures flow into malformed (option C).
        assert stats.malformed == 2

        with SqliteStorage(db, load_vec=False) as storage:
            rows = storage.connection.execute(
                "SELECT entity, entity_key, source_file, field_path FROM validation_failures "
                "ORDER BY entity"
            ).fetchall()
        assert [(r["entity"], r["entity_key"]) for r in rows] == [
            ("member", "S000033"),
            ("term", "O000172/102"),
        ]
        assert {r["source_file"] for r in rows} == {"members.jsonl"}

    def test_failures_are_idempotent(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "members.jsonl"
        db = tmp_path / "test.db"
        aoc = _fixture("current_house.json")["members"][0]
        _write_jsonl(jsonl, [_envelope_for(aoc, congress=102)])

        load_members(jsonl_path=jsonl, db_path=db)
        load_members(jsonl_path=jsonl, db_path=db)
        # Re-running does not double the row (replace-on-load, not append).
        assert _failure_rows(db) == [("term", "O000172/102")]

    def test_clean_reload_converges_away_stale_rows(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "members.jsonl"
        db = tmp_path / "test.db"
        aoc = _fixture("current_house.json")["members"][0]
        # First load: a term failure (Congress 102).
        _write_jsonl(jsonl, [_envelope_for(aoc, congress=102)])
        load_members(jsonl_path=jsonl, db_path=db)
        assert _failure_rows(db) == [("term", "O000172/102")]
        # Re-scrape fixed the data: now a Congress the terms cover. The mirror
        # table must converge to zero rows.
        _write_jsonl(jsonl, [_envelope_for(aoc, congress=119)])
        load_members(jsonl_path=jsonl, db_path=db)
        assert _failure_rows(db) == []


class TestIndexMembers:
    def test_populates_fts(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "members.jsonl"
        db = tmp_path / "test.db"
        house = _fixture("current_house.json")["members"][0]
        senate = _fixture("current_senate.json")["members"][0]
        _write_jsonl(
            jsonl,
            [
                _envelope_for(house, congress=119),
                _envelope_for(senate, congress=119),
            ],
        )
        load_members(jsonl_path=jsonl, db_path=db)

        stats = index_members(db_path=db)
        assert stats.indexed_members == 2

        with SqliteStorage(db, load_vec=False) as storage:
            rows = storage.connection.execute(
                "SELECT bioguide_id FROM members_fts WHERE members_fts MATCH ?",
                ("Sanders",),
            ).fetchall()
            assert [r["bioguide_id"] for r in rows] == ["S000033"]

    def test_idempotent(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "members.jsonl"
        db = tmp_path / "test.db"
        payload = _fixture("current_house.json")["members"][0]
        _write_jsonl(jsonl, [_envelope_for(payload, congress=119)])
        load_members(jsonl_path=jsonl, db_path=db)

        first = index_members(db_path=db)
        second = index_members(db_path=db)
        assert first.indexed_members == 1
        assert second.indexed_members == 1

        with SqliteStorage(db, load_vec=False) as storage:
            (count,) = storage.connection.execute("SELECT COUNT(*) FROM members_fts").fetchone()
            assert count == 1
