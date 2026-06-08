# 0024 — Constraint-tightening on rebuildable mirror columns

**Status**: Accepted, 2026-06-08.

## Context

[PR #89](https://github.com/johnmarcampbell/concord/pull/89) tightened ten Pydantic
model fields from `T | None` to `T` after auditing the API fixtures — `Term.party
/ start_date / end_date`, `BillCosponsor.sponsorship_date`, `BillAction.action_code
/ source_system`, `BillSummary.action_date / action_desc`, `VotePosition.vote_party
/ vote_state`. [Issue #90](https://github.com/johnmarcampbell/concord/issues/90)
is the follow-up that brings the corresponding SQLite mirror columns into
agreement: declare them `NOT NULL`.

The issue text proposed *"a straight schema change — per [ADR 0002](0002-jsonl-as-canonical-raw-store.md)
there's no migration story, SQLite is rebuildable from JSONL."* But the issue was
filed one day after [ADR 0017](0017-sqlite-schema-versioning.md) landed the
`PRAGMA user_version` migration runner, which changed the calculus. Under ADR 0017:

- `_BASE_SCHEMA` is the *head snapshot*; fresh DBs get the current shape from it.
- Existing on-disk DBs only converge through migrations, because `CREATE TABLE IF
  NOT EXISTS` no-ops against a pre-existing table. A bare `_BASE_SCHEMA` edit would
  leave a live DB nullable while a fresh install is `NOT NULL` — *both reporting
  the same `user_version`*. That silent divergence is exactly what ADR 0017 exists
  to prevent.

So the question issue #90 forces is: lean on ADR 0002's rebuildability (edit the
DDL, let the one wild demo DB get nuked-and-rebuilt), or honor ADR 0017's
convergence guarantee with a real migration? Two facts make the second non-trivial:

1. **SQLite has no `ALTER COLUMN … SET NOT NULL`.** Tightening an existing column
   requires the 12-step table rebuild: create a new table carrying the constraint,
   copy the rows, drop the original, rename the copy back, recreate indexes.
2. **A rebuild must decide what to do with a legacy row that holds a `NULL`** in a
   now-`NOT NULL` column — it would violate the constraint on copy.

This ADR records the choices, because a future reader who sees a full table-rebuild
migration added merely to tighten a constraint — when ADR 0002 says the store is
rebuildable — will reasonably ask "why bother?"

## Decision

**Tighten via a guarded table-rebuild migration, not a bare `_BASE_SCHEMA` edit.**
We converge existing DBs so `user_version` stays an honest identity of the schema,
upholding ADR 0017 rather than carving an exception into it. Concretely, issue #90
edits `_BASE_SCHEMA` (the head snapshot) *and* adds three append-only migrations —
`m005` (`member_terms`), `m006` (the three `bill_*` child tables), `m007`
(`vote_positions`) — bumping `_HEAD` 4 → 7. Three module-local entries preserve the
"each domain module owns its tables' migrations" convention; all delegate to one
shared helper, `concord.storage._ddl.rebuild_table_add_not_null`.

The pattern for any future constraint-tightening on a rebuildable mirror column is:

**1. Drop rows that violate the new constraint; log the count.** The rebuild copies
with `INSERT … SELECT … WHERE <every tightened col> IS NOT NULL`. A DB loaded under
the strict post-#89 models has no such rows, so this only bites a DB loaded under
the old nullable models; those dropped rows are derived state and reappear on the
next `concord load` from JSONL (ADR 0002). Fail-loud was rejected — it bricks
`concord serve` against a legacy DB and defeats the convergence goal. Coalesce-to-`''`
was rejected — it fabricates empty data the strict model silently accepts as a valid
(empty) `str`, masking the very contract violation #89 set out to surface.

**2. Inject `NOT NULL` into the *live* DDL read from `sqlite_master`, never
hand-write the new table.** The schema-equivalence fingerprint (ADR 0017) compares
`PRAGMA table_info` + `index_list`, which capture *neither* CHECK nor FK
constraints. A hand-copied `CREATE TABLE` that accidentally dropped `member_terms`'s
chamber CHECK or a `bill_*` foreign key would pass every fingerprint test. Reading
the live statement and adding `NOT NULL` only to the named columns preserves CHECK /
FK / DEFAULT / PRIMARY KEY by construction. A dedicated test additionally asserts the
CHECK and FK *behaviour* survives, as a backstop against a bad string transform.

**3. Guard on the precondition; rebuild only when a target column is still
nullable.** The helper inspects `PRAGMA table_info` and returns early (a no-op) when
every target column is already `NOT NULL`. On a fresh install — where `_BASE_SCHEMA`
already declares the constraint — the migration does nothing, so the existing
`test_base_schema_matches_replayed_migrations` passes trivially. The rebuild's
structural correctness is pinned instead by a dedicated test that starts from a
legacy nullable DB and asserts the converged schema fingerprint equals a fresh one.

**Foreign keys stay ON through the rebuild — no toggle.** The 12-step recipe
normally calls for `PRAGMA foreign_keys=OFF`, which is a no-op inside a transaction
(and the runner wraps each migration in one). It is unnecessary here because none of
the five tightened tables is a foreign-key *target*; their only FKs are outgoing and
are re-checked against still-present parents during the copy. A leading `DROP TABLE
IF EXISTS <tmp>` makes the helper retry-safe against an orphan temp table from a
prior failed, un-versioned attempt.

## Consequences

**Trade-offs accepted:**

- **A rebuild migration is heavier than a DDL edit.** Every existing DB drops and
  recreates the affected tables once on upgrade. Accepted to keep `user_version` an
  honest schema identity (ADR 0017) rather than relying on operators to rebuild.
- **Row drops are silent except for a log line.** A legacy DB with real NULLs loses
  those rows on migrate. Acceptable: the store is derived (ADR 0002) and the rows
  return on the next load; a `WARNING` with the per-table count surfaces it.
- **The `NOT NULL`-injection transform is a regex over the project's own DDL.** It
  assumes unquoted identifiers and a `<name> <TYPE>` column shape, and raises if it
  can't make exactly one substitution per column. Brittle in the abstract, but bounded
  by the dedicated rebuild test and the fail-fast assertions.

**Things this buys:**

- Fresh and migrated installs converge on the same `(schema, user_version)` for a
  constraint change, closing the gap ADR 0017's "What stays open" flagged between it
  and ADR 0002.
- A reusable `rebuild_table_add_not_null` helper and a documented pattern for the
  next constraint-tightening, so it isn't re-derived from scratch.

## Rejected: bare `_BASE_SCHEMA` edit, lean on ADR 0002

Edit the DDL, add no migration, let the one wild demo DB get rebuilt from JSONL. The
schema-equivalence test would even stay green (both sides run the edited base).
Rejected because it reintroduces the silent fresh-vs-migrated divergence at equal
`user_version` that ADR 0017 was created to eliminate — trading a one-time rebuild
cost for a standing correctness gap. Viable for a throwaway store; not worth the
erosion of the versioning invariant now that one exists.

## Rejected: `writable_schema` hack to rewrite the stored DDL in place

`PRAGMA writable_schema=ON` + `UPDATE sqlite_master SET sql=…` can add `NOT NULL` to
the stored `CREATE` text without copying data. Rejected: it does not validate or
clean existing NULL rows (the constraint simply starts applying to future writes,
leaving latent violations), and SQLite documents it as dangerous. The table rebuild
is slower but honest.
