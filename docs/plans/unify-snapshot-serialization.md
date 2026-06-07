# Unify JSONL snapshot-envelope serialization across the scrapers

> Make the scrapers serialize the ADR 0006 `{fetched_at, key, payload}` envelope through the existing `Snapshot[T]` Pydantic model — the same model the loaders already parse with — instead of hand-rolling `json.dumps` at six sites, so the envelope shape is single-sourced on both the write and read sides.

## Source

Conversation context only (no durable issue/doc). This plan is the output of a thermo-nuclear code-quality audit of the `storage/` + scraper write paths, followed by a grilling pass that resolved the ADR-boundary and wire-format decisions. The audit's headline finding: the SQLite write path is already unified (`SqliteStorage` facade + per-domain modules), so the only real asymmetry is on the **JSONL write side** — the snapshot envelope is parsed through one model on read but hand-rolled three different ways on write.

## Context

Concord's mutable entities (Members, Bills, Votes) persist each upstream fetch as one JSONL line in the **snapshot envelope** shape `{"fetched_at": ..., "key": {...}, "payload": ...}` (see [ADR 0006](../adr/0006-snapshot-on-fetch-for-mutable-entities.md) and [ADR 0009](../adr/0009-multi-endpoint-entities-split-jsonl.md)). The envelope is implemented as a Pydantic generic, `Snapshot[PayloadT]`, defined in [src/concord/models/_common.py:35](../../src/concord/models/_common.py). The **loaders** already validate every JSONL line through it (`Snapshot[dict[str, Any]].model_validate_json(line)` in `load_members.py`, `load_bills.py`, and `Snapshot[str]` for the Senate XML payload in `load_votes.py`).

The **scrapers**, however, do not use `Snapshot`. They each build the envelope as a raw dict and call `json.dumps(...)`, in three different idioms:

- `scraper/members.py` — inline dict + `json.dumps` ([members.py:114](../../src/concord/scraper/members.py))
- `scraper/bills.py` — inline literal, twice (`_scrape_basic_pair` at [bills.py:159](../../src/concord/scraper/bills.py), `_enrich_one_bill` at [bills.py:356](../../src/concord/scraper/bills.py))
- `scraper/votes.py` — a private `_append_envelope(fh, *, iso, key, payload)` helper ([votes.py:290](../../src/concord/scraper/votes.py)), called at 4 sites

So the envelope's canonical shape lives in `Snapshot`, but the write side reimplements it. The fix is to serialize through `Snapshot` on the write side too, via one shared helper.

Why this wasn't already done: [ADR 0018](../adr/0018-pydantic-at-the-load-boundary.md) ("Pydantic validation at the load boundary", currently marked *Proposed* though fully implemented) **Rule 1** deliberately keeps the scraper Pydantic-free *with respect to the payload* — "the scraper writes raw payloads to JSONL unchanged. The scraper does not Pydantic-validate the payload." That contract exists so JSONL faithfully mirrors what the API returned and a model fix is a re-load away, not a re-scrape away. Crucially, Rule 1 is about the **payload**, not the **envelope** (`fetched_at`/`key`, which the scraper authors itself). Serializing the envelope through `Snapshot[Any]` leaves the payload untyped and unvalidated, so it honours Rule 1 while single-sourcing the envelope. This plan ratifies that distinction by amending ADR 0018.

## Goals

1. One shared `append_snapshot(...)` helper in `scraper/_common.py` serializes the snapshot envelope via `Snapshot`, used at all six current write sites.
2. The `concord.models` import is confined to `scraper/_common.py`; the entity scraper modules (`members.py`, `bills.py`, `votes.py`) stay free of direct `concord.models` imports.
3. `votes._append_envelope` and the three inline `json.dumps` envelope literals are gone — no hand-rolled envelope serialization remains in the scraper package.
4. ADR 0018 gains a "Rule 5" documenting write-side envelope serialization through `Snapshot`, and its status is flipped Proposed → Accepted.
5. Full test suite, `mypy --strict src`, and `ruff` all pass.

## Non-goals

1. **Proceedings stay flat.** `JsonlStorage` ([storage/jsonl.py](../../src/concord/storage/jsonl.py)) writes one flat `Proceeding` per line with write-time dedup — a deliberately different contract ([ADR 0002](../adr/0002-jsonl-as-canonical-raw-store.md); ADR 0006 explicitly says do not unify Proceedings into the envelope). Not touched.
2. **`runs.jsonl` cold-backup stays as-is.** `observability.py:_flush` writes a flat `RunRecord` dump (a record table, [ADR 0019](../adr/0019-mirror-tables-vs-record-tables.md)/0021), not an envelope. It cannot use `Snapshot`/`append_snapshot`. Not touched.
3. **No payload validation in the scraper.** The payload stays `Any` and is written verbatim. ADR 0018 Rule 1 is preserved, not weakened.
4. **No change to the SQLite write path.** It is already unified.
5. **No `field_serializer` added to `Snapshot`.** We accept Pydantic's native `fetched_at` rendering (see Approach).
6. **No new base class across entity scrapers** ([ADR 0007](../adr/0007-parallel-pipelines-per-entity.md)). `append_snapshot` is a thin free function, not a base class.

## Relevant prior decisions

- [ADR 0002 — JSONL as the canonical raw store](../adr/0002-jsonl-as-canonical-raw-store.md) — Proceedings are flat; do not envelope them.
- [ADR 0006 — Snapshot-on-fetch for mutable entities](../adr/0006-snapshot-on-fetch-for-mutable-entities.md) — defines the `{fetched_at, key, payload}` envelope.
- [ADR 0007 — Parallel pipelines per entity](../adr/0007-parallel-pipelines-per-entity.md) — no base classes across entity pipelines; shared utilities live in thin `_common.py` helpers.
- [ADR 0009 — Multi-endpoint entities split their JSONL](../adr/0009-multi-endpoint-entities-split-jsonl.md) — bills/votes open multiple handles; the writer must be handle-agnostic (a free function, not a stateful per-file writer).
- [ADR 0018 — Pydantic validation at the load boundary](../adr/0018-pydantic-at-the-load-boundary.md) — **amended by this plan** (new Rule 5; status → Accepted). Rule 1 (no payload validation in the scraper) and the scraper skip rule are preserved.
- [ADR 0019 — Mirror tables vs. record tables](../adr/0019-mirror-tables-vs-record-tables.md) — `runs` is a record table; out of scope.

## Relevant files and code

- `src/concord/models/_common.py:35` — `Snapshot[PayloadT]` generic. **Unchanged**; reused on the write side.
- `src/concord/scraper/_common.py` — shared scraper helpers (currently freshness-map *read* mechanics). **Add `append_snapshot` here** + the single `from concord.models import Snapshot` import. Update the module docstring to note it now owns both envelope read and write mechanics.
- `src/concord/scraper/members.py:74,114-126` — drop the `iso = fetched_at.isoformat()` precompute; replace the inline envelope write with `append_snapshot`.
- `src/concord/scraper/bills.py` — `_scrape_basic_pair` ([:112-195](../../src/concord/scraper/bills.py), envelope at 159-169) and `_enrich_one_bill` ([:314-370](../../src/concord/scraper/bills.py), envelope at 356-366). Re-thread `fetched_at: datetime` in place of `iso: str`; drop the `iso = fetched_at.isoformat()` lines in `scrape_basic` (~230) and `scrape_enrichment` (~408); replace both writes with `append_snapshot`.
- `src/concord/scraper/votes.py` — delete `_append_envelope` ([:290-298](../../src/concord/scraper/votes.py)); re-thread `fetched_at: datetime` through `_scrape_pair` (param at 183), `_fetch_and_write_members` (301), `_scrape_senate_pair` (435), and the roster write in `scrape_senate` (364); drop the `iso` precomputes (107, 359); replace the 4 call sites (244, 326, 365, 455) with `append_snapshot`.
- `docs/adr/0018-pydantic-at-the-load-boundary.md` — amend (Rule 5 + status).
- `tests/test_scraper_members.py:68`, `tests/test_scraper_bills.py:88,255`, `tests/test_scraper_votes.py:134` — the `== FIXED_FETCHED_AT.isoformat()` assertions that break on the wire-format shift; update to parse-and-compare.
- `tests/test_scraper_common.py` — add focused `append_snapshot` round-trip + fail-fast tests.

## Approach

The envelope's canonical shape already exists as `Snapshot[PayloadT]`; the only gap is that serialization was unified on the read side (loaders) and hand-rolled on the write side (scrapers). We close the gap with a single free function:

```python
# src/concord/scraper/_common.py
from typing import IO, Any
from concord.models import Snapshot

def append_snapshot(
    fh: IO[str],
    *,
    fetched_at: datetime,
    key: dict[str, str | int],
    payload: Any,
) -> None:
    """Append one ADR 0006 snapshot envelope line to an open handle.

    Serializes via ``Snapshot`` so the envelope shape is single-sourced
    across the scraper (write) and loader (read) sides. ``payload`` is left
    as ``Any`` — written verbatim, never schema-validated (ADR 0018 Rule 1).
    """
    fh.write(Snapshot[Any](fetched_at=fetched_at, key=key, payload=payload).model_dump_json())
    fh.write("\n")
```

It is a **free function, not a stateful writer**, because bills and votes open several handles at once and write to whichever they just fetched (ADR 0009) — the file lifecycle has to stay with the scraper. This is exactly the shape `votes._append_envelope` already discovered locally; we promote the best-of-the-three to canonical and route the other five sites through it. Storage ownership note: the envelope's *read* helpers (`load_freshness_map`) already live in `scraper/_common.py`, so the *write* helper belongs beside them (not in `storage/`, which owns the unrelated flat-Proceedings `JsonlStorage`).

Two deliberate, accepted behavior changes fall out, both vetted during grilling:

- **Wire format shifts (accepted, not papered over).** `model_dump_json()` emits compact JSON and renders `fetched_at` as `…T14:02:11Z`, where the current `json.dumps({"fetched_at": fetched_at.isoformat()})` emits spaced JSON and `…T14:02:11+00:00`. Both are ISO-8601 UTC for the same instant; both round-trip through `Snapshot[...].model_validate_json` and `datetime.fromisoformat` (Python 3.12 accepts `Z`), so the loaders and freshness maps are unaffected — verified. Real data files will contain mixed `Z`/`+00:00` lines across old and new appends; this is harmless (mutable-entity JSONL has no byte-reproducibility contract, unlike `runs.jsonl`). We do **not** add a `field_serializer` to `Snapshot` to preserve `+00:00`; we accept Pydantic's native output and update the affected test assertions to compare the parsed instant instead of the literal string.
- **Fail-fast on a malformed key (accepted safety net).** `Snapshot.key` is typed `dict[str, str | int]`, so a key value that isn't a scalar now raises `ValidationError` at scrape time. This never fires on current paths — every scraper already skips a record when it cannot construct the key (ADR 0018 scraper skip rule), so the key is always well-formed scalars by envelope-build time. It is a latent guard against future bugs, not a regression. Confirmed current key shapes all satisfy the type: members `{bioguide_id: str, congress: int}`, bills `{congress: int, bill_type: str, bill_number: int}`, votes `{chamber: str, congress: int, session: int, roll_number: int}`, Senate roster `{source: "senators_cfm"}`.

The `fetched_at: datetime` parameter (rather than the pre-stringified `iso`) lets each scraper drop its `iso = fetched_at.isoformat()` precompute and thread the actual `datetime` through its pair/helper functions — a small reduction in stringly-typed plumbing.

There is no import-cycle risk: `concord.models` is a leaf (imports only pydantic/stdlib), so `scraper/_common.py → concord.models` is safe.

## Step-by-step plan

1. **Add `append_snapshot` to `scraper/_common.py`.** Insert the function shown in Approach. Add `from concord.models import Snapshot` and ensure `IO`/`Any` are imported from `typing` (and `datetime` from `datetime`, already present). Add it to `__all__`. Update the module docstring's opening lines so it describes both the freshness-map *read* mechanics (existing) and the envelope *write* mechanics (new) — it remains a thin utility module, not a base class (ADR 0007). Verify: `uv run python -c "from concord.scraper._common import append_snapshot"` imports cleanly.

2. **Route `members.py` through `append_snapshot`.** In `scrape` ([members.py](../../src/concord/scraper/members.py)): delete the `iso = fetched_at.isoformat()` line (74); replace the inline `envelope = {...}` + two `fh.write(...)` lines (114-126) with a single `append_snapshot(fh, fetched_at=fetched_at, key={"bioguide_id": bioguide_id, "congress": congress}, payload=payload)`. Import `append_snapshot` from `._common`. Verify: `uv run pytest tests/test_scraper_members.py` (expect the `fetched_at` assertion at :68 to fail until step 6 — that is the only expected failure).

3. **Route `bills.py` through `append_snapshot`.** Change `_scrape_basic_pair`'s `iso: str` param to `fetched_at: datetime` and `_enrich_one_bill`'s `iso: str` to `fetched_at: datetime`; update `scrape_basic` and `scrape_enrichment` to pass `fetched_at=fetched_at` to those helpers and delete their `iso = fetched_at.isoformat()` lines. Replace the two envelope writes (159-169 detail; 356-366 enrichment) with `append_snapshot(fh, fetched_at=fetched_at, key={...}, payload=...)` and `append_snapshot(handles[section], fetched_at=fetched_at, key={...}, payload=payload)` respectively. Import `append_snapshot`. Verify: `uv run pytest tests/test_scraper_bills.py` (expect only the `fetched_at` assertions at :88/:255 to fail until step 6).

4. **Route `votes.py` through `append_snapshot`; delete `_append_envelope`.** Change the `iso: str` params to `fetched_at: datetime` on `_scrape_pair` (183), `_fetch_and_write_members` (301), and `_scrape_senate_pair` (435); update `scrape_house`/`scrape_senate` to thread `fetched_at` and delete their `iso` precomputes (107, 359). Replace the 4 `_append_envelope(...)` call sites (244, 326, 365, 455) with `append_snapshot(...)`, passing `fetched_at=fetched_at`. Delete the `_append_envelope` function (290-298). Import `append_snapshot`. Verify: `uv run pytest tests/test_scraper_votes.py` (expect only the `fetched_at` assertion at :134 to fail until step 6).

5. **Confirm the entity scrapers are `concord.models`-free.** `grep -n "concord.models" src/concord/scraper/members.py src/concord/scraper/bills.py src/concord/scraper/votes.py` returns nothing; `grep -rn "json.dumps" src/concord/scraper/` shows no envelope serialization remains (only `_common.py` imports `Snapshot`).

6. **Update the broken `fetched_at` test assertions.** In `tests/test_scraper_members.py:68`, `tests/test_scraper_bills.py:88` and `:255`, `tests/test_scraper_votes.py:134`, replace `assert envelope["fetched_at"] == FIXED_FETCHED_AT.isoformat()` with a format-agnostic instant comparison: `assert datetime.fromisoformat(envelope["fetched_at"]) == FIXED_FETCHED_AT` (add `from datetime import datetime` if not already imported). This asserts the actual contract (the captured instant) rather than a cosmetic string. Verify: the three scraper test modules pass.

7. **Add focused tests for `append_snapshot`** in `tests/test_scraper_common.py`: (a) write a snapshot to a tmp handle, read the line back through `Snapshot[dict[str, Any]].model_validate_json`, assert `fetched_at`/`key`/`payload` round-trip; (b) assert a payload with non-ASCII and nested structures survives byte-for-byte (`json.loads(line)["payload"] == payload`); (c) assert a `str` payload (Senate XML case) round-trips; (d) assert a malformed key (e.g. `{"bad": [1, 2]}`) raises `pydantic.ValidationError`, documenting the fail-fast. Verify: `uv run pytest tests/test_scraper_common.py`.

8. **Amend ADR 0018.** Edit `docs/adr/0018-pydantic-at-the-load-boundary.md`:
   - Status line (3): change to `**Status**: Accepted, 2026-05-29; amended 2026-06-06 (Rule 5 — scraper envelope serialization).`
   - Add, after the "Scraper skip rule" subsection within **Decision**, a new subsection:

     > ### Rule 5 — Scrapers serialize the envelope (not the payload) through `Snapshot`
     >
     > Scrapers construct and serialize the ADR 0006 envelope via `Snapshot[Any](...).model_dump_json()` through the shared `append_snapshot` helper in `scraper/_common.py`. The payload is left as the unsubscripted `Any` type, so it is written verbatim and never schema-validated — **Rule 1's payload contract is unchanged.** This makes `Snapshot` the single source of truth for the envelope shape on both the write (scraper) and read (loader) sides, replacing the hand-rolled `json.dumps` that previously diverged across three scraper modules.
     >
     > The `concord.models` import is confined to `scraper/_common.py`; entity scraper modules call `append_snapshot` and stay model-free. Two consequences are accepted: (1) a malformed envelope `key` (a non-`str | int` value) now raises `ValidationError` at scrape time rather than serializing silently — a fail-fast that never fires on current paths, since the scraper skip rule above already guarantees a well-formed key by envelope-build time; (2) `model_dump_json()` emits compact JSON and renders `fetched_at` as `…Z` rather than `…+00:00`. Both are ISO-8601 UTC and round-trip through `Snapshot[...].model_validate_json` and `datetime.fromisoformat`, so existing JSONL and freshness maps load unchanged; data files may contain a mix of both renderings across appends, which is harmless (mutable-entity JSONL carries no byte-reproducibility contract).

9. **Run the full gate.** `uv run ruff format`, `uv run ruff check`, `uv run mypy src`, `uv run pytest`. All clean. Pay attention to mypy on the unsubscripted-generic instantiation — `Snapshot[Any](...)` is written explicitly in the helper to keep the type obvious to mypy strict and to readers.

## Demo seed data

Not applicable — this is a pure serialization refactor. No new tables, columns, entity types, relationships, or API capabilities; `data/` file contents are semantically identical. `backend/demo/seed.sql` (if present) needs no change.

## Testing strategy

- **Unit (new):** `tests/test_scraper_common.py` — `append_snapshot` round-trip, non-ASCII/nested payload fidelity, `str` payload (Senate), and malformed-key `ValidationError` (step 7).
- **Unit (updated):** `tests/test_scraper_members.py`, `tests/test_scraper_bills.py`, `tests/test_scraper_votes.py` — the four `fetched_at` assertions switch to parse-and-compare (step 6). All other assertions in these files already `json.loads` each line, so the compact-vs-spaced whitespace change is invisible to them.
- **Regression (must stay green):** the loader tests (`tests/test_pipeline_*` / `tests/test_storage_*`) read the written envelope format; they parse via `Snapshot`/`json.loads` and must pass unchanged, proving write/read compatibility. Any end-to-end scrape→load test is the strongest signal that the new wire format loads correctly.
- **Static:** `mypy --strict src` (covers the generic instantiation and the new helper signature) and `ruff check`/`ruff format --check`.
- **No live scrapes.** The api.data.gov rate limits make live verification impractical; all scraper tests use recorded fixtures, which is sufficient because the change is purely in serialization, not fetching.

## Acceptance criteria

- [ ] `append_snapshot` exists in `src/concord/scraper/_common.py`, is in `__all__`, and is the sole envelope-serialization path in the scraper package.
- [ ] `grep -rn "json.dumps" src/concord/scraper/` shows no envelope literals; `votes._append_envelope` is deleted.
- [ ] `grep -n "concord.models" src/concord/scraper/{members,bills,votes}.py` returns nothing (only `_common.py` imports `Snapshot`).
- [ ] ADR 0018 has Rule 5 and status `Accepted … amended 2026-06-06`.
- [ ] New `append_snapshot` tests pass, including the malformed-key fail-fast assertion.
- [ ] The four updated `fetched_at` assertions pass against the new wire format.
- [ ] `uv run pytest` is fully green (loaders included).
- [ ] `uv run mypy src` is clean.
- [ ] `uv run ruff check` and `uv run ruff format --check` are clean.

## Open questions

None — all design decisions were resolved during grilling (ADR mechanics: extend 0018 + accept; wire format: accept `Z`, update tests; seam: helper in `scraper/_common.py`; scope: envelope entities only).

## Out-of-band work (optional)

Two lower-value findings from the same audit are **deliberately deferred** (not part of this plan), recorded so they aren't lost:

- The `storage/__init__.py` docstring oversells the `Storage` Protocol as "a seam for a future alternative raw-store implementation" — a leftover framing from the Mongo era (ADR 0013). The Protocol is now Proceeding-specific (it does earn its keep abstracting JSONL-vs-SQLite for the proceedings scrape/load), but the docstring is misleading. A doc-only fix.
- `observability.py:_flush` writes `runs.jsonl` inline; folding it into a `storage/runs.py` helper would give that domain module ownership of all its persistence (SQLite + JSONL backup). Single call site, marginal; a different (non-envelope) shape, so unrelated to this plan.
