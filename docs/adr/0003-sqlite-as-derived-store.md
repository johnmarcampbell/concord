# 0003 — SQLite as the derived store

**Status**: Accepted, 2026-05-24.
**Supersedes** (in practice): the implicit choice of MongoDB carried over from the pre-rebuild project. The Mongo storage backend (#25) remains in the codebase but is no longer the recommended path.

## Context

The pipeline derives an indexed, queryable store from the canonical JSONL (ADR 0002). The original Concord project used MongoDB, chosen primarily as a learning vehicle. With the rebuild's actual access patterns now clearer — fixed schema, full-text search, vector search, simple metadata filters, a future knowledge graph with mostly 1–2 hop joins — the Mongo choice has lost its rationale.

Four options were on the table:

1. **MongoDB** — what we have. Document flexibility (unused), built-in text indexes, requires a server.
2. **PostgreSQL + pgvector + tsvector** — production-grade, full SQL, every feature we'd want, requires a server.
3. **SQLite + FTS5 + sqlite-vec** — single file, no server, full SQL, excellent FTS, embedded vector search.
4. **DuckDB** — columnar OLAP, can read JSONL natively, but lacks first-class FTS and is write-optimized for batches, not row-at-a-time appends.

## Decision

SQLite, with the FTS5 extension for keyword search and the `sqlite-vec` extension for vector search. One database file (`proceedings.db`) holds proceedings, chunks, the FTS5 virtual table, the vector table, and (later) the entity / mention tables.

## Consequences

**Trade-offs accepted:**

- **Single-writer constraint.** SQLite serializes writes. Fine for our workload — the pipeline is the only writer and runs serially anyway.
- **Single-machine ceiling.** SQLite doesn't shard across nodes. We're well under any reasonable ceiling (a 30-year corpus is ~5 GB SQLite file; SQLite handles tens of GB comfortably).
- **`sqlite-vec` is younger than Postgres `pgvector`.** Picking it accepts some maturity risk. Mitigated by the fact that the Storage abstraction lets us swap the vector layer (to LanceDB, to pgvector if we ever go Postgres) without rewriting upstream code.

**Things this buys:**

- **Zero infrastructure.** A single file on disk, no daemon to install/configure/secure on the VPS, no port to open, no replica to manage. On a 4 GB Hostinger KVM1, this matters.
- **Memory footprint stays small.** SQLite memory-maps and uses tens of MB working set regardless of file size. Postgres would consume hundreds of MB of `shared_buffers` for the same workload.
- **Hybrid search is one SQL query.** FTS5 and `sqlite-vec` results both come back as rows; combining them via RRF is a CTE join in pure SQL. No service calls, no orchestration.
- **Operational simplicity.** Backup is `cp proceedings.db`. Local development is `cp` again. Inspecting state is `sqlite3 proceedings.db` from any shell.
- **Escape hatch.** If we ever outgrow SQLite, the schema migrates to Postgres with minimal rewrite — same SQL dialect for 90% of queries, FTS5 → `tsvector`, `sqlite-vec` → `pgvector`. The migration is mechanical, not architectural.

**What stays open:**

- The existing `MongoStorage` adapter (#25) is left in place. It's an alternative backend behind the `Storage` Protocol; not a default, not the recommendation, but it works for users who want it.

## Rejected: Postgres

Better for multi-user serving and richer concurrency, but requires a daemon, consumes much more RAM, and adds operational surface for zero benefit at our scale. Right answer if Concord ever becomes a multi-user web service.

## Rejected: DuckDB

Excellent for ad-hoc analytical queries over the JSONL directly (and remains a recommended *exploration* tool — `duckdb -c "select * from 'proceedings.jsonl'"`), but it's optimized for batch analytics over columnar storage, not the row-at-a-time write workload of an ingest pipeline, and lacks a polished FTS story.

## Rejected: MongoDB (status quo)

Document flexibility is the main reason to pick Mongo; we don't use it. The storage footprint is larger than columnar/SQL options, the FTS quality is below SQLite FTS5 and Postgres `tsvector`, and it requires a server we'd otherwise not need. The original "I want to learn Mongo" rationale is no longer load-bearing.
