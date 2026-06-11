# 0025 — The Bill section catalogue is data, not a registry

**Status**: Accepted, 2026-06-11. Builds on [ADR 0009](0009-multi-endpoint-entities-split-jsonl.md); deliberately stops short of the registry that [ADR 0007](0007-parallel-pipelines-per-entity.md) rejects.

## Context

The five Bill sections (`cosponsors`, `actions`, `subjects`, `titles`, `summaries`) are the structural definition of the Bill aggregate per ADR 0009, but the knowledge of *which* sections exist had no single home. The name tuple was copied literally in three layers (`scraper.bills.BILL_ENRICHMENT_SECTIONS`, `storage.bills.BILL_TIER2_SECTIONS`, `cli.bills.DEFAULT_BILL_SECTIONS` — three synonyms for one concept), re-derived as a section→entity dict in the Stage 1 loader, and hardcoded as five `*_fetched_at` column names in a Web-layer SQL string. The Web and CLI layers imported the tuple from the Stage 0 scraper module, dragging scrape code into layers that otherwise don't need it. Adding a sixth section meant six synchronized edits with nothing failing loudly on a miss.

## Decision

One catalogue, in `concord.models.bills`: `BILL_SECTIONS`, a tuple of frozen `BillSection` records. Each record carries the names derived from the section — `name` (plural token), `entity` (the singular name persisted in `validation_failures`, ADR 0023), `jsonl_name` (the ADR 0009 sibling file), `fetched_at_column` (the `bills` column its loader stamps) — all spelled out literally, never computed, so every string stays greppable and `summaries`→`summary`-style irregular derivations can't go subtly wrong. Scraper, loader, storage, web, and CLI consult the catalogue; the old per-layer constants are deleted outright (Python imports are best-effort per [ADR 0014](0014-publish-to-pypi-cli-first.md)).

**The catalogue carries names only — never fetchers, writers, projectors, or model classes.** Per-stage behavior stays in each stage module as a private mapping keyed by `BillSection.name` (`_ENRICHMENT_FETCHERS` in the scraper, `_SECTION_PROJECTORS` in the loader, the `replace_*` writers in storage), drift-checked against the catalogue by tests rather than wired through it.

## Rejected: a full section registry

Pointing each catalogue entry at its fetcher/writer/projector (or model class) would make adding a section a one-line edit, but it centralizes stage behavior in a shared module — the first step toward the cross-stage entity registry ADR 0007 explicitly rejects, and it would force every catalogue importer (including the CLI's lazy-import paths) to pull in scraper, storage, and Pydantic machinery. The line is bright on purpose: if you find yourself adding a callable or a class reference to `BillSection`, you are re-litigating ADR 0007, not extending this ADR.

## Rejected: keeping ownership in the scraper

Deleting only the storage/CLI copies and importing from `concord.scraper.bills` everywhere would fix the drift but cement the layering smell (Web and Stage 1 importing structural constants from Stage 0). The catalogue is the *shape* of the Bill aggregate, not a scrape concern; the models package is the one layer everything already imports.
