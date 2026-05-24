"""Shared test fixtures and helpers.

Tests load recorded API JSON and article HTML from ``tests/fixtures/`` so the
suite never touches the network. The ``load_fixture`` helper returns the raw
text of a fixture file; ``load_json_fixture`` parses it as JSON.
"""

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


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
