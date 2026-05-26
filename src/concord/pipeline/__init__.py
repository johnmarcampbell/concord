"""Stage 1 (Load) and Stage 2 (Index) pipelines, per-entity.

Each entity type gets its own loader and indexer module:

- :mod:`concord.pipeline.load_proceedings` / :mod:`concord.pipeline.index_proceedings`
- :mod:`concord.pipeline.load_members` / :mod:`concord.pipeline.index_members`

See ADR 0007 for the per-entity layout rationale.
"""
