# 0015 — Staleness-aware re-scrape for mutable entities

**Status**: Accepted, 2026-05-27.

## Context

[ADR 0002](./0002-jsonl-as-canonical-raw-store.md) makes JSONL the canonical raw store and SQLite a derived projection. The contract is "every fetch appends a line"; re-running a Stage 0 scrape always issues every per-record HTTP call again. [ADR 0006](./0006-snapshot-on-fetch-for-mutable-entities.md) layers the snapshot-on-fetch envelope (`{fetched_at, key, payload}`) on top of that for mutable entities (Members, Bills, Votes). Re-running converges to a correct projection because Stage 1 picks the latest snapshot per key.

The cost of that contract is paid per record. For Bills, `scrape_basic` issues one detail call per Bill (≈10K per Congress at v1 scope) and `scrape_enrichment` issues five sub-endpoint calls per Bill (per [ADR 0009](./0009-multi-endpoint-entities-split-jsonl.md)). A scrape of two Congresses that crashes 60 % through refetches every previously-snapshotted Bill on retry. House votes pay a 2× cost (detail + per-member positions). Members pay only the list-walk (single-fetch), so the per-record cost is the JSONL write itself, not an HTTP call.

The api.congress.gov list-endpoint stubs carry a per-record `updateDate` (and Bill stubs additionally carry `updateDateIncludingText`). Walking that stub is free — pagination has to happen regardless. Comparing the stub's `updateDate` against the latest `fetched_at` already in JSONL lets the scraper skip the per-record detail/enrichment fetch when the server hasn't moved since we last looked. Senate votes are different: senate.gov LIS menu XML (`vote_menu_{c}_{s}.xml`) carries no per-roll modify-date, so the comparison degrades to presence-only.

## Decision

Add an **opt-in** `--skip-unchanged` flag to `concord scrape bills`, `concord scrape members`, and `concord scrape votes`. With the flag set, the scraper skips the per-record detail/enrichment fetch when the upstream `updateDate` has not advanced past the latest `fetched_at` already in JSONL for that key. With the flag absent, behavior is identical to today.

Per-entity staleness signal:

| Entity | Signal field | Freshness map source | Skip rule |
|---|---|---|---|
| Bills basic (`bills.jsonl`) | `max(updateDate, updateDateIncludingText)` on stub | `bills.jsonl` keyed `(congress, bill_type, bill_number)` | skip detail fetch if signal ≤ last `fetched_at` |
| Bills enrichment (`bill_<section>.jsonl` × 5) | `max(updateDate, updateDateIncludingText)` from `bills.jsonl` payload | one map per section file, same key | skip that section's fetch if signal ≤ last `fetched_at` in that section's map |
| Members (`members.jsonl`) | `updateDate` on payload | `members.jsonl` keyed `(bioguide_id, congress)` | skip JSONL write if signal ≤ last `fetched_at` |
| House votes detail (`house_votes.jsonl`) | `updateDate` on stub | `house_votes.jsonl` keyed `(chamber, congress, session, roll_number)` | skip detail fetch if signal ≤ last `fetched_at` |
| House votes positions (`house_vote_positions.jsonl`) | `updateDate` on stub | `house_vote_positions.jsonl`, same key | independent decision — skip positions fetch if signal ≤ last `fetched_at` in positions map |
| Senate votes (`senate_votes.jsonl`) | *none — menu XML carries no per-roll timestamp* | `senate_votes.jsonl`, same key (presence only) | skip detail fetch if any snapshot exists for the key |
| Senate roster (`senate_roster.jsonl`) | *not gated* | n/a | always fetched (one call per scrape) |

Comparison rules:

- Both sides parse via `datetime.fromisoformat()` (Python 3.12+ accepts `Z` and offsets). Date-only strings are coerced to midnight UTC. Naive `fetched_at` values are coerced to UTC. Equality counts as "skip."
- If either side fails to parse, treat the record as stale and fetch. The failure mode is one extra HTTP call, never a missed update.
- A missing freshness entry (no prior snapshot for the key) always falls through to "fetch."

The mechanism is implemented in `src/concord/scraper/_common.py` (`load_freshness_map`, `parse_signal_timestamp`, `is_stub_unchanged`, `load_bill_signal_map`). Per [ADR 0007](./0007-parallel-pipelines-per-entity.md) this is a thin utility, not a base class — each entity scraper consults it directly.

## Consequences

**Trade-offs accepted:**

- **The "every fetch appends a line" contract weakens under `--skip-unchanged`.** ADR 0002's strict idempotency assumes every re-run issues every HTTP call. The flag opts out per record. Default behavior is unchanged; the weaker guarantee applies only when the flag is set, and is documented here so a reader can find it.
- **Senate errata corrections are silently missed under the flag.** ADR 0006 notes Senate rolls are "mostly immutable once closed, but errata corrections do appear." Presence-only skipping cannot detect a re-issued vote XML for a roll we've already snapshotted. Workaround: re-run without the flag periodically.
- **The signal lives on the list-endpoint stub, not on the detail payload.** We trust api.congress.gov to advance `updateDate` whenever any part of the detail or sub-resource changed. If they don't, we'll skip records that have in fact moved. No mitigation; the alternative (fetch the detail to compare) defeats the entire purpose of the flag.
- **One extra full pass through each JSONL file at scrape entry.** `load_freshness_map` reads the file line-by-line once per invocation. At v1 scale (≤50K lines per file) this is well under a second. Acceptable.

**Things this buys:**

- **Resume-after-crash becomes free.** A two-Congress scrape that died after Congress 117 finished and Congress 118 reached 6,000/10,000 bills now retries Congress 118 with 6,000 skips and 4,000 fetches instead of paying full HTTP cost on all 10,000.
- **"Daily incremental" is just `--skip-unchanged`.** No new orchestration, no cursor file. The list-walk is the cursor; the JSONL is the state.
- **Per-section refresh is honoured.** A Bill whose `/actions` we re-pulled this morning but whose `/cosponsors` is stale only re-fetches `/cosponsors`. Falls out of ADR 0009's per-section JSONL split — which explicitly named this as a buy.
- **Detail and positions for House votes refresh independently.** A previous run that wrote `house_votes.jsonl` but failed `house_vote_positions.jsonl` (which is already best-effort per the existing scraper) retries only positions on the next run. The flag does not strand the roll permanently.

**What stays open:**

- **Compaction interacts cleanly.** ADR 0006 leaves compaction as future work. A compacted JSONL still surfaces the latest snapshot per key, so `load_freshness_map` works unchanged.
- **No `--force <key>` escape hatch.** If usage shows Senate errata corrections (or any other "refetch this one record despite the flag") matters in practice, add it later. Today's workaround is "run without the flag."
- **No wall-clock `--refresh-after 72h` knob.** See "Rejected" below.

## Rejected: wall-clock refresh window

An earlier design floated a `--refresh-after 72h` flag — fetch any record whose latest snapshot is older than 72 hours, regardless of `updateDate`. Rejected because:

1. **`updateDate` is the sharper signal.** It says "the upstream record actually changed." Wall-clock age says "it might have changed and we haven't checked." For Bills (where `updateDate` is reliable), age is strictly worse — it forces refetches of records the server tells us are still current.
2. **Two flags would need precedence rules.** "Skip unchanged" and "refresh after N hours" can both fire on the same record. Documenting which wins is doable but adds API surface for a knob we don't actually need yet.
3. **The age fallback is already covered by "don't pass `--skip-unchanged`."** A nightly cron that wants to brute-force a refresh runs without the flag; an incremental cron wants the sharpest signal available. No middle ground worth shipping.

Revisit if a real workflow appears that needs "refresh records we haven't touched in N days even though `updateDate` says they're current."

## Rejected: uniform bill-level enrichment freshness

The simplest enrichment shape was "one freshness check per Bill — if the latest snapshot in any of the five `bill_<section>.jsonl` files is at or after `updateDate`, skip all five sub-endpoint fetches." Rejected because:

1. **It throws away the per-section granularity ADR 0009 explicitly bought.** If `/cosponsors` succeeded yesterday and `/actions` failed yesterday, a bill-level check would mark the Bill "fresh" and skip the `/actions` retry permanently (or until `updateDate` advances). The whole point of split JSONL is that each sub-endpoint refreshes independently.
2. **It pessimises the common case.** Most enrichment failures we've seen are single-section (rate limit, transient 5xx). Per-section freshness retries exactly the section that failed; bill-level freshness either retries all five (wasteful when four are fine) or none (silently strands the failing section).

Per-section freshness maps cost one extra `load_freshness_map` call per section at scrape entry — five total, all O(N) on small files. The marginal cost is negligible; the upside is correctness.

## Rejected: comparing against payload-level timestamps after a detail fetch

Another option was "fetch the detail, then compare the detail's `updateDate` to the snapshot's `fetched_at`, and skip the JSONL write if equal." Rejected because the HTTP cost is the cost we're trying to avoid — by the time we have the detail in hand, the work is done. Reading `updateDate` off the *stub* is the only place a skip saves anything.
