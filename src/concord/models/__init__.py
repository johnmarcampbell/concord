"""Pydantic models for Concord.

Every value that flows between the API client, text fetcher, and storage
layer is one of these types. API JSON is parsed *into* these models at the
network boundary via the ``from_congress_api`` classmethod on each model;
storage writes serialize *from* these models. Nothing in the pipeline
handles untyped dicts.

When a payload violates the expected contract, ``from_congress_api`` raises
``pydantic.ValidationError`` (for field-shape failures) or ``ValueError``
(for our own pre-projection guards). Callers should catch the failure, log
the offending payload, and continue — never silently drop.

Sub-modules:

- :mod:`._common` — shared ``Chamber`` / ``SessionNumber`` types + helpers.
- :mod:`.proceedings` — Issue, Article, Proceeding.
- :mod:`.members` — Member, Term, MemberSnapshot.
- :mod:`.bills` — Bill, BillSnapshot, and the five tier-2 child models.
- :mod:`.votes` — Vote, VotePosition, and the Senate-XML intermediates.
"""

from concord.models._common import Chamber, SessionNumber
from concord.models.bills import (
    Bill,
    BillAction,
    BillSnapshot,
    BillSubject,
    BillSummary,
    BillTitle,
    Cosponsor,
    bill_id_from_components,
)
from concord.models.members import Member, MemberSnapshot, Term, normalize_state
from concord.models.proceedings import Article, Issue, Proceeding, parse_granule_id
from concord.models.votes import (
    ParsedVoteDetail,
    ParsedVotePosition,
    Vote,
    VoteKind,
    VotePosition,
    VotePositionsSnapshot,
    VoteSnapshot,
    VoteThreshold,
    amendment_id_from_components,
    parse_vote_threshold,
    vote_id_from_components,
)

__all__ = [
    "Article",
    "Bill",
    "BillAction",
    "BillSnapshot",
    "BillSubject",
    "BillSummary",
    "BillTitle",
    "Chamber",
    "Cosponsor",
    "Issue",
    "Member",
    "MemberSnapshot",
    "ParsedVoteDetail",
    "ParsedVotePosition",
    "Proceeding",
    "SessionNumber",
    "Term",
    "Vote",
    "VoteKind",
    "VotePosition",
    "VotePositionsSnapshot",
    "VoteSnapshot",
    "VoteThreshold",
    "amendment_id_from_components",
    "bill_id_from_components",
    "normalize_state",
    "parse_granule_id",
    "parse_vote_threshold",
    "vote_id_from_components",
]
