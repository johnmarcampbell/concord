# Staleness-aware re-scrape for mutable entities

> Add an opt-in `--skip-unchanged` flag to the mutable-entity scrapers (Bills, Members, Votes) so re-running a partially-completed scrape skips the expensive per-record detail/enrichment fetches when upstream hasn't changed since the last snapshot — making "resume after crash" and "daily incremental" cheap without altering the default behavior.

## Source

Conversation context. The user described a recurring pain point: re-running `concord scrape bills` after a crash mid-scrape of two Congresses' worth of bills refetches every bill from scratch, even ones already snapshotted on the previous attempt. The grilling session for this plan walked the design tree branch-by-branch — see "Approach" for the resolved decisions. (No durable issue/RFC; this paraphrase is the only source.)

## Context

Stage 0 (Scrape) of the Concord pipeline writes JSONL snapshot envelopes for every fetched record. Per [ADR 0006](../adr/0006-snapshot-on-fetch-for-mutable-entities.md), each line is `{"fetched_at": ..., "key": {...}, "payload": ...}`; per [ADR 0002](../adr/0002-jsonl-as-canonical-raw-store.md) the JSONL files are canonical and SQLite is derived/rebuildable. Re-running a Stage 0 command today walks the same list endpoints, fetches every record's detail/enrichment again, and appends fresh snapshot lines. The new snapshots supersede the old ones at Stage 1 (latest-per-key projection), so the result is correct — but the per-record HTTP cost is paid in full every time.

For Bills in particular, `scrape_basic` issues one detail call per bill (≈10k bills per Congress at v1 scope) and `scrape_enrichment` issues five sub-endpoint calls per bill (cosponsors/actions/subjects/titles/summaries — see [ADR 0009](../adr/0009-multi-endpoint-entities-split-jsonl.md)). A crashed two-Congress scrape leaves the scraper with 5,000+ already-snapshotted bills it shouldn't need to refetch on retry.

The api.congress.gov list-endpoint stubs already carry an `updateDate` field (and bills additionally carry `updateDateIncludingText`), which is the server-side "did anything about this record change?" signal. The scraper can read it cheaply during pagination — no extra HTTP cost. Comparing it against the latest `fetched_at` from existing JSONL lets us skip the per-record detail fetch when the server hasn't moved since we last looked.

Senate votes are a different story: they're sourced from senate.gov LIS XML, not api.congress.gov, and the menu XML (`vote_menu_{c}_{s}.xml`) does **not** carry a per-roll update timestamp. See "Approach" for the chamber-asymmetric handling decided in grilling.

Domain terms used here are defined in [CONTEXT.md](../../CONTEXT.md): Bill, Bill type, Member, Bioguide ID, Vote, Chamber, Roll-call number, Session.

## Goals

1. Add a `--skip-unchanged` flag to `concord scrape bills`, `concord scrape members`, and `concord scrape votes` that is **off by default** — invoking these commands without the flag must behave exactly as it does today.
2. When the flag is set, skip the per-record detail/enrichment fetch for any record whose upstream `updateDate` has not advanced past our last snapshot's `fetched_at`. For Senate votes specifically, skip the detail fetch when a snapshot for the roll already exists in `senate_votes.jsonl` (presence-only signal — see Approach).
3. Apply the same comparison **per file** for multi-file entities: Bills enrichment maintains an independent freshness map per `bill_<section>.jsonl`; House votes maintains independent freshness maps for `house_votes.jsonl` and `house_vote_positions.jsonl`.
4. Land a new [ADR 0015 — Staleness-aware re-scrape for mutable entities](../adr/0015-staleness-aware-rescrape.md) documenting per-entity staleness signals, the comparison rule, and the rejected alternatives.
5. Add a thin shared helper in `src/concord/scraper/_common.py` (new file) that loads a freshness map `{key_tuple: latest_fetched_at}` from a JSONL file. ADR 0007 explicitly allows this kind of thin utility.

## Non-goals

1. **Changing default scrape behavior.** No silent change. The flag is opt-in.
2. **Wall-clock freshness windows.** The original conversation floated a `--refresh-after 72h` knob; grilling concluded `updateDate` is the sharper signal, and a second knob would add "which wins?" semantics. Drop entirely; document as rejected in ADR 0015.
3. **Compaction of superseded snapshots.** [ADR 0006](../adr/0006-snapshot-on-fetch-for-mutable-entities.md) flags compaction as a future concern; this plan doesn't touch it.
4. **A resume cursor.** We're not persisting "where did the last run stop?" state — `--skip-unchanged` re-walks every list endpoint and lets the JSONL itself dictate what to skip. The list-endpoint walk is cheap (paginated server-side), so re-walking on resume costs nothing meaningful.
5. **Forcing a re-fetch of a specific record.** No `--force <key>` escape hatch in this change. Workaround if a Senate errata correction needs picking up: run without `--skip-unchanged`. Add `--force` only if usage shows the need.
6. **Proceedings.** They're immutable per [ADR 0002](../adr/0002-jsonl-as-canonical-raw-store.md); the flag does not apply to `concord run proceedings` (or `concord scrape proceedings`, depending on the current verb).
7. **Skipping the list-endpoint walk itself.** Pagination through stubs always happens with the flag set — that's how we discover what to skip.

## Relevant prior decisions

- [ADR 0002 — JSONL as canonical raw store](../adr/0002-jsonl-as-canonical-raw-store.md) — staleness state must be derived from JSONL, never SQLite.
- [ADR 0006 — Snapshot-on-fetch for mutable entities](../adr/0006-snapshot-on-fetch-for-mutable-entities.md) — defines the envelope `{fetched_at, key, payload}` that the freshness map reads.
- [ADR 0007 — Parallel pipelines per entity](../adr/0007-parallel-pipelines-per-entity.md) — explicitly allows a thin `scraper/_common.py`. No base class hierarchy; the helper stays a utility.
- [ADR 0009 — Multi-endpoint entities split JSONL](../adr/0009-multi-endpoint-entities-split-jsonl.md) — names "Each sub-endpoint can refresh independently" as a buy of the per-file split; this plan honors that by maintaining a per-section freshness map for Bills enrichment.
- [ADR 0010 — Votes phased by chamber](../adr/0010-votes-phased-by-chamber.md) — Senate sourced from senate.gov LIS XML, not api.congress.gov; explains why Senate's staleness signal must be different.
- [ADR 0015 — Staleness-aware re-scrape for mutable entities](../adr/0015-staleness-aware-rescrape.md) — **new, created with this plan.**

## Relevant files and code

Files to read or modify:

- [src/concord/scraper/bills.py](../../src/concord/scraper/bills.py) — `scrape_basic` (line 91), `scrape_enrichment` (line 222). Both write ADR-0006 envelopes; both gain a `skip_unchanged` parameter.
- [src/concord/scraper/members.py](../../src/concord/scraper/members.py) — `scrape` (line 33). Gains a `skip_unchanged` parameter.
- [src/concord/scraper/votes.py](../../src/concord/scraper/votes.py) — `scrape_house` (line 74), `_scrape_pair` (line 151), `scrape_senate` (line 262). House uses `updateDate`; Senate uses presence-only.
- `src/concord/scraper/_common.py` — **new file.** Houses `load_freshness_map(path)` and `is_stub_unchanged(...)`.
- [src/concord/cli/bills.py:133](../../src/concord/cli/bills.py:133), [src/concord/cli/bills.py:184](../../src/concord/cli/bills.py:184) — `_run_scrape_bills` and `_run_scrape_bills_enrich`; both gain `--skip-unchanged`.
- [src/concord/cli/members.py](../../src/concord/cli/members.py) — scrape command gains `--skip-unchanged`.
- [src/concord/cli/votes.py](../../src/concord/cli/votes.py) — house + senate scrape commands gain `--skip-unchanged`.
- [src/concord/api.py:203](../../src/concord/api.py:203), [src/concord/api.py:235](../../src/concord/api.py:235), [src/concord/api.py:388](../../src/concord/api.py:388) — `list_members`, `list_bills`, `list_house_votes`; reference only (no edits expected, but confirms stubs carry the fields we rely on).
- [src/concord/senate_xml.py:156](../../src/concord/senate_xml.py:156) — `list_roll_call_numbers`; confirms the Senate menu surface is roll-numbers-only.
- [tests/fixtures/api/bills/list_hr_119.json](../../tests/fixtures/api/bills/list_hr_119.json), [tests/fixtures/api/members/current_house.json](../../tests/fixtures/api/members/current_house.json), [tests/fixtures/api/votes/list_house_119_1.json](../../tests/fixtures/api/votes/list_house_119_1.json) — confirm fixture shapes for `updateDate` / `updateDateIncludingText`.
- [tests/test_scraper_bills.py](../../tests/test_scraper_bills.py), [tests/test_scraper_members.py](../../tests/test_scraper_members.py), [tests/test_scraper_votes.py](../../tests/test_scraper_votes.py) — existing scraper tests; new tests live alongside.
- [CONTEXT.md](../../CONTEXT.md) — no glossary additions required (the new terms are mechanism-level, not domain). If the chamber-asymmetric Vote handling needs surfacing anywhere, it goes in ADR 0015, not CONTEXT.md.

## Approach

The mechanism is the same shape across all three entities, with one branch for Senate:

1. **Build a freshness map at scrape entry.** A new `scraper/_common.py:load_freshness_map(path, key_fields)` reads the JSONL file once, parses each envelope, and returns `{tuple(envelope["key"][k] for k in key_fields): datetime}` keyed by `max(fetched_at)`. O(N) over the file; acceptable at v1 scale (a Bill JSONL with 50k lines parses in well under a second). If the file is missing, return an empty map — no skipping happens.
2. **At each list-endpoint stub, decide whether to skip.** A small helper `is_stub_unchanged(stub, freshness, key, *, signal_field, fallback_field=None)` returns True iff `freshness[key]` exists *and* the parsed `stub[signal_field]` (or fallback) is `<=` it. Both sides are parsed with `datetime.fromisoformat()`; date-only strings are coerced to midnight UTC; either side failing to parse means "treat as stale, don't skip" — the failure mode is an extra HTTP fetch, never a missed update.
3. **The per-entity scrapers consult that decision** before issuing the per-record detail/enrichment HTTP call. The list-endpoint walk and pagination are unchanged. Stubs themselves are never persisted (existing behavior).
4. **For multi-file entities, build one freshness map per file.** The comparison for each file's `is_stub_unchanged` uses the same `bill.updateDate` (or House vote's `updateDate`) on the left, but the right-hand side is the per-file freshness map — so a section already re-pulled this morning can be skipped even when other sections still need a fetch.

### Per-entity staleness signal

| Entity | Signal field on stub | Freshness map source | Skip rule |
|---|---|---|---|
| Bills basic (`bills.jsonl`) | `max(updateDate, updateDateIncludingText)` | `bills.jsonl` keyed `(congress, bill_type, bill_number)` | skip detail fetch if signal ≤ last `fetched_at` |
| Bills enrichment (`bill_<section>.jsonl` × 5) | `max(updateDate, updateDateIncludingText)` from bill stub | one map per section file, same key | skip that section's fetch if signal ≤ last `fetched_at` in *that* section's map |
| Members (`members.jsonl`) | `updateDate` | `members.jsonl` keyed `(bioguide_id, congress)` (composite per [ADR 0006](../adr/0006-snapshot-on-fetch-for-mutable-entities.md)) | skip detail fetch if signal ≤ last `fetched_at` |
| House votes detail (`house_votes.jsonl`) | `updateDate` from list stub | `house_votes.jsonl` keyed `(chamber, congress, session, roll_number)` | skip detail fetch if signal ≤ last `fetched_at` |
| House votes positions (`house_vote_positions.jsonl`) | `updateDate` from list stub | `house_vote_positions.jsonl` same key | independent decision — skip positions fetch if signal ≤ last `fetched_at` in positions map (so a positions fetch that previously failed gets retried on the next run even if detail is fresh) |
| Senate votes (`senate_votes.jsonl`) | *none — menu XML carries no per-roll timestamp* | `senate_votes.jsonl` same key (presence only) | skip detail fetch if **any** snapshot exists for the key |
| Senate roster (`senate_roster.jsonl`) | *not gated* | n/a | always fetched (one call per scrape; not a per-record cost) |

### Key design decisions resolved in grilling

- **Senate uses presence-only skip, not `updateDate`.** The senate.gov menu XML does not expose a modify-date. ADR 0006 notes Senate votes are "mostly immutable once a roll is closed, but errata corrections do appear." Trade-off: missing rare errata corrections automatically. Workaround: run without the flag periodically, or add `--force <roll>` later. ADR 0015 will document this asymmetry.
- **Per-section freshness for Bills enrichment, not bill-level uniform.** Honors ADR 0009's "each sub-endpoint can refresh independently" — a section we re-pulled this morning is skipped even if the bill text changed yesterday.
- **House votes detail and positions are tracked independently.** Same reasoning: a positions-only failure on the previous run (positions fetch is already best-effort per [src/concord/scraper/votes.py:233](../../src/concord/scraper/votes.py:233)) shouldn't strand the roll permanently when detail is fresh. Build two freshness maps; gate each fetch separately.
- **Use `max(updateDate, updateDateIncludingText)` for Bills.** Most conservative; never under-skips. The two fields are not identical (`updateDateIncludingText` lags or leads depending on what changed).
- **Compare timestamps as timezone-aware `datetime`s.** `datetime.fromisoformat()` (Python 3.12+ accepts `Z` and offsets natively); date-only strings get midnight UTC. Unparseable on either side ⇒ treat as stale (fail-safe).
- **Compare direction: skip when `stub.updateDate <= last_fetched_at`.** Equality counts as "skip" — if the server-reported update was at or before our snapshot, we already have it.
- **Flag is off by default.** No silent change to scrape behavior.
- **No `--refresh-after 72h` knob.** Dropped; `updateDate` is sharper. Documented as rejected in ADR 0015.
- **New ADR 0015 — Staleness-aware re-scrape for mutable entities.** Records the contract change (scrape is no longer guaranteed to refetch everything under the flag), the per-entity signal table above, and the two rejected alternatives (wall-clock window; uniform bill-level enrichment).

## Step-by-step plan

1. **Write ADR 0015.** Create [docs/adr/0015-staleness-aware-rescrape.md](../adr/0015-staleness-aware-rescrape.md) following the format used in the existing ADRs in [docs/adr/](../adr/). Sections: Context, Decision, Consequences (Trade-offs accepted, Things this buys, What stays open), Rejected: wall-clock refresh window, Rejected: uniform bill-level enrichment freshness. Status: `Accepted, 2026-05-27`. Cross-reference ADRs 0002, 0006, 0007, 0009, 0010. Verify `ls docs/adr/0015-*.md` shows the new file.

2. **Create `src/concord/scraper/_common.py`.** New module. Exports:
   - `load_freshness_map(path: Path, key_fields: tuple[str, ...]) -> dict[tuple, datetime]` — reads the JSONL line by line; ignores malformed lines (log warning); for each line, builds `tuple(envelope["key"][k] for k in key_fields)`, parses `envelope["fetched_at"]` with `datetime.fromisoformat()`, and keeps the maximum per key. Returns `{}` if the file does not exist. The returned `datetime` is always timezone-aware (assume UTC if naive).
   - `parse_signal_timestamp(raw: str | None) -> datetime | None` — parses both `"2026-04-01"` (→ midnight UTC) and `"2026-04-10T08:00:00Z"` / `"2025-09-09T18:53:19-04:00"` via `datetime.fromisoformat()`. Returns `None` on parse failure or `None` input.
   - `is_stub_unchanged(*, freshness: dict, key: tuple, signal: datetime | None) -> bool` — returns True iff `key in freshness` and `signal is not None` and `signal <= freshness[key]`. Any other case ⇒ False (don't skip; fetch).

   Add a module docstring explicitly noting this is a thin utility per ADR 0007, not a base class. Verify with `uv run ruff check src/concord/scraper/_common.py` and `uv run mypy src`.

3. **Add unit tests for `_common.py`.** New file `tests/test_scraper_common.py` covering:
   - `load_freshness_map`: empty file → `{}`; missing file → `{}`; one envelope → one entry; two envelopes for the same key → keeps the max `fetched_at`; malformed line in middle → logs and continues; envelope with naive `fetched_at` → coerced to UTC.
   - `parse_signal_timestamp`: date-only string → midnight UTC; full ISO Z; full ISO with `-04:00` offset; `None` input → `None`; malformed string → `None`.
   - `is_stub_unchanged`: key absent → False; signal None → False; signal == freshness → True; signal < freshness → True; signal > freshness → False.

   Run `uv run pytest tests/test_scraper_common.py -x`.

4. **Wire `skip_unchanged` into `scraper/bills.py:scrape_basic`.** Add parameter `skip_unchanged: bool = False`. When True, before the loop calls `client.get_bill_detail(...)`, build a freshness map from `storage_dir / BILLS_JSONL_NAME` keyed by `("congress", "bill_type", "bill_number")` (do this once, outside the inner loop). Inside the loop, compute `signal = max(parse_signal_timestamp(stub.get("updateDate")), parse_signal_timestamp(stub.get("updateDateIncludingText")), key=lambda d: d or datetime.min.replace(tzinfo=UTC))` — picking the max while tolerating `None`. Call `is_stub_unchanged(...)`; if True, `continue` (do not increment `written`, do not write an envelope, but **do** increment `seen` and emit a progress event with the same shape so progress UIs still tick).

   Add a new field `bills_skipped: int` to `ScrapeStats` (default 0). Track per-pair and total; surface in the return.

   Run `uv run mypy src` and `uv run ruff check src`.

5. **Wire `skip_unchanged` into `scraper/bills.py:scrape_enrichment`.** Add parameter `skip_unchanged: bool = False`. When True, build *five* freshness maps — one per requested section — keyed by `("congress", "bill_type", "bill_number")`, loading from `storage_dir / enrichment_jsonl_name(section)`. The bill stub used to drive enrichment is no longer in scope here (the function takes `bill_keys` directly), so the *signal* must be threaded in from the caller — see step 7. To keep this step independent: accept an optional `bill_signal_lookup: Callable[[tuple[int, str, int]], datetime | None] | None = None` parameter. When `skip_unchanged` is True and `bill_signal_lookup` is provided, for each `(bill_key, section)` pair: resolve the signal via the callable, look up the per-section freshness, and call `is_stub_unchanged`. Skip the section's fetch when True (don't write, don't count as a section failure, don't increment `snapshots_written`). Add `sections_skipped: int` to `EnrichStats`.

6. **Wire `skip_unchanged` into `scraper/members.py:scrape`.** Add parameter `skip_unchanged: bool = False`. When True, build a freshness map from `storage_path` keyed by `("bioguide_id", "congress")`. The current loop pulls one full Member payload at a time from `client.list_members(...)` (the list endpoint *is* the only endpoint — Members are single-fetch, not two-step). That means we must check the payload itself, not a stub: compute `signal = parse_signal_timestamp(payload.get("updateDate"))` and `is_stub_unchanged(...)`. If True, `continue`. The "saving" here is the JSONL write itself plus subsequent pipeline parse cost — not an HTTP call, since Members aren't two-step. Document this in a one-line comment so a reader doesn't expect HTTP savings. Add `members_skipped: int` to the return tuple. (Current return is `int`; change to a `NamedTuple` `ScrapeStats(members_written: int, members_skipped: int)`. Update the CLI caller in step 9. Note: this is a return-type change, so check existing callers.)

7. **Wire `skip_unchanged` into `scraper/votes.py:scrape_house` and `_scrape_pair`.** Add `skip_unchanged: bool = False` to `scrape_house`. Build two freshness maps once at entry: `detail_freshness` from `house_votes.jsonl` and `positions_freshness` from `house_vote_positions.jsonl`, both keyed by `("chamber", "congress", "session", "roll_number")`. Pass them into `_scrape_pair` (extend its signature). Inside `_scrape_pair`, after parsing `roll_number` from the stub, compute `signal = parse_signal_timestamp(stub.get("updateDate"))` and decide independently:
   - `skip_detail = is_stub_unchanged(freshness=detail_freshness, key=("house", congress, session, roll_number), signal=signal)`
   - `skip_positions = is_stub_unchanged(freshness=positions_freshness, key=("house", congress, session, roll_number), signal=signal)`

   If `skip_detail and skip_positions`, `continue` (both files already have a snapshot at or after the upstream update). Otherwise, fetch only the one(s) that aren't being skipped — `_fetch_and_write_members` already runs independently of the detail write, so a "skip detail, fetch positions" branch is straightforward. Add `votes_skipped: int` and `positions_skipped: int` to `ScrapeStats`.

8. **Wire `skip_unchanged` into `scraper/votes.py:scrape_senate`.** Add `skip_unchanged: bool = False`. Build a presence-only set from `senate_votes.jsonl` (just the keys of the freshness map; the timestamp is unused). Before fetching each roll's detail XML, check `if skip_unchanged and ("senate", congress, session, roll_number) in known_keys: continue`. The roster fetch always runs (one call per invocation, not per record). Reuse the `votes_skipped` counter from step 7. Note in a comment that this is presence-only because the menu XML lacks a modify-date — refer to ADR 0015.

9. **Surface `--skip-unchanged` in the CLI for all four entry points.** Add a Typer `bool` option named `--skip-unchanged` (default `False`) with help text `"Skip records whose upstream updateDate has not advanced since the last snapshot. Senate votes: skip if any snapshot already exists for the roll. See ADR 0015."`. Wire it through:
   - [src/concord/cli/bills.py:133](../../src/concord/cli/bills.py:133) `_run_scrape_bills` → `scrape_basic(..., skip_unchanged=...)`.
   - [src/concord/cli/bills.py:184](../../src/concord/cli/bills.py:184) `_run_scrape_bills_enrich` → `scrape_enrichment(..., skip_unchanged=..., bill_signal_lookup=...)`. The lookup is built by re-loading `bills.jsonl` once via `load_freshness_map` but with a different value type — actually, the cleaner shape is: load each bill's payload to get its `updateDate*`. Two options:
     (a) Extend `load_freshness_map` to also return a `{key: signal_datetime}` map driven from the payload.
     (b) Build a dedicated `load_bill_signal_map(bills_jsonl_path) -> dict[tuple, datetime]` in `_common.py` that parses each line's `payload` for `max(updateDate, updateDateIncludingText)`.

     Prefer (b) — keeps `load_freshness_map`'s signature minimal. Add it to `_common.py` and to step 2's test coverage.
   - [src/concord/cli/members.py](../../src/concord/cli/members.py) — pass through to `scrape(..., skip_unchanged=...)`.
   - [src/concord/cli/votes.py](../../src/concord/cli/votes.py) — pass through to `scrape_house(..., skip_unchanged=...)` and `scrape_senate(..., skip_unchanged=...)`. The same flag governs both subcommands.

   Run `uv run concord scrape bills --help`, `uv run concord scrape members --help`, `uv run concord scrape votes --help` and confirm `--skip-unchanged` appears in each.

10. **Per-scraper unit tests.** Extend each `tests/test_scraper_<entity>.py`:
    - For each entity, add a parametrized test with these cases (per [CLAUDE.md](../../CLAUDE.md) — names tuple, not comma string):
      - `("scenario", "expected_skipped")`:
        - `("flag-off", 0)` — current behavior preserved when flag absent.
        - `("empty-jsonl", 0)` — flag on but no prior snapshots → no skipping.
        - `("stub-older-than-snapshot", 1)` — stub `updateDate` is `2026-01-01`, existing snapshot `fetched_at` is `2026-04-01` → skip.
        - `("stub-newer-than-snapshot", 0)` — stub `updateDate` is `2026-04-01`, existing snapshot `fetched_at` is `2026-01-01` → fetch.
        - `("stub-equal-to-snapshot", 1)` — equality counts as skip.
        - `("stub-date-only-vs-full-iso-snapshot", ...)` — verify mixed-precision comparison: stub `"2026-04-01"` and snapshot `2026-04-01T05:00:00Z` → stub coerced to midnight UTC, < snapshot, so skip.
        - `("stub-missing-updateDate", 0)` — missing/unparseable → fetch (fail-safe).
    - For Bills enrichment specifically: add a case where one section's JSONL is fresh and four others are stale — confirm only the four stale ones are re-fetched.
    - For House votes: add a case where `house_votes.jsonl` is fresh but `house_vote_positions.jsonl` is empty — confirm detail is skipped but positions is fetched.
    - For Senate votes: add presence-only cases (key in JSONL → skip; not in JSONL → fetch). Verify it does not consult any timestamp.

    Run `uv run pytest tests/test_scraper_*.py -x`.

11. **Smoke-test against existing fixtures.** Run the full suite: `uv run pytest`. Confirm no regressions. Confirm typecheck: `uv run mypy src`. Confirm lint: `uv run ruff check && uv run ruff format --check`.

12. **Manual end-to-end check (optional but recommended).** With a `.env` containing a real `CONGRESS_API_KEY`:
    ```sh
    rm -rf /tmp/concord-skip-test && mkdir /tmp/concord-skip-test
    uv run concord scrape bills --congresses 119 --bill-types hr --limit 5 --data-dir /tmp/concord-skip-test
    # Re-run with the flag — expect 5 skips, 0 writes:
    uv run concord scrape bills --congresses 119 --bill-types hr --limit 5 --data-dir /tmp/concord-skip-test --skip-unchanged
    ```
    Confirm the second run reports 5 skipped, 0 written, and that `wc -l /tmp/concord-skip-test/bills.jsonl` is still 5. (CLI flag names like `--data-dir` may differ — adjust to whatever the current CLI exposes; check `--help` first.)

## Demo seed data

Not applicable. This change is a Stage 0 (scrape) behavior change — no new tables, columns, entity types, or API capabilities. The web demo reads the same SQLite tables as today and is unaffected.

## Testing strategy

- **Unit tests for `_common.py`** (step 3): cover `load_freshness_map`, `parse_signal_timestamp`, `is_stub_unchanged`, `load_bill_signal_map`. File: `tests/test_scraper_common.py`.
- **Per-entity scraper tests** (step 10): extend `tests/test_scraper_bills.py`, `tests/test_scraper_members.py`, `tests/test_scraper_votes.py` with the parametrized scenarios listed above. Use the existing fixtures under `tests/fixtures/api/` where possible.
- **Regression risk:** the four scraper functions (`scrape_basic`, `scrape_enrichment`, `scrape` for members, `scrape_house`, `scrape_senate`) all gain a parameter with a `False` default. Existing tests call them without the parameter and must continue to pass. The `Members.scrape` return-type change (int → NamedTuple) in step 6 is the one breaking-style change inside `src/`; update the CLI caller accordingly and check `git grep "concord.scraper.members.scrape("` for any other callers.
- **Manual check** (step 12): end-to-end re-run on a real API key proves the per-record HTTP cost actually drops.
- **No frontend changes**, so no browser walkthrough.

## Acceptance criteria

- [ ] `docs/adr/0015-staleness-aware-rescrape.md` exists and follows the same shape as the existing ADRs.
- [ ] `src/concord/scraper/_common.py` exists with `load_freshness_map`, `parse_signal_timestamp`, `is_stub_unchanged`, `load_bill_signal_map`. Module docstring references ADR 0007.
- [ ] `concord scrape bills --skip-unchanged`, `concord scrape members --skip-unchanged`, `concord scrape votes --skip-unchanged` all run without error and skip records correctly when prior JSONL exists.
- [ ] `concord scrape <entity>` (without the flag) behaves identically to today.
- [ ] All scraper functions accept `skip_unchanged: bool = False`.
- [ ] `ScrapeStats` (bills, votes) gain `bills_skipped` / `votes_skipped` / `positions_skipped` fields; `EnrichStats` gains `sections_skipped`; Members scrape returns a `NamedTuple` with `members_skipped`.
- [ ] `uv run pytest` passes (full suite).
- [ ] `uv run mypy src` passes.
- [ ] `uv run ruff check` and `uv run ruff format --check` pass.
- [ ] `git grep --no-index 'TODO\|FIXME' src/concord/scraper/` shows no new TODOs introduced by this change.
- [ ] ADR 0015 explicitly documents the chamber-asymmetric Vote handling (House: `updateDate`; Senate: presence-only) and the rejected alternatives (wall-clock window, uniform bill-level enrichment freshness).

## Open questions

None — all design decisions resolved during grilling (see "Approach"). One micro-decision noted as "prefer option (b)" in step 9 (a dedicated `load_bill_signal_map` helper over overloading `load_freshness_map`); the executor can flip to (a) if (b) feels worse during implementation, with no design impact downstream.

## Out-of-band work

- **Compaction (future).** [ADR 0006](../adr/0006-snapshot-on-fetch-for-mutable-entities.md) names compaction as a future concern. A compacted JSONL still works fine with `load_freshness_map` — the latest snapshot per key is still discoverable. No coordination needed.
- **`--force <key>` escape hatch (future).** If usage shows Senate errata corrections (or any other "we want to refetch a specific record despite the flag") matter in practice, add a `--force` option that takes a list of keys to never skip. Out of scope for this plan.
- **Cross-entity orchestration (future).** [ADR 0007](../adr/0007-parallel-pipelines-per-entity.md) explicitly leaves "scrape everything since last Tuesday" outside the scrapers themselves. With `--skip-unchanged` available, a simple shell loop calling each entity's scraper becomes effectively an incremental sync. No coordination needed.
