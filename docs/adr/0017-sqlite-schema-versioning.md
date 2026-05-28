# 0017 — SQLite schema versioning via `PRAGMA user_version`

**Status**: Accepted, 2026-05-27.

## Context

[ADR 0012](0012-web-bootstraps-empty-schema-on-startup.md) made `ensure_schema(db_path)` the single bootstrap entry point and explicitly noted that "no `schema_version` check yet" was fine while DDL stayed append-only. [ADR 0016](0016-web-initiated-enrichment.md) added the first column that breaks that contract — `bills.last_enrichment_error TEXT NULL` — and patched around it with `_POST_RELEASE_COLUMNS`, a tuple of `(table, column, ddl_fragment)` rows applied via `PRAGMA table_info` + `ALTER TABLE … ADD COLUMN` inside `SqliteStorage.__init__`. The shipped pattern works, but it has three structural problems that compound the moment a second migration lands:

1. **Two declarations of the same column.** `last_enrichment_error` is declared in both [`_BASE_SCHEMA`](../../src/concord/storage/sqlite.py) (so fresh DBs get it from `CREATE TABLE`) and `_POST_RELEASE_COLUMNS` (so older DBs get it from `ALTER TABLE`). Nothing enforces that the two declarations agree. A future developer changing the type or default in one place but not the other introduces silent divergence between fresh and migrated installs.
2. **The pattern only handles one migration shape.** *Add a nullable column to an existing table*. Anything else — backfill, rename, type change, index add/drop, new table introduced post-release, multi-step migration — falls outside it. ADR 0016's claim that "new column adds in the future follow the same pattern" is true but narrow; the next non-trivial schema change forces a second mechanism.
3. **No version pointer.** Every boot iterates `_POST_RELEASE_COLUMNS` and runs a `PRAGMA table_info` per entry, with no way to fast-path "this DB is already up to date." Linear in the migration count, forever.

The fix lands now rather than waiting because the in-the-wild surface is at its smallest — one migration entry, no production deployments past a public demo — and because `_POST_RELEASE_COLUMNS` is already a tiny migration system pretending it isn't.

## Decision

Use SQLite's built-in `PRAGMA user_version` — a single 32-bit integer stored in the database header, defaulting to `0` on any DB that has never set it — as the schema-version pointer, and replace `_POST_RELEASE_COLUMNS` with an ordered, append-only `_MIGRATIONS` list of `(version, callable)` tuples applied in order.

**`_BASE_SCHEMA` remains the head snapshot.** Fresh DBs run `_BASE_SCHEMA` (which describes the *current* shape of every table) and then the migration runner stamps `user_version` to `_HEAD`. Existing DBs run `_BASE_SCHEMA` (whose `CREATE TABLE IF NOT EXISTS` clauses are no-ops against pre-existing tables) and then the runner applies every migration strictly greater than `current`. Both paths converge on the same `(schema, user_version)`.

The runner:

```python
def _migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > _HEAD:
        raise RuntimeError(...)         # downgrade is not supported
    for version, fn in _MIGRATIONS:
        if version <= current:
            continue
        with conn:                      # atomic per-migration: BEGIN/COMMIT
            fn(conn)
            conn.execute(f"PRAGMA user_version = {version}")
```

Migration callables receive a `sqlite3.Connection` and mutate it. Each migration must be **idempotent on its own pre-condition** — `PRAGMA table_info` checks, `IF NOT EXISTS` clauses — because the same migration may run against three kinds of DB:

- A genuinely pre-migration DB (column doesn't exist).
- A fresh DB created from `_BASE_SCHEMA` that already has the head-state column (column exists; ALTER is a no-op).
- A DB that was already touched by the pre-0017 `_POST_RELEASE_COLUMNS` code path (column exists; ALTER is a no-op).

Migration **1** is `_m001_add_bill_last_enrichment_error`, a direct port of the existing `_POST_RELEASE_COLUMNS` entry for `bills.last_enrichment_error`. After 0017 lands, every wild DB at `user_version = 0` converges on `user_version = 1` after one boot, regardless of which of the three states above it was in.

**Migrations are append-only, never reordered or rewritten in place.** Editing a landed migration would silently produce different schemas on installs that ran the old version vs. the new one. New schema work is always a new entry with a higher version number.

**A schema-equivalence test enforces the BASE-vs-migrations contract.** A `tests/test_storage_sqlite.py` case asserts that `_BASE_SCHEMA` applied to a fresh connection produces the same schema fingerprint (table names + each table's `PRAGMA table_info` + each table's `PRAGMA index_list`, normalized) as `_BASE_SCHEMA` followed by every migration in `_MIGRATIONS`. The contract: every migration that adds, drops, or modifies a column or index must be accompanied by the matching change to `_BASE_SCHEMA`, and the test fails if they drift. This is the linchpin of option A — without it, the duplication problem returns under a new name.

`ensure_schema(db_path)` and `SqliteStorage.__init__` continue to be the only public entry points to schema setup. The migration runner is called once per `SqliteStorage` construction, after `_BASE_SCHEMA` (and `_VEC_SCHEMA` if `load_vec=True`) have run. The CLI's `concord serve` path picks it up automatically via `create_app() → ensure_schema(db_path)` per [ADR 0012](0012-web-bootstraps-empty-schema-on-startup.md).

## Consequences

**Trade-offs accepted:**

- **Two places to touch when adding a column.** Adding a new column means writing migration N *and* updating `_BASE_SCHEMA` to match. The schema-equivalence test catches drift, so this is bounded duplication — a typo gets a red test, not a divergent install — but it's a real ergonomic cost relative to option B (no `_BASE_SCHEMA`, replay all migrations on every fresh install). Accepted because the readable consolidated DDL of `_BASE_SCHEMA` is load-bearing for understanding the store, and the test makes the contract enforceable rather than aspirational.
- **`user_version = 0` is the implicit pre-0017 floor.** Every DB created before this ADR — including DBs that were already bumped to "post-release" by `_POST_RELEASE_COLUMNS` — reports `0` because that pattern never set `user_version`. The migration runner treats `0` as "apply everything." Idempotency in migration 1 ensures DBs that already have `last_enrichment_error` are not harmed by the re-application.
- **Downgrade is unsupported.** A DB at `user_version = N` opened by a Concord build whose `_HEAD < N` raises. This is appropriate for the deployment shape — the PyPI release is a CLI with a single user per install — and avoids the much harder problem of writing down-migrations.
- **Each migration runs in its own transaction.** Multi-statement migrations are atomic; a half-applied migration is impossible. Cost: a migration that needs to do bulk data work on a multi-GB DB will hold a write lock for that work. Acceptable; if we ever need a long online migration, that migration can opt out of the wrapping transaction explicitly.
- **`PRAGMA user_version` is a single integer with no metadata.** No `applied_at`, no checksum, no name. Acceptable: the source of truth for "what is migration 7" is the source code, and a migration's name lives in the callable's `__name__`. If observability becomes useful — diagnosing which version a stranger's `proceedings.db` reports — adding an `INFO`-level log line at startup is the right place, not a metadata table.

**Things this buys:**

- **One source of truth per schema change.** Add a migration; update `_BASE_SCHEMA`; the test pins them together. No tuples of `(table, column, ddl_fragment)` to maintain in parallel with the DDL.
- **Room for non-column-add migrations.** Backfills, index changes, new tables, multi-step changes — all expressible as a migration callable that does whatever it needs to. The runner doesn't care.
- **Fast no-op on subsequent boots.** `PRAGMA user_version` is one read; equality with `_HEAD` short-circuits before any `PRAGMA table_info` scan.
- **A documented contract for future ADRs.** "Add a migration entry; update `_BASE_SCHEMA`; the schema-equivalence test enforces the link" is small enough to live in this ADR and be referenced rather than re-derived.

**What stays open:**

- **Compaction across major versions.** Once the migration list is long enough to be annoying to read, we may want to declare a floor (e.g. "v1.0 assumes `user_version >= 12`; older DBs must boot v0.x first to migrate forward") and delete the now-baked-in migrations. Not built; the right moment is when the list crosses ~20 entries, not before.
- **Vector schema versioning.** `_VEC_SCHEMA` is conditional on `load_vec=True` and currently has no post-release migrations. If `chunks_vec` ever grows a column, the same `_MIGRATIONS` list handles it — the migration callable can check whether `chunks_vec` exists before touching it, since `load_vec=False` callers won't have it.
- **`ADR 0002` and non-derived state.** [ADR 0016](0016-web-initiated-enrichment.md) introduced `last_enrichment_error` as runtime state that cannot be rebuilt from JSONL. The framing of [ADR 0002](0002-jsonl-as-canonical-raw-store.md) — SQLite is derived/rebuildable — bends here, and 0017 makes the bend permanent by giving migrations a first-class home. A sibling amendment to 0002 noting this category ("derived store + small amount of web-runtime state that survives a JSONL rebuild") is worth writing but is out of scope for this ADR.

## Rejected: no `_BASE_SCHEMA`, replay all migrations on every install (option B)

The purist alternative: delete `_BASE_SCHEMA`, make migration 1 the body of today's `_BASE_SCHEMA`, and have fresh installs run every migration in order. Pro: one source of truth, no drift possible, no schema-equivalence test needed. Con: fresh-install boot is linear in the migration count, and — more importantly — `_BASE_SCHEMA` is the load-bearing artifact for *humans* reading the codebase to understand what tables exist. Splintering that across N migrations would make a fresh contributor's job materially harder. The schema-equivalence test buys back the drift safety at a smaller readability cost.

## Rejected: a `schema_migrations` table (Django/Rails/Alembic-style)

A dedicated table with `(version, name, applied_at, checksum)` rows would give us richer observability than a single integer and is the industry-standard shape. Rejected because `PRAGMA user_version` already exists, is atomic with the rest of the DB header, requires zero DDL, and our deployment shape (single SQLite file, one user per install, migrations live in the source tree) doesn't benefit from per-migration metadata. The integer is sufficient; the table is what we'd reach for if we ever needed cross-process coordination, which we don't.
