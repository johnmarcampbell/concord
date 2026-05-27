# 0013 — Drop the Mongo storage backend

**Status**: Accepted, 2026-05-27.
**Amends** [ADR 0003](0003-sqlite-as-derived-store.md): the "What stays open" paragraph that preserved `MongoStorage` as an alternative behind the `[mongo]` extra is now closed.

## Context

ADR 0003 made SQLite the recommended derived store but explicitly left `storage/mongo.py` in place as an alternative backend behind the `Storage` Protocol, hidden behind an optional `[mongo]` extra. Six months on, that caveat has aged badly:

- **The "alternative backend" is half-real.** `MongoStorage` only implements the raw-store interface for Proceedings (Stage 0). Members, Bills, and Votes scrape directly into per-entity JSONL files and load straight into `SqliteStorage`; the `Storage` Protocol never enters those code paths. So the package today advertises a Mongo path that covers one of four entity types.
- **No known users.** The backend exists for hypothetical Mongo-shops who might want it. There is no documented user, no example deployment, no migration guide, no integration test against a real Mongo server (the suite uses `mongomock`).
- **Publishing forces honesty.** We're publishing this to PyPI (`concord-congress`). Every advertised extra is a public commitment — pymongo's API drift, security advisories, and the `[mongo]` install path become things we own indefinitely. Owning that for zero users is poor stewardship of attention.
- **CLI surface keeps growing because of it.** `concord scrape proceedings --help` exposes `--mongo-uri`, `--mongo-db`, `--mongo-collection` — three flags none of the other entity scrapers have, all serving the half-implemented Mongo path. The help output is noisier than the supported feature set.
- **ADR 0003's real escape hatch was always Postgres.** The "if Concord ever becomes a multi-user web service" line points at Postgres + pgvector + tsvector, not Mongo. Mongo's continued presence is residue from the pre-rebuild project's "I want to learn Mongo" rationale, which 0003 already retired.

## Decision

Delete `storage/mongo.py`, its tests, the CLI flags that route to it, the `[mongo]` extra, and the `mongomock` dev dependency. Update [ADR 0003](0003-sqlite-as-derived-store.md) to point here. The `Storage` Protocol stays — it's a useful seam even with one current implementation (`JsonlStorage`), and removing it would force a larger refactor of the scraper module for no immediate gain.

## Consequences

**Trade-offs accepted:**

- **One fewer working backend.** Anyone who wanted to point Concord at a Mongo cluster now can't, without restoring the file from git history. Given there are no such users today, the cost is theoretical.
- **The `Storage` Protocol has a single implementation.** Slightly weakens the "this is a real abstraction" story until a second raw-store backend ever lands. Acceptable — the protocol still pulls its weight by making the scraper's storage dependency explicit and test-mockable.
- **ADR 0003's "What stays open" section now contradicts reality.** Handled by amending 0003 with a pointer to this ADR rather than rewriting history. Per the project's ADR convention, ADRs are appended, not edited away.

**Things this buys:**

- **Smaller maintenance surface for PyPI.** No `[mongo]` extra to test against pymongo's release stream, no `mongomock` to keep in sync, no Mongo-specific failure modes to triage.
- **Cleaner CLI.** `concord scrape proceedings --help` loses three flags that only one of four entity scrapers ever had.
- **Honest scope.** The supported path is JSONL → SQLite, full stop. That matches what the pipeline actually does for every entity except Proceedings, and what the README, ADRs, and CLAUDE.md guide every reader toward.
- **Reversibility is cheap.** The deleted files are preserved in git history. If a real Mongo user shows up, `git show <commit>:src/concord/storage/mongo.py` is the starting point for a revert PR. The `Storage` Protocol staying in place means the revert is purely additive.

## Rejected: keep Mongo, fix the gaps

Extending `MongoStorage` to cover Members, Bills, and Votes would make the "alternative backend" real. It would also cost weeks of work, expand the test matrix, and bring no users any closer. Not warranted absent demand.

## Rejected: move Mongo to a separate package

Could ship `concord-congress-mongo` as a sidecar with its own release cadence. Solves the maintenance-surface problem but keeps the dead-code problem and adds packaging overhead. Not warranted absent demand.
