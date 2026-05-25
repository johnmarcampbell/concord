"""Web layer: public-facing search demo.

See :doc:`docs/plans/web-layer` for the design and
:doc:`docs/adr/0001-python-end-to-end-for-the-web-layer` for why this lives
in the same Python process as the pipeline.
"""

from .app import create_app

__all__ = ["create_app"]
