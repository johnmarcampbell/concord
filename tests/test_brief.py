"""Unit tests for Bill Brief generation (ADR 0020) and its record table."""

from pathlib import Path
from typing import Any

import pytest

from concord.brief import (
    BRIEF_PROMPT_VERSION,
    DEFAULT_BRIEF_MODEL,
    Briefer,
    BriefError,
    BriefFacts,
    build_facts,
    facts_hash,
)
from concord.models import BillDetail
from concord.storage.sqlite import SqliteStorage

_NEUTRAL_JSON = '{"executive_summary": "A neutral summary of the bill."}'


# -- OpenAI-chat-shaped stub --------------------------------------------------


class _Msg:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str | None) -> None:
        self.message = _Msg(content)


class _ChatResponse:
    def __init__(self, content: str | None) -> None:
        self.choices = [_Choice(content)]


class _RecordingCompletions:
    """Records each create() call and returns canned content (or raises)."""

    def __init__(self, content: str | None = _NEUTRAL_JSON, error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def create(self, *, model: str, messages: Any, response_format: Any, temperature: float) -> Any:
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "response_format": response_format,
                "temperature": temperature,
            }
        )
        if self.error is not None:
            raise self.error
        return _ChatResponse(self.content)


class _Chat:
    def __init__(self, completions: _RecordingCompletions) -> None:
        self.completions = completions


class _StubChatClient:
    def __init__(self, content: str | None = _NEUTRAL_JSON, error: Exception | None = None) -> None:
        self.chat = _Chat(_RecordingCompletions(content, error))


def _sample_facts(**overrides: Any) -> BriefFacts:
    base: dict[str, Any] = {
        "bill_id": "119-hr-1",
        "identifier": "HR 1",
        "title": "Lower Energy Costs Act",
        "congress": 119,
        "origin_chamber": "House",
        "sponsor_display_name": "Steve Scalise",
        "policy_area": "Energy",
        "introduced_date": "2025-01-09",
        "latest_action_date": "2026-03-30",
        "latest_action_text": "Became Public Law.",
        "cosponsor_count": 3,
        "original_cosponsor_count": 2,
        "withdrawn_cosponsor_count": 1,
        "cosponsor_party_counts": {"R": 2, "D": 1},
        "subjects": ["Energy", "Pipelines"],
        "action_count": 5,
        "vote_count": 2,
        "latest_summary_stage": "Introduced in House",
        "latest_summary_date": "2025-01-09",
        "latest_summary_text": "Requires lower energy costs.",
    }
    base.update(overrides)
    return BriefFacts(**base)


# -- build_facts --------------------------------------------------------------


class TestBuildFacts:
    def test_counts_and_identifier(self) -> None:
        bill = {
            "bill_id": "119-hr-1",
            "bill_type": "hr",
            "bill_number": 1,
            "title": "Lower Energy Costs Act",
            "congress": 119,
            "origin_chamber": "House",
            "sponsor_display_name": "Steve Scalise",
            "policy_area": "Energy",
            "introduced_date": "2025-01-09",
            "latest_action_date": "2026-03-30",
            "latest_action_text": "Became Public Law.",
        }
        cosponsors = [
            {"bioguide_id": "A", "is_original_cosponsor": 1, "sponsorship_withdrawn_date": None},
            {"bioguide_id": "B", "is_original_cosponsor": 0, "sponsorship_withdrawn_date": None},
            {
                "bioguide_id": "C",
                "is_original_cosponsor": 0,
                "sponsorship_withdrawn_date": "2025-03",
            },
        ]
        facts = build_facts(
            bill=bill,
            cosponsors=cosponsors,
            cosponsor_party_counts={"R": 2, "D": 1},
            subjects=["Energy"],
            action_count=4,
            vote_count=1,
            latest_summary={
                "version_code": "00",
                "action_desc": "Introduced in House",
                "action_date": "2025-01-09",
                "summary_text": "<p>Requires <b>lower</b> energy costs.</p>",
            },
        )
        assert facts.identifier == "HR 1"
        assert facts.cosponsor_count == 3
        assert facts.original_cosponsor_count == 1
        assert facts.withdrawn_cosponsor_count == 1
        assert facts.latest_summary_stage == "Introduced in House"
        # HTML is stripped from the CRS summary before it reaches the model.
        assert "<" not in (facts.latest_summary_text or "")
        assert "lower" in (facts.latest_summary_text or "")

    def test_no_summary(self) -> None:
        bill = {
            "bill_id": "119-hr-9",
            "bill_type": "hr",
            "bill_number": 9,
            "title": "Some Bill",
            "congress": 119,
            "origin_chamber": "House",
        }
        facts = build_facts(
            bill=bill,
            cosponsors=[],
            cosponsor_party_counts={},
            subjects=[],
            action_count=0,
            vote_count=0,
            latest_summary=None,
        )
        assert facts.latest_summary_stage is None
        assert facts.latest_summary_text is None
        assert facts.cosponsor_count == 0


# -- facts_hash ---------------------------------------------------------------


class TestFactsHash:
    def test_stable_for_same_inputs(self) -> None:
        f = _sample_facts()
        h1 = facts_hash(f, model="m", prompt_version=1)
        h2 = facts_hash(f, model="m", prompt_version=1)
        assert h1 == h2

    def test_changes_with_facts(self) -> None:
        a = facts_hash(_sample_facts(), model="m", prompt_version=1)
        b = facts_hash(_sample_facts(cosponsor_count=99), model="m", prompt_version=1)
        assert a != b

    def test_changes_with_model_and_prompt_version(self) -> None:
        f = _sample_facts()
        assert facts_hash(f, model="m1", prompt_version=1) != facts_hash(
            f, model="m2", prompt_version=1
        )
        assert facts_hash(f, model="m", prompt_version=1) != facts_hash(
            f, model="m", prompt_version=2
        )


# -- Briefer.generate ---------------------------------------------------------


class TestBrieferGenerate:
    def test_parses_json_and_uses_model(self) -> None:
        client = _StubChatClient('{"executive_summary": "It does X and Y."}')
        briefer = Briefer(client)
        out = briefer.generate(_sample_facts())
        assert out.executive_summary == "It does X and Y."
        call = client.chat.completions.calls[0]
        assert call["model"] == DEFAULT_BRIEF_MODEL
        assert call["response_format"] == {"type": "json_object"}

    def test_fact_pack_and_honesty_rules_in_prompt(self) -> None:
        client = _StubChatClient()
        Briefer(client).generate(_sample_facts())
        blob = " ".join(m["content"] for m in client.chat.completions.calls[0]["messages"])
        # Ground-truth facts reach the model.
        assert "Lower Energy Costs Act" in blob
        assert "Requires lower energy costs." in blob
        # Honesty rules are present.
        assert "counterpoint" in blob.lower()
        assert "never invent" in blob.lower()

    def test_lens_passed_as_emphasis(self) -> None:
        client = _StubChatClient()
        Briefer(client).generate(_sample_facts(), lens="Emphasize fiscal impact")
        messages = client.chat.completions.calls[0]["messages"]
        user_msgs = [m["content"] for m in messages if m["role"] == "user"]
        assert any("Emphasize fiscal impact" in m for m in user_msgs)

    def test_neutral_when_no_lens(self) -> None:
        client = _StubChatClient()
        Briefer(client).generate(_sample_facts(), lens=None)
        messages = client.chat.completions.calls[0]["messages"]
        user_msgs = [m["content"] for m in messages if m["role"] == "user"]
        assert any("neutral executive summary" in m for m in user_msgs)

    def test_tolerates_non_json_prose(self) -> None:
        client = _StubChatClient("Just a prose summary, no JSON here.")
        out = Briefer(client).generate(_sample_facts())
        assert out.executive_summary == "Just a prose summary, no JSON here."

    def test_client_error_raises_brieferror_surfacing_the_cause(self) -> None:
        client = _StubChatClient(error=RuntimeError("kaboom: 401 unauthorized"))
        with pytest.raises(BriefError) as excinfo:
            Briefer(client).generate(_sample_facts())
        # The wrapped message must name the underlying cause (type + text)
        # so logs are diagnosable, not just "brief generation failed".
        msg = str(excinfo.value)
        assert "RuntimeError" in msg
        assert "kaboom: 401 unauthorized" in msg
        assert "119-hr-1" in msg
        # And the original exception is chained for traceback/exc_info.
        assert isinstance(excinfo.value.__cause__, RuntimeError)

    def test_empty_content_raises_brieferror(self) -> None:
        client = _StubChatClient("   ")
        with pytest.raises(BriefError):
            Briefer(client).generate(_sample_facts())


# -- bill_briefs record table -------------------------------------------------


def _seed_bill(storage: SqliteStorage) -> None:
    storage.upsert_bill(
        BillDetail(
            bill_id="119-hr-1",
            congress=119,
            bill_type="hr",
            bill_number=1,
            origin_chamber="House",
            title="Lower Energy Costs Act",
            update_date="2026-04-01",
        ),
        fetched_at="2026-05-25T00:00:00+00:00",
    )


def _read_brief(storage: SqliteStorage, lens: str) -> dict[str, Any] | None:
    """Read a bill_briefs row back through the connection (no storage read method)."""
    row = storage.connection.execute(
        "SELECT * FROM bill_briefs WHERE bill_id = ? AND lens = ?",
        ("119-hr-1", lens),
    ).fetchone()
    return dict(row) if row is not None else None


class TestBillBriefStorage:
    def test_round_trip_and_upsert(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        with SqliteStorage(db_path, load_vec=False) as storage:
            _seed_bill(storage)
            assert _read_brief(storage, "") is None
            storage.upsert_bill_brief(
                bill_id="119-hr-1",
                lens="",
                executive_summary="First summary.",
                facts_hash="hash-1",
                model="gpt-4o-mini",
                prompt_version=BRIEF_PROMPT_VERSION,
                generated_at="2026-05-30T00:00:00+00:00",
            )
            row = _read_brief(storage, "")
            assert row is not None
            assert row["executive_summary"] == "First summary."
            # Re-upsert on the same (bill_id, lens) replaces in place.
            storage.upsert_bill_brief(
                bill_id="119-hr-1",
                lens="",
                executive_summary="Second summary.",
                facts_hash="hash-2",
                model="gpt-4o-mini",
                prompt_version=BRIEF_PROMPT_VERSION,
                generated_at="2026-05-30T01:00:00+00:00",
            )
            row2 = _read_brief(storage, "")
            assert row2 is not None
            assert row2["executive_summary"] == "Second summary."
            assert row2["facts_hash"] == "hash-2"

    def test_neutral_and_conditioned_are_distinct(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        with SqliteStorage(db_path, load_vec=False) as storage:
            _seed_bill(storage)
            storage.upsert_bill_brief(
                bill_id="119-hr-1",
                lens="",
                executive_summary="Neutral.",
                facts_hash="h",
                model="m",
                prompt_version=1,
                generated_at="t",
            )
            storage.upsert_bill_brief(
                bill_id="119-hr-1",
                lens="for a fiscal-conservative audience",
                executive_summary="Tailored.",
                facts_hash="h",
                model="m",
                prompt_version=1,
                generated_at="t",
            )
            neutral = _read_brief(storage, "")
            tailored = _read_brief(storage, "for a fiscal-conservative audience")
            assert neutral is not None
            assert neutral["executive_summary"] == "Neutral."
            assert tailored is not None
            assert tailored["executive_summary"] == "Tailored."
