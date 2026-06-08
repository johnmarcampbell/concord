---
status: accepted
---

# Load Validation Failures as a rebuildable mirror table

After [#89](https://github.com/johnmarcampbell/concord/pull/89) tightened each
parser to `X.from_congress_api(payload)` raising on a contract violation (ADR
0018), the Stage 1 loaders catch those exceptions, warn, and skip — surfacing
bad data instead of silently dropping it. But the warnings are unaggregatable
stderr prose: we cannot ask *which field most often drifts?*, *which entity
fails most?*, or *which rows did the last load of bill X drop?*. Issue
[#91](https://github.com/johnmarcampbell/concord/issues/91) asked to make those
records structured and queryable. This ADR records how — and, more importantly,
why the answer is **not** the one the issue sketched.

See `CONTEXT.md` ("Observability") for the **Load Validation Failure** term.

## What we decided

- **A `validation_failures` SQLite table, not JSON logs.** Issue #91's original
  sketch was a `concord/_logging.py` helper emitting one JSON line per failure
  behind `CONCORD_LOG_FORMAT=json`. ADR 0021 had since rejected JSON logs
  project-wide: "the structured/queryable sink is now the DB, so logs only need
  to be a readable human heartbeat plus a correlation key." We honour that — each
  failure becomes a queryable row; the existing `_log.warning` stays as a
  one-line heartbeat, now with its `; payload=%r` tail dropped because the
  payload lives durably in the table.

- **A mirror table (ADR 0019), converged by replace-on-load — deliberately
  *not* a "Load Run" ledger.** A Load Validation Failure is a *deterministic
  function of the canonical raw store*: re-run `load` over the same immutable
  snapshot stream with the same parser code and you get byte-identical failures.
  That is the definition of a mirror table — rebuildable, no cold backup,
  idempotent. So a re-load *converges* the table (DELETE the failures for what is
  being re-loaded, then INSERT the current set) rather than appending. This is
  the load-bearing contrast with ADR 0021's Scrape Run ledger, whose
  `runs` / `run_events` are *record* tables with a `runs.jsonl` backup precisely
  because a scrape outcome is *unreconstructable* (the network is
  non-deterministic). Two failure streams, two storage kinds, on purpose.

- **Scope: the ten model-parse entities only** — Bill, Cosponsor, Bill action,
  legislative subject, title, summary, Vote, Vote position, Member, Term.
  Excludes (a) envelope / JSONL-corruption skips (a malformed `Snapshot` line is
  *local* damage, not upstream drift; it stays a plain warning) and (b) Senate
  LIS→Bioguide bridge misses (a join gap of ours, not a contract violation). The
  invariant this buys: a row in `validation_failures` *always* means "an upstream
  payload violated the model contract."

- **`entity_key` is the parent natural key for child rows.** Bill children
  (cosponsor / action / subject / title / summary) key on the parent `bill_id`;
  vote positions key on the parent `vote_id`. Actions, titles, and summaries have
  no stable per-row identity anyway, and parent-keying lets one column serve both
  the "what did bill X drop?" query and the `load_one` delete predicate — so
  there is no separate scope column. A full `load` deletes by entity-family;
  `load_one` (ADR 0016) narrows the delete by `entity_key`.

- **No load timestamp; payloads serialised with sorted keys.** A mirror table
  must rebuild byte-identically, so there is no wall-clock column — the offending
  snapshot is already pinpointed by `entity_key` + `source_file`, and the payload
  is stored as `json.dumps(payload, sort_keys=True)` for the same byte-stability
  reason (the discipline `runs.jsonl` already uses).

- **`field_path` is the first Pydantic error `loc`, as a scalar.** The primary
  query — "which field started coming back null?" — wants a plain
  `GROUP BY field_path`, so we store the first `exc.errors()[0]["loc"]` as a
  dotted string rather than a JSON array needing `json_each`. `NULL` for
  `ValueError` / `KeyError`, which carry no Pydantic loc. A genuinely multi-field
  failure under-counts its 2nd+ field; acceptable, since contract-tightening
  failures are overwhelmingly single-field, and widening to an array later is
  cheap.

- **`LoadStats.malformed` is corrected to count class (b) consistently.** Today
  it counts envelope corruption plus *top-level* parse failures but silently
  omits dropped *child* rows (cosponsors, positions, …) — a latent under-count
  predating this work. We close that gap, so `malformed = envelope-corruption +
  every model-parse failure`, and the table is the model-parse detail:
  `envelope_failures = malformed − table_rows`.

## Considered options

- **A symmetric "Load Run" ledger (rejected).** Mint a run_id at the load seam,
  accumulate failures as a second Run Event kind (CONTEXT.md does call that term
  "deliberately general"), append forever, back it with JSONL — maximal reuse of
  ADR 0021's machinery and a tidy "every stage execution is a Run" mental model.
  Rejected because it mismodels the data: load failures are *reconstructable*, so
  an append-only, unreconstructable-record treatment would (a) break the
  idempotency contract by growing on every re-run, (b) carry a pointless cold
  backup, and (c) make "the last load's failures" a query over run_ids instead of
  simply *the table's current contents*. The mirror-table treatment answers "last
  load" for free.

- **JSON logs via `CONCORD_LOG_FORMAT=json` (rejected — issue #91's original).**
  The structured sink is the DB (ADR 0021); reviving JSON logs would re-introduce
  exactly what that ADR killed and leave aggregation to log-scraping.

- **A CI gate that fails the build when the drift rate exceeds N/1000 (dropped,
  not deferred).** #91 floated it as future work. It would need a live API call
  inside CI, which the project deliberately avoids; we drop it outright rather
  than carry it as a someday-item.

## Consequences

- A new schema migration (`_m004`) and `_HEAD` bump land per ADR 0017; the
  migration DDL must stay byte-equivalent to the `_BASE_SCHEMA` declaration or the
  schema-equivalence test fails.
- The `_parsed_rows` generator and the `_project_*` projectors in
  `pipeline/load_bills.py` gain a failures-collector parameter — the one
  non-trivial refactor. Child-row failures that were previously logged-and-
  forgotten now flow to both the table and the corrected `malformed` count.
- The Senate vote branch iterates `latest_votes.items()` rather than `.values()`
  so the parent `vote_id` (from the envelope key) is still available as
  `entity_key` even when the XML body itself fails to parse.
- `validation_failures` is queryable for the field / entity aggregations #91
  wanted; any *rendering* of it (a web view) is left to a later, separate effort,
  exactly as ADR 0021 did for `/runs`.
