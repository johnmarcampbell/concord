"""Shared model utilities and type aliases.

Defines the ``Chamber`` / ``SessionNumber`` Literal types used across
multiple entities, the chamber-name normalizer that the API's verbose
forms (``House of Representatives``) demand, and the
:class:`Snapshot` envelope generic (ADR 0006 / ADR 0018).
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

Chamber = Literal["house", "senate"]
SessionNumber = Literal[1, 2]


def normalize_chamber(value: Any) -> Any:
    """Map the API's verbose chamber names to the canonical ``house``/``senate``.

    The API uses ``House of Representatives`` and ``Senate``; tests and SQL
    use the lowercased one-word forms. Pass-through for already-normalized
    values lets callers use either form on input.
    """
    if not isinstance(value, str):
        return value
    lower = value.strip().lower()
    if lower in {"house", "house of representatives"}:
        return "house"
    if lower == "senate":
        return "senate"
    return value


class Snapshot[PayloadT](BaseModel):
    """ADR 0006 snapshot envelope for a mutable entity, parameterized by payload type.

    Wraps one fetch of an upstream resource: ``fetched_at`` is the capture
    timestamp, ``key`` is the natural-key composite used for dedup and
    upsert, and ``payload`` is the wire-shape data (typed by ``PayloadT``).
    For multi-endpoint entities (ADR 0009) each sub-endpoint produces its
    own snapshot stream — e.g. ``Snapshot[BillDetail]`` for the identity
    endpoint, ``Snapshot[BillCosponsor]`` for one cosponsors row.

    Loaders construct snapshots after running the payload through the
    wire-shape model's ``from_congress_api`` factory (or ``from_<source>``
    for non-Congress sources). ``Snapshot[T]`` validates the envelope
    fields; the factory owns the payload-shape transformation. See
    ADR 0018 for the contract.
    """

    model_config = ConfigDict(extra="ignore")

    fetched_at: datetime
    key: dict[str, str | int]
    payload: PayloadT
