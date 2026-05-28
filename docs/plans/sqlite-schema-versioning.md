# SQLite schema versioning via `PRAGMA user_version`

> Replace [ADR 0016](../adr/0016-web-initiated-enrichment.md)'s `_POST_RELEASE_COLUMNS` ad-hoc migration mechanism with a versioned migration runner keyed on `PRAGMA user_version`, with `_BASE_SCHEMA` retained as the head snapshot and a schema-equivalence test pinning the two together. See [ADR 0017](../adr/0017-sqlite-schema-versioning.md) for the framing.

## Source

Conversation context, 2026-05-27. The user was reviewing the migration code shipped in [PR #81](https://github.com/johnmarcampbell/concord/pull/81) (the bill-enrichment button) and identified three structural problems with `_POST_RELEASE_COLUMNS`: duplicate declarations of `last_enrichment_error` between `_BASE_SCHEMA` and the migration tuple, the pattern only handling `ADD COLUMN`, and no version pointer. We chose to replace it now rather than wait for the next migration shape because the in-the-wild surface is at its smallest (one entry) and `_POST_RELEASE_COLUMNS` is already a migration system pretending it isn't. ADR 0017 was drafted as the framing artifact alongside this plan.

## Context

Today, schema bootstrap goes through one entry point — [`storage/sqlite.py:ensure_schema(db_path)`](../../src/concord/storage/sqlite.py) — which constructs a `SqliteStorage` and immediately closes it. The constructor:

1. Opens the SQLite file (creating it if absent).
2. Sets `PRAGMA journal_mode = WAL` and `PRAGMA foreign_keys = ON`.
3. Loads `sqlite-vec` (if `load_vec=True`).
4. Runs `_BASE_SCHEMA` via `executescript` — a long string of `CREATE TABLE IF NOT EXISTS` statements that describes the complete current shape of every table.
5. Runs `_VEC_SCHEMA` if `load_vec=True`.
6. Calls `_apply_idempotent_migrations(conn)`, which iterates `_POST_RELEASE_COLUMNS` (a tuple of `(table, column, ddl_fragment)`), checks `PRAGMA table_info(table)`, and runs `ALTER TABLE … ADD COLUMN` if absent.
7. `COMMIT`.

`_POST_RELEASE_COLUMNS` has exactly one entry today: `("bills", "last_enrichment_error", "TEXT")`. The column is *also* declared in `_BASE_SCHEMA`'s `bills` table DDL — fresh DBs get it from `CREATE TABLE`, existing DBs from `ALTER TABLE`. Nothing enforces the two declarations agree.

This plan replaces (6) with a versioned migration runner keyed on `PRAGMA user_version`, retains `_BASE_SCHEMA` as the head snapshot (option A in ADR 0017), and adds a schema-equivalence test that catches drift between `_BASE_SCHEMA` and the migration list. The first migration is a direct port of today's `_POST_RELEASE_COLUMNS` entry; subsequent migrations append.

Domain terms used here are defined in [CONTEXT.md](../../CONTEXT.md). This plan touches the **derived store** (SQLite) only; the **canonical raw store** (JSONL, per [ADR 0002](../adr/0002-jsonl-as-canonical-raw-store.md)) is untouched.

## Goals

1. Add a `_MIGRATIONS: tuple[tuple[int, Callable[[sqlite3.Connection], None]], ...]` ordered list and a `_HEAD` constant in [`src/concord/storage/sqlite.py`](../../src/concord/storage/sqlite.py).
2. Add `_migrate(conn)` — read `PRAGMA user_version`; raise if `> _HEAD`; apply every migration `> current` in order; each wrapped in `with conn:`; bump `user_version` to the migration's version on success.
3. Add migration 1: `_m001_add_bill_last_enrichment_error(conn)` — port today's `_POST_RELEASE_COLUMNS` entry for `bills.last_enrichment_error`.
4. Replace the `_apply_idempotent_migrations(self._conn)` call in `SqliteStorage.__init__` with `_migrate(self._conn)`.
5. Delete `_POST_RELEASE_COLUMNS` and `_apply_idempotent_migrations`.
6. Update `ensure_schema`'s docstring to point at the new mechanism and ADR 0017.
7. Add a `test_base_schema_matches_replayed_migrations` test in [`tests/test_storage_sqlite.py`](../../tests/test_storage_sqlite.py) that asserts `_BASE_SCHEMA` + every migration applied on a fresh DB is a structural no-op (i.e. `_BASE_SCHEMA` already matches HEAD).
8. Add a `test_migration_runner_*` set of tests covering: fresh DB stamps to `_HEAD`, version-0 DB without column gets ALTER then bumps to 1, version-0 DB *with* column (the post-0.2.x case where pre-0017 `_POST_RELEASE_COLUMNS` already ran) skips ALTER then bumps to 1, version-1 DB is a no-op, version-greater-than-head raises.
9. Update the existing `test_alter_table_adds_last_enrichment_error_when_missing` and `test_migration_is_idempotent` tests in [`tests/test_storage_sqlite.py`](../../tests/test_storage_sqlite.py) to assert against the new mechanism (they're testing the right behaviour; they need to call `_migrate` / inspect `user_version` instead of `_apply_idempotent_migrations`).

## Non-goals

1. **Down-migrations.** ADR 0017 explicitly rejects them. A DB at `user_version > _HEAD` raises; users who need to downgrade reinstall.
2. **Per-migration metadata table.** Single `PRAGMA user_version` integer is sufficient. See ADR 0017's "Rejected: a `schema_migrations` table" section.
3. **Vector schema (`_VEC_SCHEMA`) versioning beyond what falls out for free.** The runner handles `chunks_vec` migrations the same way as any other; no special-casing is needed and none is added in this plan.
4. **Compacting the migration list.** Migration 1 is the only entry today; compaction is irrelevant until the list is much longer.
5. **Changing the call shape of `ensure_schema(db_path)` or `SqliteStorage.__init__`.** Both keep the same signature. The change is purely internal to the migration step.
6. **Amending [ADR 0002](../adr/0002-jsonl-as-canonical-raw-store.md).** The category shift (`last_enrichment_error` is non-derived runtime state) is real, but out of scope here. ADR 0017's "What stays open" section flags it for a sibling amendment.
7. **Logging on migration application.** Useful but not required for v1; the test suite is the verification surface today. If we want runtime observability later, an `INFO` log inside `_migrate` is the right place.

## Relevant prior decisions

- [ADR 0002 — JSONL as canonical raw store](../adr/0002-jsonl-as-canonical-raw-store.md). SQLite is derived; in principle a user can always delete the DB and re-run the pipeline. Migrations exist to spare users that cost — and now also to preserve non-derived state like `last_enrichment_error`.
- [ADR 0003 — SQLite + FTS5 + sqlite-vec as the derived store](../adr/0003-sqlite-as-derived-store.md). Establishes the storage backend whose schema this plan versions.
- [ADR 0012 — Web layer bootstraps an empty schema on startup](../adr/0012-web-bootstraps-empty-schema-on-startup.md). `ensure_schema(db_path)` is the bootstrap entry point. Explicitly noted "no `schema_version` check yet" as acceptable while DDL was append-only; ADR 0017 closes that gap.
- [ADR 0016 — Web layer may invoke Stage 0 enrichment on demand](../adr/0016-web-initiated-enrichment.md). Introduced `bills.last_enrichment_error` and the `_POST_RELEASE_COLUMNS` pattern that this plan replaces.
- [ADR 0017 — SQLite schema versioning via `PRAGMA user_version`](../adr/0017-sqlite-schema-versioning.md). The framing for this plan.

## Relevant files and code

- [`src/concord/storage/sqlite.py:72`](../../src/concord/storage/sqlite.py) — start of `_BASE_SCHEMA`. The `bills` table DDL containing `last_enrichment_error TEXT` is around line 210.
- [`src/concord/storage/sqlite.py:591`](../../src/concord/storage/sqlite.py) — `ensure_schema(db_path)`. Docstring update.
- [`src/concord/storage/sqlite.py:606-631`](../../src/concord/storage/sqlite.py) — `_POST_RELEASE_COLUMNS` + `_apply_idempotent_migrations`. Deleted by this plan.
- [`src/concord/storage/sqlite.py:637-660`](../../src/concord/storage/sqlite.py) — `SqliteStorage.__init__`. Replace the `_apply_idempotent_migrations(self._conn)` line with `_migrate(self._conn)`.
- [`src/concord/storage/sqlite.py:1023, 1031`](../../src/concord/storage/sqlite.py) — `set_bill_enrichment_error` / `clear_bill_enrichment_error`. Unchanged; included in the file map because they're the consumers of the column the first migration adds.
- [`tests/test_storage_sqlite.py`](../../tests/test_storage_sqlite.py) — existing migration tests (`test_alter_table_adds_last_enrichment_error_when_missing`, `test_migration_is_idempotent`) live here. New tests go in the same file.
- `CLAUDE.md` — has a one-line entry referencing the new column added in PR #81. No edit required; the ADR is the right place for the versioning story.

## Approach

### Option A: `_BASE_SCHEMA` is the head snapshot

ADR 0017 picks option A: `_BASE_SCHEMA` continues to describe the *current* shape of every table (including columns added by every landed migration). Fresh DBs run `_BASE_SCHEMA` and skip past all migrations on first boot (each migration is a no-op because its target column / table / index is already present). Existing DBs hit the same `_BASE_SCHEMA` (CREATE IF NOT EXISTS is harmless), then `_migrate` brings them forward.

The drift risk between `_BASE_SCHEMA` and the migration list is bounded by **`test_base_schema_matches_replayed_migrations`** — see [Step 7](#step-by-step-plan). That test is the contract: if it fails, the developer who added the migration must update `_BASE_SCHEMA` to match (or vice versa).

### Migration callables are idempotent on their pre-condition

`_m001_add_bill_last_enrichment_error` must succeed against three kinds of DB:

- A pre-0.2.x DB at `user_version = 0` that has never seen `last_enrichment_error`. ALTER runs; column added.
- A 0.2.x DB at `user_version = 0` that already had `last_enrichment_error` added by the pre-0017 `_POST_RELEASE_COLUMNS` code path. ALTER skipped; column unchanged.
- A fresh DB at `user_version = 0` whose `_BASE_SCHEMA` already declared `last_enrichment_error`. ALTER skipped; column unchanged.

All three converge on `user_version = 1` with the column present. The skip mechanic is `PRAGMA table_info(bills)` — same shape as today's `_apply_idempotent_migrations`, just inlined into the migration body. Future migrations follow the same posture: check the pre-condition; act if needed; let the runner bump the version.

### One transaction per migration

`with conn:` wraps each migration call. SQLite's `connect` returns a connection in autocommit mode that `with conn` upgrades to a BEGIN/COMMIT pair (or ROLLBACK on exception). This means:

- A migration that fails halfway leaves the DB exactly as it was at the start of that migration; `user_version` is not bumped. Re-boot retries.
- Migrations cannot share state across the transaction boundary — each is independent. Good; reduces coupling between migration numbers.
- Bulk-data migrations hold the write lock for their duration. Acceptable for any realistic Concord DB size; revisitable if we ever need an online migration.

### Migration list is append-only

The list is module-level data, ordered by version. Never reorder, never renumber, never edit a landed entry's behaviour. Adding a new migration is always "append a new tuple with the next integer." This is the only contract; if a migration turns out to have been wrong, the fix is a *new* migration that corrects it.

`_HEAD = _MIGRATIONS[-1][0] if _MIGRATIONS else 0` derives from the list rather than a separate constant, so adding an entry automatically updates head.

### Schema-equivalence test is the linchpin

```python
def test_base_schema_matches_replayed_migrations(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(_BASE_SCHEMA)
    before = _schema_fingerprint(conn)

    for _version, fn in _MIGRATIONS:
        with conn:
            fn(conn)
    after = _schema_fingerprint(conn)

    assert before == after, (
        "BASE_SCHEMA and the migration list disagree. Either:\n"
        "  - a migration added a column / index / table that _BASE_SCHEMA doesn't already have, OR\n"
        "  - _BASE_SCHEMA has something the migrations don't.\n"
        "Update _BASE_SCHEMA to reflect HEAD."
    )
```

`_schema_fingerprint(conn)` is a helper that walks `sqlite_master` for table names, then `PRAGMA table_info(<table>)` and `PRAGMA index_list(<table>)` for each, and returns a normalized hashable structure (sorted tuples of tuples). Defined inside the test file — not exported — because it's a test-only diagnostic.

## Step-by-step plan

### Step 1 — Add `_MIGRATIONS`, `_HEAD`, and `_migrate` in `storage/sqlite.py`

In [`src/concord/storage/sqlite.py`](../../src/concord/storage/sqlite.py), between the existing `_VEC_INSERT_SQL` constant (around line 588) and the `ensure_schema` function (around line 591), add:

```python
def _m001_add_bill_last_enrichment_error(conn: sqlite3.Connection) -> None:
    """ADR 0016: ``bills.last_enrichment_error TEXT NULL``.

    Idempotent against DBs whose ``_BASE_SCHEMA`` already declares the
    column (fresh installs after this ADR landed) and against DBs that
    were touched by the pre-ADR-0017 ``_POST_RELEASE_COLUMNS`` code
    path. ``PRAGMA table_info`` returns the existing column set; if the
    column is present we skip the ALTER, the runner still bumps
    ``user_version``.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(bills)")}
    if "last_enrichment_error" not in existing:
        conn.execute("ALTER TABLE bills ADD COLUMN last_enrichment_error TEXT")


#: Ordered, append-only schema migrations. Each entry is
#: ``(version, callable)``; the callable receives a connection and
#: mutates it to bring the DB from version N-1 to version N. The
#: runner wraps each call in a transaction and bumps
#: ``PRAGMA user_version`` on success. See ADR 0017.
#:
#: NEVER reorder, renumber, or edit a landed entry. The fix for a
#: buggy migration is a *new* migration with a higher version.
_MIGRATIONS: tuple[tuple[int, Callable[[sqlite3.Connection], None]], ...] = (
    (1, _m001_add_bill_last_enrichment_error),
)
_HEAD: int = _MIGRATIONS[-1][0] if _MIGRATIONS else 0


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply every pending migration in order; bump ``user_version``.

    Reads ``PRAGMA user_version`` (defaults to ``0`` on any DB that has
    never set it). Raises if the DB reports a version higher than
    ``_HEAD`` — downgrade is not supported. Each migration runs inside
    its own transaction; a failure leaves the DB at the previous
    version. See ADR 0017.
    """
    current: int = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > _HEAD:
        raise RuntimeError(
            f"SQLite user_version={current} exceeds this build's _HEAD={_HEAD}. "
            "Downgrade is not supported; reinstall a newer Concord build."
        )
    for version, fn in _MIGRATIONS:
        if version <= current:
            continue
        with conn:
            fn(conn)
            conn.execute(f"PRAGMA user_version = {version}")
```

Import `Callable` from `collections.abc` at the top of the module if not already imported.

### Step 2 — Wire `_migrate` into `SqliteStorage.__init__`

In [`src/concord/storage/sqlite.py`](../../src/concord/storage/sqlite.py), `SqliteStorage.__init__` (around line 637–660):

```python
self._conn.executescript(_BASE_SCHEMA)
if load_vec:
    self._conn.executescript(_VEC_SCHEMA)
_migrate(self._conn)           # was: _apply_idempotent_migrations(self._conn)
self._conn.commit()
```

The trailing `self._conn.commit()` is now mostly a no-op for the `_migrate` path (each migration committed via its own `with conn:` block) but stays in place to cover the `executescript` calls above.

### Step 3 — Delete `_POST_RELEASE_COLUMNS` and `_apply_idempotent_migrations`

Remove the constant tuple and the function (currently around lines 606–631 in [`src/concord/storage/sqlite.py`](../../src/concord/storage/sqlite.py)). No other call sites — `grep _POST_RELEASE_COLUMNS src tests` should return zero matches after this step. Same for `_apply_idempotent_migrations`.

### Step 4 — Update `ensure_schema`'s docstring

Rewrite the docstring to reference ADR 0017 and the new mechanism. Replacement:

```python
def ensure_schema(db_path: Path | str) -> None:
    """Create the SQLite file (and parent dir) and apply the full schema.

    Idempotent: every DDL statement in ``_BASE_SCHEMA`` and
    ``_VEC_SCHEMA`` is ``CREATE … IF NOT EXISTS``, and post-release
    schema changes are applied via the versioned migration runner
    (``_migrate``) keyed on ``PRAGMA user_version`` — see ADR 0017. Safe
    to call against a fresh DB, an older DB, or a DB already at HEAD;
    in all three cases the file converges on the current schema with
    ``user_version = _HEAD``.

    Used by the web layer to bootstrap an empty store on first boot —
    see ADR 0012.
    """
    SqliteStorage(db_path).close()
```

### Step 5 — Add `test_base_schema_matches_replayed_migrations`

In [`tests/test_storage_sqlite.py`](../../tests/test_storage_sqlite.py), add the test as defined in [Approach](#schema-equivalence-test-is-the-linchpin). Define a private `_schema_fingerprint(conn) -> tuple[...]` helper at module scope (or inside the test) that returns a normalized structure across:

- `sqlite_master` table names (filter to `type = 'table'`, exclude `sqlite_*` internals).
- For each table: a sorted tuple of `PRAGMA table_info` rows (name, type, notnull, dflt_value, pk).
- For each table: a sorted tuple of `PRAGMA index_list` rows (name, unique, origin, partial), each followed by its `PRAGMA index_info` columns.

Skip `chunks_vec` (and any other virtual table) in the fingerprint walk — `PRAGMA table_info` returns special shapes for `sqlite-vec` virtual tables that are stable but not meaningful for equivalence checking.

### Step 6 — Add migration-runner state-machine tests

In [`tests/test_storage_sqlite.py`](../../tests/test_storage_sqlite.py), add tests covering:

- `test_fresh_db_stamps_to_head` — open a brand-new DB via `ensure_schema`; assert `PRAGMA user_version == _HEAD`.
- `test_pre_migration_db_gets_column_and_bumps_to_1` — build a "stale" `bills` table that predates `last_enrichment_error` (the same construction the existing `test_alter_table_adds_last_enrichment_error_when_missing` uses), confirm `PRAGMA user_version = 0` and column absent; call `ensure_schema`; assert column present and `user_version = 1`.
- `test_already_migrated_db_skips_alter` — start with a DB that has `last_enrichment_error` present but `user_version = 0` (the post-0.2.x-pre-0017 case); call `ensure_schema`; assert no error and `user_version = 1`.
- `test_db_at_head_is_no_op` — open and close once; open and close again; assert second `ensure_schema` does not re-run migrations (verifiable via `user_version == _HEAD` both times and via mocking the migration callable to assert it's called only once across two boots).
- `test_db_above_head_raises` — manually set `PRAGMA user_version = _HEAD + 1`; assert `ensure_schema` raises `RuntimeError` with a downgrade-not-supported message.

### Step 7 — Update the existing migration tests

The two tests added in PR #81 — `test_alter_table_adds_last_enrichment_error_when_missing` and `test_migration_is_idempotent` in [`tests/test_storage_sqlite.py`](../../tests/test_storage_sqlite.py) — are still useful as targeted regression tests for migration 1's behavior. Either:

- Keep them as-is (they call `ensure_schema`, which now goes through `_migrate`), and add an extra assertion at the end of each that `user_version == 1`.
- Or rename them to `test_m001_*` and have them call `_m001_add_bill_last_enrichment_error` directly for a more targeted assertion.

Either is fine; whichever the implementer prefers. The state-machine tests from Step 6 cover the runner; these cover migration 1 specifically.

### Step 8 — Confirm no other consumers of the old surface

`grep -r '_POST_RELEASE_COLUMNS\|_apply_idempotent_migrations' src tests docs` should return zero matches after Step 3. The docs hit will include this plan and ADR 0017 referencing the *old* mechanism by name — that's expected and correct; don't rewrite them.

### Step 9 — Run the full check suite

```sh
uv run ruff check
uv run ruff format --check
uv run mypy src
uv run pytest
```

All four must be clean before opening the PR. The schema-equivalence test will be the new noisy one if `_BASE_SCHEMA` and migration 1 disagree — that's the contract working.

### Step 10 — Open the PR

Title: `refactor(storage): versioned schema migrations via PRAGMA user_version (ADR 0017)`.

Body should reference [ADR 0017](../adr/0017-sqlite-schema-versioning.md), note that the behavior is equivalent to the previous `_POST_RELEASE_COLUMNS` mechanism for every DB shape (fresh, pre-PR-81, post-PR-81-pre-0017), and link this plan as the source of record.

## Verification

The mechanical checks (ruff / mypy / pytest) are in Step 9. The behavioural verification beyond that is:

1. **Fresh-DB path.** Delete a local SQLite file, run `concord serve`, confirm boot succeeds and `sqlite3 data/concord.db 'PRAGMA user_version'` reports `_HEAD` (currently `1`).
2. **Existing-DB path.** Against an existing local DB that predates PR #81 (if available), run `concord serve` and confirm `PRAGMA user_version` reports `1` and `PRAGMA table_info(bills)` includes `last_enrichment_error`.
3. **Post-PR-81-pre-0017 path.** Against a DB that was booted by a pre-0017 build and has `last_enrichment_error` but `user_version = 0`, run `concord serve` and confirm `user_version` is now `1` and the column is unchanged. If no such DB is available locally, the `test_already_migrated_db_skips_alter` test (Step 6) is the verification surface.

## Risks and mitigations

- **Risk:** A future migration mutates `_BASE_SCHEMA` without a corresponding migration entry (e.g. a developer "fixes" a column type directly in `_BASE_SCHEMA`). **Mitigation:** the schema-equivalence test fails immediately — `_BASE_SCHEMA` describes one thing and the replayed migrations describe another.
- **Risk:** A future migration is non-idempotent (e.g. it does an unconditional ALTER that fails if the column exists). **Mitigation:** the post-0.2.x DBs case (column already added by `_POST_RELEASE_COLUMNS`) is a permanent test fixture (`test_already_migrated_db_skips_alter` in Step 6) — any new migration that breaks against a pre-condition-already-met DB will be caught. ADR 0017's "Migration callables are idempotent on their pre-condition" rule is the design contract.
- **Risk:** A migration's transaction conflicts with the `executescript` calls above it that ran with autocommit. **Mitigation:** the `with conn:` block is structurally independent; `executescript` autocommits as it runs, so by the time `_migrate` starts the connection is in a clean state. The `self._conn.commit()` at the end of `__init__` covers any straggler.
- **Risk:** Multiple `SqliteStorage` instances open the same DB concurrently and race on `_migrate`. **Mitigation:** SQLite's write lock serializes the `ALTER TABLE` inside the migration's transaction; the later writer sees `user_version` already at `1` and short-circuits. Same property as today's `_apply_idempotent_migrations`.
- **Risk:** Tests that built "stale" DBs by writing custom `CREATE TABLE` strings (the existing PR-81 tests) miss that `PRAGMA user_version` defaults to `0` only on a fully fresh DB; if a test artifact has `user_version != 0`, the runner skips migration 1. **Mitigation:** the state-machine tests in Step 6 each set `PRAGMA user_version` explicitly before calling `ensure_schema`, so test fixtures are unambiguous.
