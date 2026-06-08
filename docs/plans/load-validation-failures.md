# Plan: structured records for load-stage validation failures (issue #91)

**Issue:** [#91 — Structured logging for API validation failures](https://github.com/johnmarcampbell/concord/issues/91)
**Design of record:** [ADR 0023](../adr/0023-load-validation-failures-mirror-table.md) · glossary term **Load Validation Failure** in [CONTEXT.md](../../CONTEXT.md) ("Observability").

> Read ADR 0023 first. It explains *why* this is a mirror table and not what
> the issue literally asked for (JSON logs / a "Load Run" ledger). This plan is
> the *how*. If anything below conflicts with ADR 0023, the ADR wins.

## What we're building

When a Stage 1 loader catches an `X.from_congress_api(...)` rejection
(`ValidationError` / `ValueError` / `KeyError`), it currently warns and skips.
We add a **`validation_failures` SQLite mirror table** that records one queryable
row per such failure, so contract drift can be aggregated ("which field started
coming back null?", "which entity fails most?", "what did the last load of bill X
drop?"). The existing `_log.warning` lines stay as a human heartbeat.

### Settled invariants (from the grill / ADR 0023)

- **Mirror table, replace-on-load.** Failures are a deterministic function of the
  canonical JSONL → rebuildable (ADR 0019). A re-load *converges* the table
  (DELETE the scope, INSERT the current set), never appends. **No** `runs.jsonl`-
  style backup, **no** run_id, **no** load timestamp (a wall-clock column would
  break byte-identical rebuild).
- **Scope = the 10 model-parse entities only:** `bill`, `cosponsor`, `action`,
  `subject`, `title`, `summary`, `vote`, `vote_position`, `member`, `term`.
  Envelope/JSONL-corruption skips and Senate LIS→Bioguide bridge misses are **out**
  (they stay plain `_log.warning`).
- **`entity_key` is the parent natural key for child rows:** bill children key on
  `bill_id`; vote positions key on `vote_id`. This single column serves both the
  per-parent query and the `load_one` delete predicate, so there is **no separate
  scope column**.
- **`field_path`** = first Pydantic `exc.errors()[0]["loc"]` as a dotted scalar
  string; `NULL` for `ValueError`/`KeyError`.
- **`payload`** stored as `json.dumps(payload, sort_keys=True)` (byte-stable).
- **`LoadStats.malformed` is reformulated (option C):** inline increments are for
  envelope corruption *only*; every class-(b) failure flows through the `failures`
  list and `malformed += len(failures)` at the end. This fixes a latent
  under-count (dropped child rows are invisible today) and makes the relationship
  exact: `table_rows == len(failures) == malformed − envelope_failures`.
- **Out of scope:** Proceedings (not one of the 10), any web view, any CI
  drift-rate gate (dropped entirely — no live-API-in-CI).

## House rules (CONTRIBUTING.md)

Python 3.12+. **Never** `from __future__ import annotations`. Absolute imports
only. Line length 100. Type-hint everything in `src/`. Any `# noqa` needs an
inline reason. `pytest.mark.parametrize` uses a **tuple** of names.

---

## Step 1 — `src/concord/models/validation.py` (new)

A Concord-originated Pydantic model (no `from_congress_api`; it is not a wire
shape), with a `from_exc` classmethod that owns `field_path` extraction. Mirror
the docstring style of `models/runs.py`.

```python
"""Domain model for the Load Validation Failure mirror table (ADR 0023)."""

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, ValidationError


class ValidationFailure(BaseModel):
    """One upstream payload that violated a model contract at the Stage 1 load
    boundary (ADR 0023). Concord-originated — no ``from_congress_api`` factory.
    Field names match the ``validation_failures`` columns one-for-one. ``payload``
    is the offending value (a dict for JSON entities, the raw XML str for Senate
    votes); the storage layer serializes it with sorted keys."""

    model_config = ConfigDict(extra="ignore")

    entity: str
    entity_key: str
    source_file: str
    exc_type: str
    exc_msg: str
    field_path: str | None
    payload: Any

    @classmethod
    def from_exc(
        cls,
        *,
        entity: str,
        entity_key: str,
        source_file: str,
        exc: Exception,
        payload: Any,
    ) -> Self:
        """Build a failure record from a caught parse exception.

        ``field_path`` is the first Pydantic error location as a dotted string
        (e.g. ``sponsors.0.bioguideId``); ``None`` for ``ValueError`` / ``KeyError``,
        which carry no Pydantic loc.
        """
        field_path: str | None = None
        if isinstance(exc, ValidationError):
            errors = exc.errors()
            if errors:
                field_path = ".".join(str(part) for part in errors[0]["loc"])
        return cls(
            entity=entity,
            entity_key=entity_key,
            source_file=source_file,
            exc_type=type(exc).__name__,
            exc_msg=str(exc),
            field_path=field_path,
            payload=payload,
        )


__all__ = ["ValidationFailure"]
```

## Step 2 — `src/concord/storage/validation.py` (new)

Mirror `storage/runs.py` exactly: a `*_SCHEMA` DDL string, a column tuple, an
`insert_sql`-built statement, the `m004` migration, the `replace_*` function, and
a `_row_from_*` projector. **Pure SQL, takes `conn`** — `SqliteStorage` owns the
transaction boundary.

```python
"""Load Validation Failure mirror-table storage helpers (ADR 0023)."""

import json
import sqlite3
from collections.abc import Sequence
from typing import Any

from concord.models.validation import ValidationFailure
from concord.storage._sql import insert_sql

VALIDATION_FAILURES_SCHEMA = """
-- validation_failures is a MIRROR table (ADR 0019/0023): one row per Stage-1
-- model-contract rejection (X.from_congress_api raised). Rebuildable from the
-- canonical JSONL, so a re-load CONVERGES it via replace-on-load (DELETE the
-- scope, INSERT the current set) rather than appending. No run_id and no
-- load timestamp, on purpose -- a wall-clock column would break byte-identical
-- rebuild. entity_key is the PARENT natural key for child rows (bill_id for
-- bill sections, vote_id for positions). Contrast runs/run_events, which are
-- record tables (ADR 0021). payload holds the offending value as sorted JSON.
CREATE TABLE IF NOT EXISTS validation_failures (
    entity       TEXT NOT NULL,
    entity_key   TEXT NOT NULL,
    source_file  TEXT NOT NULL,
    exc_type     TEXT NOT NULL,
    exc_msg      TEXT NOT NULL,
    field_path   TEXT,
    payload      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_validation_failures_entity_key
    ON validation_failures (entity, entity_key);
"""

_VALIDATION_FAILURE_COLUMNS: tuple[str, ...] = (
    "entity",
    "entity_key",
    "source_file",
    "exc_type",
    "exc_msg",
    "field_path",
    "payload",
)

_VF_INSERT_SQL = insert_sql("validation_failures", _VALIDATION_FAILURE_COLUMNS)


def m004_add_validation_failures(conn: sqlite3.Connection) -> None:
    """ADR 0023: the ``validation_failures`` mirror table.

    ``CREATE TABLE IF NOT EXISTS`` — a no-op on fresh installs (whose
    ``_BASE_SCHEMA`` already declares it) and a creator on pre-0023 DBs. The DDL
    must stay byte-equivalent to the ``_BASE_SCHEMA`` declaration or the
    schema-equivalence test (ADR 0017) fails.
    """
    conn.executescript(VALIDATION_FAILURES_SCHEMA)


def replace_validation_failures(
    conn: sqlite3.Connection,
    failures: Sequence[ValidationFailure],
    *,
    entities: Sequence[str],
    entity_key: str | None = None,
) -> None:
    """Replace-on-load: DELETE the scope, then INSERT ``failures`` (ADR 0023).

    ``entities`` is the family being re-loaded (the delete is ``entity IN (...)``).
    ``entity_key`` narrows the delete for the single-entity ``load_one`` path;
    ``None`` clears the whole family (the full-load path). Always call this — even
    with an empty ``failures`` — so a load that now parses cleanly clears stale
    rows. Pure SQL; the caller owns the transaction.
    """
    placeholders = ", ".join("?" for _ in entities)
    if entity_key is None:
        conn.execute(
            f"DELETE FROM validation_failures WHERE entity IN ({placeholders})",  # noqa: S608 - static placeholders, parameterized values
            tuple(entities),
        )
    else:
        conn.execute(
            f"DELETE FROM validation_failures WHERE entity IN ({placeholders}) AND entity_key = ?",  # noqa: S608 - static placeholders, parameterized values
            (*entities, entity_key),
        )
    if failures:
        conn.executemany(_VF_INSERT_SQL, [_row_from_failure(f) for f in failures])


def count_validation_failures(conn: sqlite3.Connection, *, entity: str | None = None) -> int:
    """Row count, optionally filtered to one ``entity`` — for tests/diagnostics."""
    if entity is None:
        return int(conn.execute("SELECT COUNT(*) FROM validation_failures").fetchone()[0])
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM validation_failures WHERE entity = ?", (entity,)
        ).fetchone()[0]
    )


def _row_from_failure(f: ValidationFailure) -> tuple[Any, ...]:
    """Project a :class:`ValidationFailure` into the column tuple; payload is
    dumped with sorted keys for a byte-stable row (matches runs storage)."""
    values: dict[str, Any] = {
        "entity": f.entity,
        "entity_key": f.entity_key,
        "source_file": f.source_file,
        "exc_type": f.exc_type,
        "exc_msg": f.exc_msg,
        "field_path": f.field_path,
        "payload": json.dumps(f.payload, sort_keys=True),
    }
    return tuple(values[col] for col in _VALIDATION_FAILURE_COLUMNS)
```

## Step 3 — wire it into `src/concord/storage/sqlite.py`

Four edits, mirroring how `runs` was wired:

1. **Import** (near line 46-49):
   `from concord.storage import validation as validation_storage`
   and `from concord.models.validation import ValidationFailure`.
2. **Schema fragment** — add `validation_storage.VALIDATION_FAILURES_SCHEMA` to the
   fold tuple at lines 150-155 (after `runs_storage.RUNS_SCHEMA`).
3. **Migration registration** — append to `_MIGRATIONS` (line 210-214):
   `(4, validation_storage.m004_add_validation_failures),`
   `_HEAD` becomes 4 automatically.
4. **Delegation method** on `SqliteStorage` (beside `insert_run`, ~line 528):

   ```python
   def replace_validation_failures(
       self,
       failures: Sequence[ValidationFailure],
       *,
       entities: Sequence[str],
       entity_key: str | None = None,
   ) -> None:
       """Replace-on-load the validation_failures rows for a load scope (ADR 0023)."""
       with self._maybe_transaction():
           validation_storage.replace_validation_failures(
               self._conn, failures, entities=entities, entity_key=entity_key
           )
   ```
   Optionally also expose `count_validation_failures` for tests.

## Step 4 — `src/concord/pipeline/load_members.py`

The members loader already counts `member` + `term` in `malformed` (its only
class-(b) sites), so the malformed *total* is unchanged; we switch those two
sites from inline-increment to the `failures` list, then add `len(failures)` once.

- Add import: `from concord.models.validation import ValidationFailure`.
- In `load(...)`: create `failures: list[ValidationFailure] = []`.
- **Term site** (lines 96-107): on except, **drop** `malformed += 1`; instead:
  ```python
  failures.append(
      ValidationFailure.from_exc(
          entity="term",
          entity_key=f"{bioguide_id}/{congress}",
          source_file=jsonl_path.name,
          exc=exc,
          payload=payload,
      )
  )
  ```
  (`jsonl_path` is the `load` arg.)
- **Member site** (lines 116-126): on except, **drop** `malformed += 1`; append
  `ValidationFailure.from_exc(entity="member", entity_key=bioguide_id, source_file=jsonl_path.name, exc=exc, payload=payload)`.
- Keep the envelope-parse `malformed += 1` at line 80 (class (a)) and its warning.
- Before `storage.close()` in the `finally`/`try`, after the member loop:
  ```python
  storage.replace_validation_failures(failures, entities=("member", "term"))
  malformed += len(failures)
  ```
  Call it **unconditionally** (even when `failures` is empty) so stale rows clear.
- Trim the two kept `_log.warning` lines to drop their `payload=%r` tail (the
  payload now lives in the table). Keep a concise `entity / key / exc` message.

## Step 5 — `src/concord/pipeline/load_bills.py` (the non-trivial one)

This loader has the tier-1/tier-2 split and the dispatch-table projectors, so the
`failures` collector and `source_file` must thread through several signatures.

- Add import `from concord.models.validation import ValidationFailure`.
- Define the family once: `_BILL_ENTITIES = ("bill", "cosponsor", "action", "subject", "title", "summary")`.

**`load(...)`** and **`load_one(...)`** each own a `failures: list[ValidationFailure] = []`:
- **Tier-1 bill site** (`load` line 83-92; `load_one` line 158-165): on except, **drop**
  `malformed += 1`; append
  `ValidationFailure.from_exc(entity="bill", entity_key=f"{_c}-{_t}-{_n}" (the loop key) / bill_id, source_file=BILLS_JSONL_NAME, exc=exc, payload=payload)`.
- After the tier-2 transaction block, before `storage.close()`:
  - `load`: `storage.replace_validation_failures(failures, entities=_BILL_ENTITIES)`
  - `load_one`: `storage.replace_validation_failures(failures, entities=_BILL_ENTITIES, entity_key=bill_id)`
  - then `malformed += len(failures)` in both.
- Keep the envelope `malformed`/`t2["malformed"]` accounting (class (a)) untouched.

**Thread `failures` + `source_file` into the projectors.** The tier-2 rows are the
class-(b) child sites that are currently uncounted.
- `_load_tier2_section(storage_dir, section, storage, failures)` and
  `_load_tier2_section_for_bill(..., failures)` gain a `failures` param and forward
  it to `_project_section`.
- `_project_section(storage, section, bill_id, payload, fetched_at, failures)`:
  compute `source_file = enrichment_jsonl_name(section)` and pass `source_file` +
  `failures` to the dispatched projector. Update the `_SECTION_PROJECTORS` type
  alias to the new signature
  `Callable[[SqliteStorage, str, dict[str, Any], str, str, list[ValidationFailure]], None]`.
- Each `_project_*(storage, bill_id, payload, fetched_at, source_file, failures)`
  forwards `source_file` + `failures` into `_parsed_rows`.
- **`_parsed_rows`** gains `source_file: str` and `failures: list[ValidationFailure]`
  params. On except, replace the lone `_log.warning(... payload=%r ...)` with:
  ```python
  failures.append(
      ValidationFailure.from_exc(
          entity=row_label,          # "cosponsor" | "action" | "subject" | "title" | "summary"
          entity_key=bill_id,        # parent key
          source_file=source_file,
          exc=exc,
          payload=row,
      )
  )
  _log.warning("skipping %s row for %s: %s", row_label, bill_id, exc)  # trimmed
  ```
  Note `row_label` already equals the `entity` value at every call site
  (`"cosponsor"`, `"action"`, `"subject"`, `"title"`, `"summary"`) — confirm and
  keep them in sync.

## Step 6 — `src/concord/pipeline/load_votes.py`

Votes splits into `_load_house` + `_load_senate`, both writing `vote` /
`vote_position`. The replace must run **once** for the whole load, so thread a
**shared** `failures` list into both sub-loaders and call replace in the top-level
`load()`.

- Add import `from concord.models.validation import ValidationFailure`.
- In top-level `load(...)`: create `failures: list[ValidationFailure] = []`, pass it
  into `_load_house(..., failures=failures)` and `_load_senate(..., failures=failures)`,
  then after both:
  ```python
  storage.replace_validation_failures(failures, entities=("vote", "vote_position"))
  malformed += len(failures)
  ```
  (Add `failures` to the sub-loader signatures; they append, never replace.)
- **House vote site** (line 149-154): drop `malformed += 1`; append
  `ValidationFailure.from_exc(entity="vote", entity_key=vote_id_from_components(*key), source_file=HOUSE_VOTES_JSONL_NAME, exc=exc, payload=payload)`.
- **House positions** — `_parse_position_rows(payload, vote_id)` gains
  `source_file` + `failures` params (caller passes `HOUSE_VOTE_POSITIONS_JSONL_NAME`).
  On except (line 261-265): append
  `ValidationFailure.from_exc(entity="vote_position", entity_key=vote_id, source_file=source_file, exc=exc, payload=row)` and keep a trimmed warning. (This row was **uncounted** before — now it flows into `failures` and thus `malformed`.)
- **Senate vote site** — change the loop at line 216 from `.values()` to
  `.items()` so the `VoteKey` is in scope, and on except (line 217-222) drop
  `malformed += 1`; append
  `ValidationFailure.from_exc(entity="vote", entity_key=vote_id_from_components(*vote_key), source_file=SENATE_VOTES_JSONL_NAME, exc=exc, payload=xml_payload)`.
  (Recovering the key from the envelope is why we iterate `.items()` — the XML body
  failed to parse, but the envelope key still gives us the `vote_id`.)
- **Leave out of scope** (stay plain `_log.warning`, no failure row): the Senate
  roster parse failure (line 197), `unresolved_member` (line 313), and
  `missing_party_or_state` (line 322). These are class (a)/(c), not contract
  violations.
- Keep all envelope `malformed += bad` accounting (class (a)).
- **Per-sub-loader caveat:** each sub-loader still returns a `LoadStats` whose
  `malformed` currently includes its own class-(b) inline counts. After the change,
  the sub-loaders must NOT add class-(b) to their own `malformed`; the single
  `malformed += len(failures)` happens in the top-level `load()` after both run.
  Audit the `malformed` summation at lines 94/105 so class-(b) isn't double-counted.

## Step 7 — tests

- **`tests/test_models_validation.py` (new):** unit-test `ValidationFailure.from_exc`
  — a `ValidationError` yields the expected dotted `field_path`; a `ValueError` and
  a `KeyError` yield `field_path is None`; `exc_type`/`exc_msg` populate.
- **`tests/test_storage_validation_sqlite.py` (new):** mirror
  `test_storage_*_sqlite.py`. Assert: insert N failures; `replace_validation_failures`
  with `entities=(...)` clears the family and re-inserts; with `entity_key=` clears
  only that key; calling with empty `failures` clears stale rows (convergence);
  payload round-trips as sorted JSON.
- **`tests/test_pipeline_bills.py`:** feed a synthetic malformed tier-1 bill payload
  and a malformed tier-2 row (e.g. a cosponsor missing `bioguideId`). Assert a
  `validation_failures` row exists with the right `entity` / `entity_key=bill_id` /
  `field_path`; assert **idempotency** (re-run `load` → same row count, not doubled);
  assert `load_one(bill_id)` scopes the delete to that bill. **Replace** the issue's
  "(and not the prose log)" intent — we keep the prose log, so assert the *row*, not
  the log's absence.
- **`tests/test_pipeline_members.py`:** malformed member + malformed term → two rows
  with `entity="member"` (`entity_key=bioguide`) and `entity="term"`
  (`entity_key="<bioguide>/<congress>"`).
- **`tests/test_pipeline_votes.py`:** malformed house vote, malformed house position,
  and a malformed Senate vote XML → rows; specifically assert the **Senate** row's
  `entity_key` is the recovered `vote_id` even though the XML body didn't parse.
- **`tests/test_sqlite_runs.py` → `TestSchemaVersion`:** `test_head_is_three` now
  fails. Update it (rename to `test_head_is_four`) to assert `_HEAD == 4` and the
  fresh-DB `version == 4`; optionally add a `validation_failures`-present assertion.
- **`tests/test_storage_sqlite.py::test_base_schema_matches_replayed_migrations`**
  needs no change but **must stay green** — it proves `_BASE_SCHEMA` (with the new
  fragment) equals replaying `m004` on a fresh DB. If it fails, the fragment in
  `_BASE_SCHEMA` and the `VALIDATION_FAILURES_SCHEMA` migration DDL have drifted;
  make them byte-identical.

## Validation

```sh
uv run ruff format
uv run ruff check
uv run mypy src
uv run pytest
```

All four must pass. Pay attention to mypy strict on the new `src/` modules
(type-hint everything; `payload: Any` is intentional) and to the schema-equivalence
test.

## Risks / edge cases

- **`--limit`:** a limited `load` deletes the whole family but only re-processes N
  entities, so failures beyond the limit vanish until a full load. Accepted — the
  existing mirror tables have the same property under `--limit`; do **not**
  special-case it.
- **Mid-load crash:** because `replace_validation_failures` runs once at the end
  (DELETE+INSERT atomically inside `_maybe_transaction`), a crash leaves the
  *previous* load's rows intact rather than a half-cleared table. A re-run converges
  everything (mirror-table contract). Don't move the replace earlier.
- **Double-count guard:** the one correctness trap is leaving an old inline
  `malformed += 1` at a class-(b) site *and* adding `malformed += len(failures)`.
  Grep each loader for `malformed += 1` after editing and confirm every remaining
  one is an **envelope** (class (a)) site.
- **`payload` types:** dict for JSON entities, raw XML `str` for Senate votes —
  `json.dumps(..., sort_keys=True)` handles both (a str dumps to a quoted JSON
  string). Don't assume dict.
```
