"""Shared model utilities and type aliases.

Defines the ``Chamber`` / ``SessionNumber`` Literal types used across
multiple entities, plus the chamber-name normalizer that the API's
verbose forms (``House of Representatives``) demand.
"""

from typing import Any, Literal

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


__all__ = ["Chamber", "SessionNumber", "normalize_chamber"]
