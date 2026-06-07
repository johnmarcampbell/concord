"""Import-order regression guard.

The observability ledger (ADR 0021) introduced cross-module edges:
``senate_xml`` imports ``concord.models`` and ``concord.observability``, and
``observability`` imports ``concord.models`` — while ``concord.models.votes``
parses senate XML. A careless top-level import in any of these closes a cycle
that only fires when a particular module is imported *first* (the rest of the
suite imports ``concord.models`` early, masking it). These subprocess imports
each start from a clean interpreter so an order-dependent ``ImportError`` can't
hide behind collection order.
"""

import subprocess
import sys

import pytest

# Every module on the observability<->models<->client cycle, as a first import.
_LEAF_FIRST_IMPORTS = [
    "concord.senate_xml",
    "concord.observability",
    "concord.models",
    "concord.text",
    "concord.api",
]


@pytest.mark.parametrize("module", _LEAF_FIRST_IMPORTS)
def test_module_imports_cleanly_as_first_import(module: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed args, no shell, trusted module list
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"importing {module} first failed (likely a circular import):\n{result.stderr}"
    )
