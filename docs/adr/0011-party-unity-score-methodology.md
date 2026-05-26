# 0011 — Party Unity Score as the party-line methodology

**Status**: Accepted, 2026-05-26.

## Context

The Phase 3 roadmap calls out party-line alignment statistics as the first place editorial neutrality is at risk in Concord: a Member-profile stat that reads "votes 92% with party" is a load-bearing claim, and the way that 92% is computed determines whether it's a defensible journalistic statistic or a vibe number with arithmetic.

Several scoping options were considered:

1. **No party-line stat.** Render the Recent-votes list only; let the reader form their own impression.
2. **Naive "agrees with party majority on every vote".** Denominator = every vote the Member cast; numerator = times they voted with their party's majority. Highly inflated by procedural and unanimous votes.
3. **CQ-style Party Unity Score.** Denominator restricted to *party-unity votes* — votes where a majority of one party opposed a majority of the other; numerator = times the Member voted with their party's majority on those votes.
4. **Multiple side-by-side stats.** Show both a naive number and a party-unity number, let the reader pick.

Concord needs a single number per Member-Congress that means something defensible, and it needs to be derivable from data we already have on the Vote and the vote_positions table.

## Decision

Concord ships a single **Party Unity Score** per `(bioguide_id, congress)`, modeled on the *Congressional Quarterly* Almanac's Party Unity Score methodology — published annually by CQ since 1953 and broadly cited in political science.

Precise definition:

- **Party-unity vote** — a Vote where a majority of Republican-party `vote_positions` (`vote_party = 'R'`) on that Vote oppose a majority of Democratic-party `vote_positions` (`vote_party = 'D'`) on that Vote. Only positions of `'Yea'` or `'Nay'` count toward each party's majority; `'Present'` and `'Not Voting'` are excluded. Independents do not count toward either majority. The vote is party-unity iff the R majority and the D majority are on opposite sides.
- **Denominator** for one Member-Congress — count of party-unity Votes in that Congress where the Member cast `'Yea'` or `'Nay'`. Votes where the Member was Present or Not Voting are excluded from the denominator.
- **Numerator** — of those denominator votes, count where the Member's `position` agreed with their `vote_party`'s majority on that Vote.
- **Score** — `numerator / denominator`, formatted as a percentage.

Independents (Members whose **modal** `vote_party` across the Congress's party-unity votes is `'I'`) get no row in `member_party_unity`; their Member profile shows a muted "No party-unity score (Independent)" note rather than a number.

Members with denominator < 10 in a Congress get a "(not enough votes yet)" treatment — the row exists in `member_party_unity`, but the UI suppresses the percentage and shows the raw count instead. The 10-vote threshold is conventional in the academic literature and avoids the "100% party-line on the only three party-unity votes she's cast" early-Congress noise.

The methodology is exposed to readers at `/about/methodology#party-unity`; the Member profile's score has a `?` icon linking to that anchor.

## Consequences

**Trade-offs accepted:**

- **One denormalization on `vote_positions`.** Computing party majorities per Vote requires knowing each position's party at the time of the vote; we denormalize `vote_party` onto `vote_positions` rather than joining through `terms`. Trade-off: ~2M rows in `vote_positions` at 3-Congress scope each carry a 1-character `vote_party`; the alternative join is several orders of magnitude slower at the index step.
- **Senate is unscored until 3b.** The definition is chamber-aware (party-unity votes are computed per Vote, and Senate Votes don't land until 3b per [ADR 0010](./0010-votes-phased-by-chamber.md)); Senate Members' Party Unity Score rows are absent until then.
- **Party switchers get a "modal party" assignment.** Sinema's switch from D to I mid-Congress is the canonical case. We resolve `member_party_unity.party` as the Member's modal `vote_party` across party-unity Votes in the Congress, not their last-known party. Honest, but loses the temporal detail.
- **The score doesn't tell you everything about a Member.** Procedural unanimity inflates the naive number; restricting to party-unity Votes deflates it relative to readers' intuitions. The methodology page exists precisely so the number's meaning is legible rather than implicit.

**Things this buys:**

- **A defensible single number.** Naming the methodology after CQ's published one lets the methodology page point to decades of political-science literature. Readers, journalists, and anyone re-using Concord data have a citeable definition.
- **Computable from data we already have.** The denominator and numerator are SQL aggregations over `votes` + `vote_positions`. No model layer, no editorial judgment in the indexer.
- **Independents are surfaced, not hidden.** Showing "no party-unity score (Independent)" makes the methodology's reach legible; silently rendering 0% or omitting the Member would mislead.
- **Low-N suppression prevents headline-grade nonsense.** A freshman Senator in week 2 with two party-unity votes under their belt doesn't get a "100% with party" badge; the UI says "voted on 2 party-unity votes — not enough for a stable score yet."

**What stays open:**

- **Historical trend chart on the Member page** — the current Congress is featured prominently; older Congresses appear as a small numeric strip. A visualization belongs to Phase 7.
- **Other derived stats** (vote attendance %, bipartisan-coalition membership, leadership-against-rank-and-file score). Each would land as its own ADR; none are in scope for 3a.
- **Refinements to "modal party"** for the rare Member who genuinely splits across parties within one Congress. Current behavior is mode-with-ties-broken-arbitrarily; if a real edge case surfaces we revisit, ideally as an ADR amendment rather than a silent code change.

## Rejected: no party-line stat

Refusing to publish any party-line number would dodge the editorial-neutrality risk. It was rejected because the Recent-votes list itself invites the reader to compute a vibe number from a handful of recent rows — and the vibe number is worse than a methodology-backed one. Refusal punts the editorial work onto every reader independently.

## Rejected: naive "agrees with party majority on every vote"

The naive number is dominated by unanimous and procedural votes — naming-a-post-office bills, motions to recess, journal approvals — where every Member of every party votes the same way. The result is a percentage in the 95–99% range for almost every Member, with no useful signal about how often they break from their party when it matters. Rejected because it produces a confidently meaningless number, which is worse than no number.

## Rejected: side-by-side display of multiple methodologies

Showing both the naive and the party-unity number invites the reader to pick the one that flatters their argument. It also doubles the methodology surface to explain. Rejected in favor of a single number with one methodology page that defends it.
