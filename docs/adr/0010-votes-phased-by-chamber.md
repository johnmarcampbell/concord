# 0010 — Votes phased by chamber, not metadata-vs-positions

**Status**: Accepted, 2026-05-26.

## Context

The Phase 3 sketch in [docs/plans/members-bills-votes-roadmap.md](../plans/members-bills-votes-roadmap.md) named "Votes" as one phase fed by `/v3/house-vote` and `/v3/senate-vote` on api.congress.gov, with clerk.house.gov / senate.gov bulk XML augmenting the API where it was "thin" — meaning, in the roadmap's framing, that the API would carry metadata and totals but the XML would be needed for full per-member positions.

A spike against the live API (preserved as findings in the Phase 3a plan; the spike files themselves are deleted with that plan) found two facts that don't fit that framing:

1. **api.congress.gov has no Senate vote endpoint at all.** `/v3/house-vote/{c}/{s}/{roll}` works, but every Senate variant (`/v3/senate-vote`, `/v3/senate-rollcall-vote`, `/v3/senate-roll-call-vote`, the bare `/v3/senate-vote/{c}/{s}/{roll}`) returns 404. "Thin" turned out to mean "absent."
2. **The House endpoint delivers full per-member positions** at `/v3/house-vote/{c}/{s}/{roll}/members`. Bioguide-keyed; one ~100 KB call per roll. The clerk.house.gov XML the roadmap reached for is not needed — the API already has it.

Together these collapse the roadmap's implied metadata-vs-positions split. The work that's needed isn't "first API, then XML for positions"; it's "House from one source end-to-end, Senate from a different source end-to-end."

## Decision

Phase 3 is split by **chamber**, not by metadata-vs-positions:

- **Phase 3a — Votes (House)** uses api.congress.gov end-to-end. Two endpoints per roll: detail + members. Populates `votes` (chamber `'house'`) and `vote_positions` (Bioguide-keyed).
- **Phase 3b — Votes (Senate)** uses senate.gov LIS XML end-to-end. Builds a Bioguide↔LIS-ID mapping. Populates the same `votes` (chamber `'senate'`) and `vote_positions` tables.

Each phase has exactly one upstream source. The two phases write into the same SQL tables; downstream surfaces (`/votes/...`, the Bill-page Vote history, the Member-page Recent votes + Party Unity Score) become "complete" only after 3b lands.

## Consequences

**Trade-offs accepted:**

- **Asymmetric coverage during the gap between 3a and 3b.** A Senate Member's profile shows a "(Phase 3b)" placeholder for both Recent votes and Party Unity Score; the Vote-history section on a Senate-originated Bill is empty until 3b ingests senate.gov XML. Mitigated by surfacing the placeholder explicitly rather than silently rendering empty sections.
- **Party Unity Score is House-only at 3a ship.** The methodology in [ADR 0011](./0011-party-unity-score-methodology.md) is defined chamber-by-chamber; no row in `member_party_unity` exists for Senate Members until 3b. The cross-chamber comparability the roadmap implied is delayed.
- **A `--chambers senate` no-op CLI switch lives for one phase.** `concord scrape votes --chambers senate` logs a "lands in Phase 3b — skipping" message and does no work. Removed at the start of 3b. The cost of carrying it is one branch in the CLI for the duration of one phase; the benefit is that the operator's command vocabulary doesn't change between phases.

**Things this buys:**

- **Each phase has one source.** No "fetch from API; if absent, fall back to XML" branch in the Vote scraper. 3a is "call api.congress.gov, write JSONL"; 3b is "fetch senate.gov XML, parse, write JSONL." Each phase is testable against captured fixtures from one upstream.
- **The schema is the same across phases.** `votes.chamber` accepts `'house'` or `'senate'`; the `vote_positions` shape doesn't care which side wrote the row. 3b adds no tables; it adds a Senate branch to the existing scraper module and a Senate branch to the existing loader.
- **Partial value lands at 3a.** A House-only Vote layer is still useful: most party-line battles play out in the House first; the Member-page Party Unity Score is the highest-value editorial stat the roadmap names, and it's well-defined House-only (House Members; House votes; House cross-party splits).
- **The Bioguide↔LIS-ID mapping work is contained to 3b.** Senate roll-call XML uses LIS IDs (the Senate's internal Legislative Information System identifier) rather than Bioguide IDs; the join table that lets us reuse `vote_positions.bioguide_id` is a 3b concern only. 3a doesn't pay for it.

**What stays open:**

- **Cadence parity between the two sources** — once 3b ships, daily refresh has to walk both upstreams. Operator-driven via `concord run votes` per Phase 3a's stance; remains operator-driven in 3b.
- **Coverage uniformity for the Vote-history section on cross-chamber Bills** — a Bill that originated in the House may have Senate votes against it; 3a renders only the House half until 3b lands. The empty Senate rows are not flagged; the section just renders fewer rows. Acceptable for the short window between 3a and 3b.

## Rejected: one Phase 3 covering both chambers via the original metadata-vs-positions split

The roadmap's framing — "API for metadata; XML for positions" — would have required, at v1, that the scraper hit api.congress.gov for House and Senate metadata, then clerk.house.gov / senate.gov for House and Senate positions. The spike eliminated the first leg of the Senate version (no API). What remained was "API for House metadata + API for House positions + XML for Senate metadata + XML for Senate positions" — i.e., the chamber split, with a misleading framing.

The asymmetry isn't between metadata and positions; it's between chambers. Naming it by chamber surfaces that asymmetry where it belongs (in the phase names) instead of burying it in a phase whose scope description doesn't match the work.

## Rejected: defer all of Votes until both sources can ship together

A single Phase 3 that waits for senate.gov XML parsing before any Vote data lands was rejected because the House half is much smaller in surface area (one API client, one fixture set, no LIS-ID mapping) and produces a fully working Party Unity Score for ~430 Members — the headline editorial stat. Holding that back to ship simultaneously with senate.gov XML parsing trades months of "no Vote data" for a uniformity that only matters once the surface stops looking new.
