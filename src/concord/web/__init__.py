"""Web layer: public-facing search demo.

See :doc:`docs/plans/web-layer` for the design and
:doc:`docs/adr/0001-python-end-to-end-for-the-web-layer` for why this lives
in the same Python process as the pipeline.

Per ADR 0022 this package re-exports nothing — import from the defining
submodule (``from concord.web.app import create_app``).
"""
