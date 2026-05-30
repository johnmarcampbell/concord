# 0020 — Bill Brief: an LLM-generated executive summary

**Status**: Accepted, 2026-05-30. Depends on [ADR 0019](0019-mirror-tables-vs-record-tables.md). First chat/completions use of the OpenAI dependency introduced for embeddings in [ADR 0004](0004-openai-embeddings.md).

## Context

A Bill profile page already shows everything Concord scrapes: identity, sponsor, cosponsors, action history, subjects, titles, the CRS summaries, and vote history. That is comprehensive but not *fast to absorb*. The use case driving this ADR is a policymaker or staffer who wants to come up to speed on a bill quickly — and, often, to come up to speed *for a purpose*: to brief a principal, to prepare an argument, to frame the bill for a particular audience.

We want a **Bill Brief**: a short, readable distillation that sits on top of the structured data. Two hard requirements shaped the design:

1. **Honesty is non-negotiable.** A tool that lets a user spin the facts is worse than no tool. The facts must be fixed; only their framing may flex.
2. **Cheap and bounded.** One outbound LLM call per brief, not a multi-step agent. The bill's data is already well-curated (the CRS summaries are themselves human-authored summaries), so the model's job is synthesis, not extraction from raw legalese — a single call is sufficient.

The brainstorm that produced this ADR converged on a clean separation: a deterministic template carries all the facts, and the LLM authors exactly one thing.

## Decision

A Bill Brief has two layers.

**The deterministic layer (no LLM).** Everything factual is computed in SQL / Python and rendered identically every time, with sections dropping out when their data is absent. This is the existing Bill profile content plus a compact **fact pack** (`concord.brief.BriefFacts`): the identifier, title, sponsor, policy area, introduced / latest-action dates, cosponsor counts (total / original / withdrawn) and a best-effort party split, subject list, action and vote counts, and the **most recent CRS summary shown verbatim and labeled with its stage** (the summary's `version_code` / `action_desc` + `action_date`). The CRS summary is the authoritative "what the bill does," straight from the source, and the profile's existing Summaries section already renders it stage-labeled.

**The generated layer (one LLM call).** The model authors exactly one field — an **executive summary** (`concord.brief.GeneratedBrief.executive_summary`). It is the only generated content and the only conditionable content. v1 ships this single field; the structured-output shape (`GeneratedBrief`, a Pydantic model returned as a JSON object) is deliberately extensible — adding a field later means extending the model, the prompt, and bumping `BRIEF_PROMPT_VERSION`.

**Conditioning ("the lens").** The generation form carries an optional free-text lens — "emphasize fiscal impact for a budget-committee audience," "frame for a rural-healthcare brief." The lens steers *emphasis, framing, and audience*; it can never alter a fact. This is enforced structurally, not by trusting the model to balance honesty against advocacy in one breath:

- The deterministic fact pack and the verbatim CRS summary are handed to the model as ground truth it may narrate but must not contradict. The numbers live on the page next to the prose, so a tailored summary cannot quietly disagree with a vote count the reader can see.
- The system prompt's honesty rules sit *above* the user's lens in priority and say so explicitly: honor the requested emphasis; never misstate, omit, or distort the bill's actual status, scope, or effects; always surface the strongest honest counterpoint to a one-sided framing; stay neutral in tone. The lens is delimited as an "emphasis request," not as instructions that can override these rules.

**Honesty floor:** because the lens conditions the executive summary itself (an aide tailoring a brief for a principal is a first-class use case, not a misuse), the always-neutral anchor is the *fact pack*, not a neutral paragraph. The generated prose floats above an immutable block of facts.

**The single call.** `concord.brief.Briefer` wraps an injected OpenAI-compatible client exactly as `concord.embedding.Embedder` does (constructor injection, model name as a module constant `DEFAULT_BRIEF_MODEL`, a `_…Like` Protocol so tests pass a stub with no network or key). It issues one `chat.completions.create` in JSON-object mode and parses the result into `GeneratedBrief`, tolerating a model that ignores JSON mode by falling back to treating the whole response as the summary. This is the first chat/completions call in the codebase; embeddings (ADR 0004) were the only prior OpenAI use.

**Persistence — a record table (ADR 0019).** Generated briefs live in `bill_briefs`, a **record table**: derived from the data but not deterministically rebuildable from JSONL, and therefore surviving a mirror re-derivation. The row stores the `executive_summary`, the `lens` (verbatim — always stored, see below), a `facts_hash` (a SHA-256 over the fact pack + `model` + `prompt_version`), and `model` / `prompt_version` / `generated_at`. Keyed by `(bill_id, lens)`:

- **The neutral brief (`lens = ''`) is the cacheable default.** Generate once; reuse on every profile view. Its `facts_hash` lets the profile page flag it **stale** when the underlying mirror data has moved since generation, prompting a regenerate.
- **Conditioned briefs (`lens ≠ ''`) are keyed by their lens text** and cached too, but their cardinality is unbounded by design (see "What stays open").

The table is added via migration `_m002_add_bill_briefs` with the matching `_BASE_SCHEMA` entry, per the [ADR 0017](0017-sqlite-schema-versioning.md) contract (the schema-equivalence test pins them together).

**Storing vs. showing the lens.** The lens is *always stored* (provenance, regeneration, debugging, staleness). Whether it is *displayed* on the rendered brief is an independent presentation choice; v1 shows a small "Tailored for: …" marker, and that can be toggled off later without any data-model change.

**UX and gating.** The brief lives on the Bill profile page as an HTMX-swappable section with a lens textbox and a Generate/Regenerate button (`POST /bills/{c}/{t}/{n}/brief`). Generation is **synchronous** — one call takes a few seconds, and a sync route runs in FastAPI's threadpool, so no background-task machinery is needed (unlike enrichment's ADR 0016 flow). The feature is enabled whenever an OpenAI-backed `Briefer` is present, which in production is always: `concord serve` already constructs an OpenAI client at startup for query embeddings, so `OPENAI_API_KEY` is already a serve requirement. No separate opt-in flag is added.

## Consequences

**Trade-offs accepted:**

- **Generation costs money and is user-triggered, with no rate limit yet.** Unlike enrichment (ADR 0016), a brief spends the operator's *own* OpenAI budget and writes only a derived record — it does not drain the shared `CONGRESS_API_KEY` quota or write to the canonical store, so the risk profile is lower. Still, an adversarial visitor could click Regenerate in a loop. Per-IP rate limiting is the documented follow-up (the same `slowapi` limiter that guards `/search` is the natural home), and the neutral-brief cache already absorbs the common case to one call.
- **Blocking the event loop for the call's duration.** A sync route in the threadpool means the call doesn't block the loop, but it does hold a threadpool slot for a few seconds. Fine for the single-process demo; revisit if `concord serve` ever grows real concurrency.
- **Non-determinism in a SQLite table.** The whole reason for ADR 0019. A brief is not reproducible from JSONL; the `facts_hash` + `prompt_version` make staleness detectable but not the content reproducible.
- **Unbounded conditioned-brief cardinality.** Every distinct lens string is its own cached row. No eviction in v1.

**Things this buys:**

- **Fast comprehension with the facts one scroll away.** The reader gets a synthesized summary backed by a visible, immutable fact pack and the verbatim CRS summary.
- **Honest steerability.** The aide-briefing-a-principal use case is served (the exec summary tailors to audience) without giving up the honesty floor (facts fixed, counterpoint required, lens subordinate to the rules).
- **A reusable LLM seam.** `Briefer` mirrors `Embedder`, so the codebase now has a tested pattern for chat/completions the same way it had one for embeddings — future generated features inject the same client.

**What stays open:**

- **Rate limiting** on `POST /bills/{c}/{t}/{n}/brief`. Deferred; `slowapi` is the mechanism.
- **A longer "detail" section.** The brainstorm scoped v1 to the executive summary alone and explicitly deferred a longer analytical section; `GeneratedBrief` is shaped to grow it.
- **Conditioned-brief eviction / TTL.** When the `(bill_id, lens)` row count becomes a problem, an LRU or age-based sweep is the fix.
- **Richer fact pack.** Party split is best-effort (joins each cosponsor to their latest Term; un-indexed Members count as "Unknown"). Deeper coalition analytics or related-bill linkage are natural extensions.

## Rejected: generate on every page view, no persistence

Simplest to reason about, but it pays for an LLM call on every profile load (latency + cost) and throws away an artifact that is identical across views for the neutral case. The cache-as-record-table approach (ADR 0019) keeps the common case to one call and is the whole reason 0019 was written.

## Rejected: let the lens rewrite the whole brief, with honesty enforced only by prompt

Considered making the entire brief a single lens-conditioned narrative and trusting the system prompt to keep it honest. Rejected in favor of the structural split (immutable fact pack + verbatim CRS summary as ground truth, lens subordinate to explicit honesty rules). Prompt-only honesty is one jailbreak away from spin; a visible, deterministic fact block the prose sits beside is a far stronger guarantee.

## Rejected: an opt-in env flag mirroring `CONCORD_ENABLE_WEB_ENRICHMENT`

Enrichment is gated because it triggers outbound scraping against a shared, rate-limited `CONGRESS_API_KEY` and writes to the canonical store. A brief does neither — it spends the operator's own OpenAI budget (already required for serve) and writes a derived record. Adding a second flag would be friction without a matching risk. Cost control is addressed by the deferred rate limiter instead.
