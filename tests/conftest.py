"""Shared test fixtures and helpers.

Tests load recorded API JSON and article HTML from ``tests/fixtures/`` so the
suite never touches the network. The ``load_fixture`` helper returns the raw
text of a fixture file; ``load_json_fixture`` parses it as JSON.

The suite also never touches the network for *tokenization*. The real
``tiktoken.get_encoding("cl100k_base")`` downloads a BPE vocab on first use,
which fails in a sandboxed/offline environment. ``_offline_tiktoken`` (autouse,
session-scoped) swaps in :class:`_OfflineEncoding`, a lossless stand-in, so any
test that builds a ``Chunker`` runs offline.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import tiktoken

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class _OfflineEncoding:
    """Lossless ~4-chars-per-token stand-in for a real tiktoken encoding.

    The ``Chunker`` only needs two things from an encoding: a token *count*
    that drives chunk boundaries, and ``encode``/``decode`` that round-trip so
    decoded spans can be located in the source text (see ``chunking.py``).

    This tokenizer splits text into fixed 4-character spans — tiktoken's rough
    average for English — so token counts land in the same ballpark as
    ``cl100k_base`` and boundary-preference tests stay meaningful. Every token
    is a verbatim slice of the input, so ``decode(encode(t)) == t`` exactly and
    any contiguous run of tokens decodes to a contiguous substring, which is
    all the Chunker's ``str.find``/``rfind`` round-tripping relies on.
    """

    _WIDTH = 4

    def __init__(self) -> None:
        self._to_id: dict[str, int] = {}
        self._to_str: dict[int, str] = {}

    def encode(self, text: str) -> list[int]:
        ids: list[int] = []
        for i in range(0, len(text), self._WIDTH):
            span = text[i : i + self._WIDTH]
            tid = self._to_id.get(span)
            if tid is None:
                tid = len(self._to_id)
                self._to_id[span] = tid
                self._to_str[tid] = span
            ids.append(tid)
        return ids

    def decode(self, ids: list[int]) -> str:
        return "".join(self._to_str[i] for i in ids)


@pytest.fixture(autouse=True, scope="session")
def _offline_tiktoken():
    """Replace ``tiktoken.get_encoding`` with an offline, network-free stub.

    A fresh :class:`_OfflineEncoding` per call mirrors the real API (each
    ``Chunker`` gets its own encoder) without ever downloading a vocab.
    """
    with patch.object(tiktoken, "get_encoding", lambda _name: _OfflineEncoding()):
        yield


@pytest.fixture
def fixtures_dir() -> Path:
    """Absolute path to ``tests/fixtures/``."""
    return FIXTURES_DIR


@pytest.fixture
def load_fixture():
    """Return a callable that reads a fixture file as text.

    Usage::

        def test_thing(load_fixture):
            html = load_fixture("articles/sample.html")
    """

    def _load(relative_path: str) -> str:
        return (FIXTURES_DIR / relative_path).read_text(encoding="utf-8")

    return _load


@pytest.fixture
def load_json_fixture():
    """Return a callable that reads a fixture file and parses it as JSON."""

    def _load(relative_path: str) -> Any:
        return json.loads((FIXTURES_DIR / relative_path).read_text(encoding="utf-8"))

    return _load
