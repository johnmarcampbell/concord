# 0028 — Stage-1 load atomicity granularity: atomic for members/bills/votes, per-record for proceedings

**Status**: Accepted, 2026-07-07.

## Context

[ADR 0006](./0006-snapshot-on-fetch-for-mutable-entities.md) makes every Stage-1 loader a projection of a JSONL snapshot stream into the derived SQLite store, and [ADR 0002](./0002-jsonl-as-canonical-raw-store.md)/[0003](./0003-sqlite-as-derived-store.md) make that store rebuildable — the JSONL is canonical, so a load that fails partway can always be re-run to convergence. Every loader is idempotent by natural-key dedup.

Until [#150](https://github.com/johnmarcampbell/concord/pull/156) (single transaction owner) landed, *who committed* during a load was **accidental** — a side effect of which projector happened to self-commit:

- `load_members` opened no `transaction()`, so each `upsert_member` self-committed → loading ~535 Members meant ~535 fsyncs.
- `load_bills` wrapped its five tier-2 sections in one `transaction()` but committed tier-1 `upsert_bill` per row.
- `load_votes` already wrapped its whole load in one `transaction()`.

With commit ownership now uniform (every write routes through `SqliteStorage._maybe_transaction()`, which joins an open `transaction()` or opens its own), each loader *can* choose its granularity freely. Committing is the slow part of a load (each commit fsyncs), so the choice is not free speed — it is a real semantic decision about failure behaviour: **wrap the whole load in one `transaction()`** (one commit, all-or-nothing) **vs. commit per record** (more fsyncs, but partial progress survives a mid-load crash). This ADR records that choice per entity.

## Decision

Split the four Stage-1 loaders on whether a load has a **cross-row invariant** to protect:

**Whole-load-atomic (one `transaction()` around the whole load) — `load_members`, `load_bills` (tier-1 + tier-2), `load_votes`:**

- Each load projects a **logical unit across multiple tables** — a Member plus its DELETE-then-INSERT `member_terms`; a Bill plus its five tier-2 sections; a Vote plus its `vote_positions` — and/or **converges the `validation_failures` family** in the same pass ([ADR 0023](./0023-load-validation-failures-mirror-table.md)). Atomicity is therefore a genuine benefit: a crash never leaves a member with half its terms, a bill with three of five sections, or the failures mirror inconsistent with the rows actually written.
- Volumes are **bounded** (current-Congress scale), and the codebase already accepts a single large transaction here: `load_votes` wraps its whole load today, and `load_bills` already commits up to tens of thousands of tier-2 rows in one transaction. Folding tier-1 into that same transaction is strictly less than what tier-2 already does.
- Because re-running is free (idempotent, JSONL-canonical), the per-record alternative's only advantage — partial progress survives a crash — buys nothing: the fix for a failed atomic load is to re-run it, which a per-record load would also require. So we take the faster, cleaner all-or-nothing path and collapse N (or 5N) fsyncs to one.
- The family-wide `replace_validation_failures` call runs **inside** the same transaction (as `load_votes` already does), so the mirror table and the entity rows commit together.

**Per-record commit (no enclosing `transaction()`) — proceedings Stage-1 (`cli/proceedings.py::_run_load`):**

- The proceedings projection is a stream of **independent single-row inserts with no cross-row invariant** — one `Proceeding` per `granule_id`, one table, nothing to keep atomic across rows. The atomicity argument that justifies batching the other three is simply absent.
- Proceedings is the **highest-volume, largest-row loader**: the Congressional Record is daily and each row carries the full article text, so a multi-decade backfill is hundreds of thousands of large rows. Wrapping that in one transaction would grow the WAL without bound before a single commit and forfeit incremental durability across a long-running load.
- Per-record commit preserves the **crash-safe resume contract** already documented for the sibling online `pull` pipeline (`load_proceedings.pull`): "writes one Proceeding at a time … killing the process loses at most the in-flight article; the next invocation picks up only what's missing." Keeping Stage-1 per-record too means both proceedings write paths share one failure model.

This is the deliberate per-entity choice deferred from the commit-ownership cleanup ([#149](https://github.com/johnmarcampbell/concord/issues/149)): it converges the *accidentally* inconsistent granularity onto a *principled* split, not a uniform rule.

## Consequences

- **Members / bills / votes loads are now all-or-nothing.** A crash or a raised exception anywhere in the load (including in the final `replace_validation_failures`) rolls back the entire batch; nothing is half-projected. Re-running converges. New pipeline tests assert this rollback for members and bills; votes already had the transaction.
- **Members loads drop from ~535 fsyncs to one; bills tier-1 from one-per-bill to zero-extra** (it joins the tier-2 transaction). This is the performance win #149 was filed for, now taken deliberately rather than by accident.
- **Proceedings Stage-1 is unchanged** — the per-record commit it already had is now the *documented* choice, not an accident of the old self-committing `write`. A future "DRY the loaders" pass must not wrap it in a `transaction()`: doing so would balloon the WAL on a backfill and break the resume contract above. This ADR is the guard against that.
- **No new pattern.** This uses the existing `transaction()` / `_maybe_transaction()` seam from #150; it introduces no periodic-checkpoint machinery (commit-every-N), which would be a third idiom. If a proceedings backfill ever proves too slow per-record *and* too large to wrap whole, periodic checkpointing is the escalation — deliberately out of scope until it bites.

## Rejected: one uniform rule for all four loaders

Wrapping *every* loader (including proceedings) in one `transaction()` for consistency. Rejected because it optimises the wrong axis: proceedings has no cross-row invariant to gain from atomicity, is the one loader whose volume makes a single transaction genuinely costly (unbounded WAL growth, no incremental durability), and has a documented per-record resume contract that a whole-load transaction would silently break. "Consistent granularity" is not the goal — *deliberate, justified* granularity is; a rule that's wrong for the highest-volume loader is worse than a principled split.

## Rejected: leave all four as-is (per-record everywhere the old code happened to self-commit)

Do nothing beyond #150. Rejected because the old granularity was an accident of which projector self-committed, not a decision — `load_members`' ~535 fsyncs and `load_bills`' per-row tier-1 commits were pure overhead with no atomicity benefit to show for them, and the inconsistency with `load_votes` was exactly the "accidentally inconsistent" seam #149 was filed to resolve.
