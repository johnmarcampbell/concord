"""Stage 0 scrapers, one module per entity type.

Each entity type gets its own scraper that paginates the relevant
``api.congress.gov`` endpoint and appends to a per-entity JSONL file.

- :mod:`concord.scraper.proceedings`
- :mod:`concord.scraper.members`

See ADR 0007 for the per-entity layout rationale.
"""
