# 0002 — JSONL as the canonical raw store

**Status**: Accepted, 2026-05-24.

## Context

Concord's scraper produces one `Proceeding` per article in a date range. That output has to live somewhere. The downstream pipeline (index, embed, eventually enrich) consumes it; the web layer indirectly serves it. The choice of "what's the source of truth for the data" gates all of those.

Two shapes were considered:

1. **JSONL** — one Proceeding per line in an append-only file. The pipeline reads it; downstream stages derive everything else.
2. **Database as canonical** — write directly into SQLite (or any other DB), no intermediate file. The DB *is* the source of truth.

## Decision

The scraper writes JSONL. JSONL is the canonical raw store. Every other store (SQLite for indexing/search, future entity/graph tables, exports) is *derived* and rebuildable from this file.

## Consequences

**Trade-offs accepted:**

- Two writes per record on a full pipeline run: once to JSONL, once to the derived store. The duplication is fine — JSONL is cheap to write, and the second write is a regenerable index, not duplicated source-of-truth state.
- Slightly more bytes on disk (raw JSONL + derived SQLite). At our scale (~3 GB raw for a full 30-year corpus, ~5–6 GB derived) this is operationally irrelevant.

**Things this buys:**

- **Every derived store is rebuildable from scratch** without re-scraping. Changing the SQLite schema, the chunking strategy, the embedding model, the NER pipeline, or the entity vocabulary becomes a re-run, not a re-collect. Re-scraping is slow (network-bound, rate-limited) and tedious; re-deriving is fast and free.
- **The scraper has one job and one output.** No coupling between scraping and any choice the derived store eventually makes.
- **Portable.** JSONL is grep-able, splittable, streamable, version-control-friendly (in chunks), and consumable by every tool in the Python data ecosystem (pandas, polars, DuckDB, jq). The dataset can be shared as-is without distributing a database file.
- **A natural recovery point.** If the derived store is ever corrupted or schema-broken, JSONL stays intact and re-derivation is mechanical.

**What stays open:**

- Eventually the JSONL file gets large (~3 GB) and a single-file format gets awkward to manipulate. Sharding by year (`proceedings/year=2024.jsonl`) is a non-breaking change if it ever matters.
- If a future use case wants random-access into the raw store, a sidecar index (`granule_id → byte_offset`) is straightforward to add without changing the canonical format.

## Rejected: database as canonical

Putting the source of truth in SQLite would have collapsed two stores into one. It was rejected because every downstream architectural change would then risk the canonical data — schema migrations, ORM upgrades, accidental DDL — and re-collecting from the live API is too expensive an undo button. JSONL as a separate layer is a small price to keep the source of truth out of the blast radius of derived-store decisions.
