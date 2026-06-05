"""The web layer's route-registration seam is uniform (issue #114).

Every sibling module that ``create_app`` wires exposes a single
``register(...)`` callable — no ``register_*_routes`` free functions left
over from the pre-decomposition layout. This pins the convention so a new
route module can't silently reintroduce the old shape.

Importing the modules also resolves their cross-module helper imports
(e.g. ``routes_bills`` pulls ``compute_enrichment_state`` from
``enrichment``), so re-privatising those names would fail this test at
import time. Behaviour of the routes themselves is covered by the
``test_web_*`` integration suites.
"""

import importlib

import pytest

#: Every ``concord.web`` submodule that exposes a ``register`` seam called
#: by ``create_app``. Keep in sync with the call sequence in
#: ``app.create_app``.
_REGISTER_SEAM_MODULES = (
    "brief",
    "enrichment",
    "filters",
    "routes_bills",
    "routes_members",
    "routes_meta",
    "routes_proceedings",
    "routes_search",
    "routes_votes",
)


@pytest.mark.parametrize("module_name", _REGISTER_SEAM_MODULES)
def test_module_exposes_register_seam(module_name: str) -> None:
    module = importlib.import_module(f"concord.web.{module_name}")
    assert callable(module.register)
