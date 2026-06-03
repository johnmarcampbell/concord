"""Pydantic models for Concord.

Every value that flows between the API client, text fetcher, and storage
layer is one of these types. API JSON is parsed *into* these models at the
load boundary (Stage 1) via the ``from_congress_api`` classmethod on each
wire-shape model; senate.gov XML uses ``from_senate_xml`` analogously.
Storage writes serialize *from* these models. See ADR 0018 for the
wire-shape / domain model split and the load-boundary validation rule.

When a payload violates the expected contract, ``from_congress_api`` raises
``pydantic.ValidationError`` (for field-shape failures) or ``ValueError``
(for our own pre-projection guards). Callers should catch the failure, log
the offending payload, and continue — never silently drop.

Persistence envelopes are :class:`Snapshot[T]` (ADR 0006 / ADR 0018);
the snapshot validates the envelope shape, the wire-shape model's
``from_congress_api`` validates the payload.

Sub-modules:

- :mod:`._common` — shared ``Chamber`` / ``SessionNumber`` types, helpers,
  and the :class:`Snapshot` envelope generic.
- :mod:`.proceedings` — Issue, Article, Proceeding (predates the envelope
  per ADR 0006; persists flat).
- :mod:`.members` — Member, Term.
- :mod:`.bills` — BillDetail and the five tier-2 child models.
- :mod:`.votes` — Vote, VotePosition (domain models for House);
  HouseVoteMembers, SenateVoteDetail, SenateVotePosition (wire shapes).
"""

from concord.models._common import Chamber, SessionNumber, Snapshot
from concord.models.bills import (
    BillAction,
    BillCosponsor,
    BillDetail,
    BillSubject,
    BillSummary,
    BillTitle,
    bill_id_from_components,
)
from concord.models.members import Member, Term, normalize_state
from concord.models.proceedings import Article, Issue, Proceeding, parse_granule_id
from concord.models.runs import Attempt, RunEvent, RunRecord
from concord.models.votes import (
    HouseVoteMembers,
    SenateVoteDetail,
    SenateVotePosition,
    Vote,
    VoteKind,
    VotePosition,
    VoteThreshold,
    amendment_id_from_components,
    parse_vote_threshold,
    vote_id_from_components,
)

__all__ = [
    "Article",
    "Attempt",
    "BillAction",
    "BillCosponsor",
    "BillDetail",
    "BillSubject",
    "BillSummary",
    "BillTitle",
    "Chamber",
    "HouseVoteMembers",
    "Issue",
    "Member",
    "Proceeding",
    "RunEvent",
    "RunRecord",
    "SenateVoteDetail",
    "SenateVotePosition",
    "SessionNumber",
    "Snapshot",
    "Term",
    "Vote",
    "VoteKind",
    "VotePosition",
    "VoteThreshold",
    "amendment_id_from_components",
    "bill_id_from_components",
    "normalize_state",
    "parse_granule_id",
    "parse_vote_threshold",
    "vote_id_from_components",
]
