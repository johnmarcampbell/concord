"""Dependency-free home for exceptions shared across Concord layers.

This module must import nothing outside the standard library so that both
the network clients (e.g. :mod:`concord.senate_xml`) and the domain models
(e.g. :mod:`concord.models.votes`) can import from it without re-creating
the ``models -> senate_xml`` import cycle that issue #123 untangles. See
ADR 0018 for why the senate.gov XML parse lives on the model.
"""


class SenateXmlError(Exception):
    """Raised on a senate.gov LIS failure.

    Two call paths raise it: the client (:mod:`concord.senate_xml`) on a
    non-XML response or transport error, and the model
    (:meth:`concord.models.votes.SenateVoteDetail.from_senate_xml`) on
    malformed or incomplete detail XML.
    """
