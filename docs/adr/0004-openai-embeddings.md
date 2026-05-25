# 0004 — OpenAI for embeddings

**Status**: Accepted, 2026-05-24.

## Context

Stage 2 of the pipeline turns chunks of proceeding text into vector embeddings stored in `sqlite-vec`. The embedding model has to come from somewhere. Concord's deploy target is a 4 GB Hostinger KVM1 with 1 vCPU.

Two realistic paths:

1. **Local model on the VPS.** A small sentence-transformers model (e.g. `BAAI/bge-small-en-v1.5`, 384-dim, ~130 MB on disk, ~500 MB RAM during inference). CPU-only. Self-contained, no external dependency, no cost.
2. **OpenAI API.** `text-embedding-3-small` (1536-dim). ~$0.02 per million tokens. Zero VPS RAM. Requires an API key and outbound HTTPS.

## Decision

Use the OpenAI API (`text-embedding-3-small`) for all embedding work in stage 2.

## Consequences

**Trade-offs accepted:**

- **Dependency on a paid third-party service.** If OpenAI changes pricing, deprecates the model, or has an outage, stage 2 is affected. Mitigated by the fact that stage 2 only runs on cron-driven batch jobs (no live serving dependency), and by the abstraction — swapping providers or switching to a local model is one component, not a rewrite.
- **Network egress on every embed.** Stage 2 is now network-bound rather than CPU-bound, but it's also faster end-to-end (~30–60 min for a 5-year backfill vs ~3–8 hours on the local CPU).
- **Recurring cost.** ~$3 one-time for the 5-year backfill, then a few cents per year for the daily increment. Trivial.

**Things this buys:**

- **Stage 2 stops contending for VPS RAM.** No model loaded, no 500 MB inference spike. Critical on a 4 GB machine where the web server and pipeline coexist.
- **Higher embedding quality.** `text-embedding-3-small` reliably outperforms small open-source models on retrieval benchmarks. We get this without paying the RAM tax a comparable local model would impose.
- **Operational simplicity.** No model files to download, no tokenizer to manage, no version drift between dev and prod. `pip install openai`, set an env var.
- **Faster development feedback.** Re-embedding a 5-year corpus during iteration takes an hour, not most of a day.

**What stays open:**

- The embedding abstraction (an `embed(texts: list[str]) -> list[Vector]` function) is small. If a future requirement demands data sovereignty, offline operation, or a model fine-tuned to Congressional Record language, swapping in a local model is contained: change the function body, re-run stage 2, eat the one-time re-embed cost ($3) when the model dimension changes (which forces dropping the `sqlite-vec` table and rebuilding).

## Rejected: local model

The only reasons to pick local are (a) cost (irrelevant at our volume), (b) data sovereignty (irrelevant — Congressional Record is public), or (c) "demonstrate I can run open models on modest hardware" (a fine resume goal, but not this project's resume goal). The operational cost of managing model artifacts, tokenizers, and inference loops on a tight VPS isn't justified when the API alternative is $3/year.
