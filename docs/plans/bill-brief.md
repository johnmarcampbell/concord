# Bill Brief — PRD

> A self-contained, glanceable view of a single Bill: a deterministic **fact pack** rendered verbatim, topped by one LLM-authored **executive summary** that a user can optionally condition for their own audience or angle — without ever bending the facts.

**Status:** PRD. v1 implemented and shipped behind the Bill Brief feature ([ADR 0020](../adr/0020-bill-brief-llm-summary.md)); see [Implementation status](#implementation-status). Derived from the bill-to-brief design conversation.

**Date:** 2026-05-30.

## Background

A Bill profile page already shows everything Concord scrapes — identity, sponsor, cosponsors, action history, subjects, titles, the CRS summaries, vote history. That is comprehensive but slow to absorb. The driving use case is a policymaker or staffer who needs to come up to speed on a bill *quickly*, and often *for a purpose*: to brief a principal, to prepare an argument, to frame the bill for a particular audience.

The bill's data is already well-curated — the CRS summaries are themselves human-authored summaries — so the model's job is synthesis, not extraction from raw legalese. That makes a cheap, single-call feature viable and reliable.

## Users and use cases

- **Staffer briefing a principal.** Generates a brief and tailors the executive summary to the principal's priorities ("frame for a budget-committee audience"). The tailoring of the *summary itself* is a feature, not a misuse — it's why the conditioning exists.
- **Analyst / journalist coming up to speed.** Wants a neutral, fair distillation plus the underlying facts in one place, without reading the whole legislative history.
- **Demo visitor.** Lands on a bill and gets an at-a-glance card without needing to read every section below.

## Product principles

1. **Facts are non-negotiable.** The deterministic fact pack and the verbatim CRS summary are computed/sourced by Concord and shown on the page. The LLM may narrate them but never contradict, invent, or omit them.
2. **Steerable, but honest.** The user's optional *lens* changes emphasis, framing, and audience — never a number, a date, a provision, or the bill's actual status. The honesty rules outrank the lens.
3. **The fact pack is the neutral anchor.** Because the executive summary is itself conditionable, the always-neutral baseline is the *fact pack* (an immutable block the prose sits beside), not a neutral paragraph. A tailored summary can't quietly disagree with a vote count printed right under it.
4. **Always surface the counterpoint.** The summary must include the strongest honest counter to any one-sided framing the lens requests.
5. **Cheap and bounded.** One outbound LLM call per brief. No multi-step agent.

## What a Bill Brief is

A self-contained card with two layers.

### Layer 1 — the deterministic fact pack (no LLM)

Computed in SQL/Python, rendered identically every time, sections dropping out when data is absent:

- Identity: bill identifier, title, Congress, origin chamber.
- Sponsor and policy area.
- Introduced date; **status** = latest action (date + text).
- **Coalition:** cosponsor counts (total / original / withdrawn) and a best-effort party split (D / R / I / Other / Unknown).
- Legislative subjects.
- Activity counts: actions on file, recorded votes tied to the bill.
- **The most recent CRS summary, shown verbatim and labeled with its stage** (the summary's `version_code` / `action_desc` + `action_date`). This is the authoritative "what the bill does," straight from the source.

The fact pack is shown **even before any summary is generated**, so the card is glanceable on first load.

### Layer 2 — the executive summary (one LLM call)

The model authors exactly one field: a ~3–6 sentence executive summary distilling what the bill does, where it stands, and the shape of its support. It is the only generated content and the only conditionable content. The structured output is deliberately extensible (a longer "detail" section is a planned future field).

### The lens

An optional free-text box on the generate form. Empty → a neutral summary. Non-empty → the summary is tailored to the requested emphasis/audience, subject to the honesty rules. The lens is **always stored** with the brief (provenance, regeneration, debugging, staleness); whether it is **displayed** is an independent presentation choice (v1 shows a small "Tailored for: …" marker).

## Requirements

### Functional

- **R1.** The Bill profile renders a Brief card containing the fact pack (Layer 1) whenever the feature is enabled, with or without a generated summary.
- **R2.** A "Generate brief" button produces the executive summary via one LLM call and shows it atop the fact pack. The button reads "Regenerate brief" once a brief exists.
- **R3.** An optional lens text box conditions the executive summary. The lens reaches the model as a delimited "emphasis request" subordinate to the honesty rules.
- **R4.** The latest CRS summary is shown verbatim with its stage label, and is also fed to the model as ground truth.
- **R5.** The neutral (no-lens) brief is cached and reused across views. When the underlying fact pack moves (or the model/prompt changes), the cached brief is flagged **stale** and the user is prompted to regenerate.
- **R6.** A failed generation never hides a still-good brief: on error, any existing cached brief is shown (flagged stale) alongside a generic error message.

### Honesty / safety

- **R7.** The system prompt forbids inventing provisions, numbers, dates, vote counts, sponsors, or outcomes not present in the provided material, and requires the strongest honest counterpoint to any requested framing.
- **R8.** End users never see internal error detail (keys, model names, upstream messages); failures surface a generic message in the UI and the real cause in the operator's logs.

### Non-functional

- **R9.** One outbound LLM call per generation. Generation is synchronous (a threadpool route), no background-task machinery.
- **R10.** The cache/staleness policy and the rendered-view shape live in exactly one place (one typed seam shared by the read and generate paths).
- **R11.** Generated briefs are a **record table** ([ADR 0019](../adr/0019-mirror-tables-vs-record-tables.md)) — derived but not deterministically rebuildable from JSONL, and therefore surviving a mirror re-derivation.

## Implementation status

v1 is implemented (ADR 0020, ADR 0019). Legend: ✅ done · ⏳ deferred.

| Requirement | Status | Notes |
|---|---|---|
| R1 — fact pack rendered, pre-generation too | ✅ | `BriefFacts` rendered in `bills/_brief.html`; assembled on every profile view when enabled. |
| R2 — generate / regenerate | ✅ | `POST /bills/{c}/{t}/{n}/brief`, synchronous. |
| R3 — lens conditioning | ✅ | Lens textarea → delimited emphasis in the prompt; "Tailored for: …" marker shown. |
| R4 — latest CRS summary verbatim + stage | ✅ | Shown in a collapsible block and fed to the model. |
| R5 — neutral cache + staleness | ✅ | `bill_briefs` keyed `(bill_id, lens)`; `facts_hash` over fact pack + model + prompt version drives the stale flag. |
| R6 — failed regenerate falls back to cached | ✅ | `get_or_generate_brief` returns the stale cached view + error. |
| R7 — honesty rules in prompt | ✅ | System prompt: ground-truth-only, counterpoint-required, neutral tone, rules outrank the lens. |
| R8 — generic UI error, real cause logged | ✅ | `BriefError` carries the underlying cause; web seam logs with `exc_info`. |
| R9 — single synchronous call | ✅ | `Briefer` mirrors `Embedder`; one `chat.completions` call in JSON mode. |
| R10 — one seam, typed view | ✅ | `BriefView` dataclass; `cached_view` / `get_or_generate_brief` in `concord/web/brief.py`. |
| R11 — record table | ✅ | `bill_briefs` added via migration `_m002`; survives a rebuild (loaders UPSERT, `ensure_schema` is `IF NOT EXISTS`). |
| Per-IP rate limiting on generate | ⏳ | Cost control follow-up; `slowapi` is the mechanism (as for `/search`). |
| Longer analytical "detail" section | ⏳ | `GeneratedBrief` is shaped to grow a second field. |
| Conditioned-brief eviction / TTL | ⏳ | `(bill_id, lens)` cardinality is unbounded by design; no sweep yet. |
| Richer fact pack (deeper coalition analytics, related bills) | ⏳ | Party split is best-effort (unindexed Members → "Unknown"). |
| Lens display as a toggle | ⏳ | Always stored; v1 always shows it. Hiding it is a pure presentation change. |

### Key code

- `concord/brief.py` — `BriefFacts`, `GeneratedBrief`, `BriefView`, `Briefer`, `build_facts`, `facts_hash`.
- `concord/web/brief.py` — `assemble_facts`, `cached_view`, `get_or_generate_brief`, `register_brief_routes`.
- `concord/web/search.py` — `get_bill_brief`, `cosponsor_party_breakdown` (the read layer).
- `concord/storage/sqlite.py` — `bill_briefs` table + `upsert_bill_brief` (record-table write).
- `concord/web/templates/bills/_brief.html` — the card.

## Non-goals (v1)

- An opt-in env flag à la `CONCORD_ENABLE_WEB_ENRICHMENT`. A brief spends the operator's own OpenAI budget and writes only a derived record — it doesn't drain the shared `CONGRESS_API_KEY` or touch the canonical store — so it's on whenever serve has an OpenAI client. Cost control is the deferred rate limiter instead.
- Multi-call / agentic generation.
- Replacing or collapsing the detailed Bill profile sections. The brief is the at-a-glance card; the sections below are the drill-down. (Whether the brief should later *replace* some of them is an open question.)

## Open questions / follow-ups

- **Brief vs. detail sections.** The card's fact pack overlaps the detailed sections below it. Keep both (at-a-glance + drill-down), or collapse the detail sections once a brief is present?
- **Rate limiting** before any public, un-gated deployment.
- **Ordinal rendering** of Congress numbers ("119th") is hardcoded `th` across the UI and the brief's prompt — see [issue #98](https://github.com/johnmarcampbell/concord/issues/98).
- **Model choice.** v1 defaults to `gpt-4o-mini` (one constant in `concord/brief.py`); revisit as quality/cost dictate.

## References

- [ADR 0020 — Bill Brief: an LLM-generated executive summary](../adr/0020-bill-brief-llm-summary.md)
- [ADR 0019 — Mirror tables vs. record tables](../adr/0019-mirror-tables-vs-record-tables.md)
- [ADR 0004 — OpenAI embeddings](../adr/0004-openai-embeddings.md) (the OpenAI dependency this feature first uses for chat)
- [CONTEXT.md](../../CONTEXT.md) — *Bill Brief*, *fact pack*, *lens*, *mirror table*, *record table*.
