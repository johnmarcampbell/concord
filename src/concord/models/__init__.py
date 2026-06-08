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

Per ADR 0022 this package intentionally re-exports nothing — import each
symbol from the submodule that defines it
(``from concord.models.votes import Vote``), not from ``concord.models``.
"""
