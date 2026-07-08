"""Unit tests for the Bill read-side aggregate (issue #153).

``BillAggregate.from_sql`` is the read counterpart to the Stage-1 loader's
write-side rejoin (ADR 0009): one factory that owns every Bill-read query.
These tests cover the populated / missing / partial cases and prove
``assemble_facts(agg)`` reproduces the fact-pack party split that the old
``cosponsor_party_breakdown`` query produced.
"""

from pathlib import Path

from concord.models.bills import (
    BillAction,
    BillCosponsor,
    BillDetail,
    BillSubject,
    BillSummary,
    BillTitle,
)
from concord.models.members import Member, Term
from concord.models.votes import Vote
from concord.storage.sqlite import SqliteStorage
from concord.web.brief import assemble_facts
from concord.web.search import BillAggregate, BillRow, VoteHit

# Section fetched_at stamps: actions is deliberately the lexically-largest
# so the freshness roll-up (updated_at) must pick it over identity + peers.
_BILL_STAMP = "2026-05-25T00:00:00+00:00"
_SECTION_STAMP = "2026-05-26T00:00:00Z"
_NEWEST_STAMP = "2026-05-27T00:00:00Z"


def _member(bioguide_id: str, party: str) -> tuple[Member, list[Term]]:
    return (
        Member(
            bioguide_id=bioguide_id,
            first_name="First",
            last_name=bioguide_id,
            display_name=f"Rep. {bioguide_id}",
        ),
        [
            Term(
                bioguide_id=bioguide_id,
                congress=119,
                chamber="house",
                state="CA",
                district=1,
                party=party,
                start_date="2025-01-03",
                end_date="2027-01-03",
            ),
        ],
    )


def _vote(roll_number: int) -> Vote:
    return Vote(
        vote_id=f"house-119-1-{roll_number}",
        chamber="house",
        congress=119,
        session=1,
        roll_number=roll_number,
        vote_kind="standard",
        start_date=f"2026-04-{roll_number:02d}T18:00:00Z",
        vote_question="On Passage of the Bill",
        vote_type="Yea-and-Nay",
        threshold="simple_majority",
        result="Passed",
        yea_count=220,
        nay_count=210,
        present_count=0,
        not_voting_count=0,
        bill_id="119-hr-1",
        amendment_id=None,
        is_party_unity=False,
        update_date="2026-04-30",
    )


def _seed_full(storage: SqliteStorage) -> None:
    """One fully-enriched Bill: sponsor + three cosponsors (R / D / unindexed),
    all five sections, and two votes."""
    for member, terms in (
        _member("R000001", "Republican"),
        _member("D000001", "Democratic"),
    ):
        storage.upsert_member(member, terms, fetched_at=_BILL_STAMP)
    # "X000001" is an intentional un-indexed cosponsor — a bill_cosponsors row
    # with no matching Member/Term, so its party resolves to None -> "Unknown".
    storage.upsert_bill(
        BillDetail(
            bill_id="119-hr-1",
            congress=119,
            bill_type="hr",
            bill_number=1,
            origin_chamber="House",
            title="Lower Energy Costs Act",
            introduced_date="2025-01-09",
            policy_area="Energy",
            sponsor_bioguide_id="R000001",
            latest_action_date="2026-03-30",
            latest_action_text="Became Public Law.",
            update_date="2026-04-01",
        ),
        fetched_at=_BILL_STAMP,
    )
    storage.replace_bill_cosponsors(
        "119-hr-1",
        [
            BillCosponsor(
                bioguide_id="R000001", sponsorship_date="2025-01-09", is_original_cosponsor=True
            ),
            BillCosponsor(
                bioguide_id="D000001", sponsorship_date="2025-02-01", is_original_cosponsor=False
            ),
            BillCosponsor(
                bioguide_id="X000001", sponsorship_date="2025-02-02", is_original_cosponsor=False
            ),
        ],
        fetched_at=_SECTION_STAMP,
    )
    storage.replace_bill_actions(
        "119-hr-1",
        [
            BillAction(
                action_date="2025-01-09",
                action_text="Introduced in House",
                action_code="Intro-H",
                source_system="House",
            ),
            BillAction(
                action_date="2026-03-30",
                action_text="Became Public Law",
                action_code="36000",
                source_system="Library of Congress",
            ),
        ],
        fetched_at=_NEWEST_STAMP,
    )
    storage.replace_bill_subjects(
        "119-hr-1",
        [BillSubject(name="Energy"), BillSubject(name="Taxation")],
        fetched_at=_SECTION_STAMP,
    )
    storage.replace_bill_titles(
        "119-hr-1",
        [BillTitle(title_type="Official Title", title_text="Lower Energy Costs Act")],
        fetched_at=_SECTION_STAMP,
    )
    storage.replace_bill_summaries(
        "119-hr-1",
        [
            BillSummary(
                version_code="00",
                action_date="2025-01-09",
                action_desc="Introduced in House",
                summary_text="<p>Early summary.</p>",
            ),
            BillSummary(
                version_code="01",
                action_date="2025-06-01",
                action_desc="Reported to House",
                summary_text="<p>Later, richer summary prose.</p>",
            ),
        ],
        fetched_at=_SECTION_STAMP,
    )
    storage.upsert_vote(_vote(1), fetched_at=_SECTION_STAMP)
    storage.upsert_vote(_vote(2), fetched_at=_SECTION_STAMP)


class TestFromSql:
    def test_populated_bill_carries_every_section(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "out.db")
        _seed_full(storage)

        agg = BillAggregate.from_sql(storage.connection, "119-hr-1")

        assert agg is not None
        # The identity row is a typed BillRow, not a bare dict.
        assert isinstance(agg.bill, BillRow)
        assert agg.bill.bill_id == "119-hr-1"
        assert agg.bill.sponsor_display_name == "Rep. R000001"
        assert len(agg.cosponsors) == 3
        assert len(agg.actions) == 2
        assert agg.subjects == ["Energy", "Taxation"]
        assert len(agg.titles) == 1
        assert len(agg.summaries) == 2
        # vote_history is typed all the way down.
        assert len(agg.vote_history) == 2
        assert all(isinstance(v, VoteHit) for v in agg.vote_history)

    def test_cosponsors_carry_latest_term_party(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "out.db")
        _seed_full(storage)

        agg = BillAggregate.from_sql(storage.connection, "119-hr-1")

        assert agg is not None
        party_by_id = {c["bioguide_id"]: c["party"] for c in agg.cosponsors}
        assert party_by_id == {
            "R000001": "Republican",
            "D000001": "Democratic",
            "X000001": None,  # un-indexed cosponsor -> no Term -> None
        }

    def test_updated_at_is_newest_stamp_and_nothing_missing(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "out.db")
        _seed_full(storage)

        agg = BillAggregate.from_sql(storage.connection, "119-hr-1")

        assert agg is not None
        # actions' stamp is the lexically-largest, so it wins the roll-up.
        assert agg.updated_at == _NEWEST_STAMP
        assert agg.any_missing is False

    def test_missing_bill_returns_none(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "out.db")
        _seed_full(storage)

        assert BillAggregate.from_sql(storage.connection, "119-hr-999") is None

    def test_partial_bill_flags_missing_and_empties_sections(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "out.db")
        # Identity only — no section has been enriched yet.
        storage.upsert_bill(
            BillDetail(
                bill_id="119-hr-2",
                congress=119,
                bill_type="hr",
                bill_number=2,
                origin_chamber="House",
                title="Unenriched Bill",
                update_date="2026-04-01",
            ),
            fetched_at=_BILL_STAMP,
        )

        agg = BillAggregate.from_sql(storage.connection, "119-hr-2")

        assert agg is not None
        assert agg.any_missing is True
        assert agg.cosponsors == []
        assert agg.actions == []
        assert agg.subjects == []
        assert agg.titles == []
        assert agg.summaries == []
        assert agg.vote_history == []
        # No section stamps -> the roll-up falls back to the identity stamp.
        assert agg.updated_at == _BILL_STAMP


class TestFromNaturalKey:
    def test_resolves_same_aggregate_as_from_sql(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "out.db")
        _seed_full(storage)

        agg = BillAggregate.from_natural_key(
            storage.connection, congress=119, bill_type="hr", bill_number=1
        )

        assert agg is not None
        assert agg.bill.bill_id == "119-hr-1"
        assert len(agg.cosponsors) == 3

    def test_missing_bill_returns_none(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "out.db")
        _seed_full(storage)

        assert (
            BillAggregate.from_natural_key(
                storage.connection, congress=119, bill_type="hr", bill_number=999
            )
            is None
        )


class TestAssembleFacts:
    def test_party_split_matches_cosponsor_terms(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "out.db")
        _seed_full(storage)
        agg = BillAggregate.from_sql(storage.connection, "119-hr-1")
        assert agg is not None

        facts = assemble_facts(agg)

        # The parity guard for the cosponsor_party_breakdown -> from_sql move:
        # one Republican, one Democrat, one un-indexed (Unknown).
        assert facts.cosponsor_party_counts == {"R": 1, "D": 1, "Unknown": 1}

    def test_counts_and_latest_summary(self, tmp_path: Path) -> None:
        storage = SqliteStorage(tmp_path / "out.db")
        _seed_full(storage)
        agg = BillAggregate.from_sql(storage.connection, "119-hr-1")
        assert agg is not None

        facts = assemble_facts(agg)

        assert facts.bill_id == "119-hr-1"
        assert facts.cosponsor_count == 3
        assert facts.original_cosponsor_count == 1
        assert facts.action_count == 2
        assert facts.vote_count == 2
        assert facts.subjects == ["Energy", "Taxation"]
        # summaries_for_bill returns oldest-first; the latest is the pick.
        assert facts.latest_summary_stage == "Reported to House"
        assert facts.latest_summary_date == "2025-06-01"
        assert facts.latest_summary_text is not None
        assert "<p>" not in facts.latest_summary_text
        assert "Later, richer summary prose." in facts.latest_summary_text
