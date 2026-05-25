# 0001 — Python end-to-end for the web layer

**Status**: Accepted, 2026-05-24.

## Context

Concord is gaining a public-facing demo: a search UI fronted by a small HTTP layer that reads the SQLite derived store. The demo URL is going on a resume, so it has to look intentional, but it's still a personal project on a small VPS, not a product.

Three realistic architectures were on the table:

1. **Python end-to-end (lens)** — FastAPI serving server-rendered HTML (HTMX or plain Jinja). Same process, same repo, same language as the scraper and pipeline.
2. **Polyglot (Python + Node)** — Python for the data pipeline, a Node/Fastify API + React/Vite frontend reading the same SQLite file. Established pattern the author uses elsewhere.
3. **Python pipeline + statically-deployed SPA** — backend on the VPS, frontend on a Pages-style host calling a JSON API.

## Decision

Go with (1). The web layer is a server-rendered Python app in the same repo as the rest of Concord, reading the same SQLite file the pipeline writes.

## Consequences

**Trade-offs accepted:**

- The frontend has a lower polish ceiling than a modern React app would. We're deliberately treating the UI as a *lens onto the data*, not as a peer showpiece — the technical interest in this project lives in the pipeline, search, and (later) the knowledge graph.
- The "polyglot resume signal" is forfeited. The trade is for less ceremony and a tighter feedback loop. Picking HTMX-style server rendering when the project doesn't need a SPA is itself a signal — that the engineer chooses tools deliberately.

**Things this buys:**

- One language, one dependency manager, one deploy target, one test suite. On a small VPS with one developer, every reduction in coordination surface compounds.
- The pipeline and the web layer can share Python code directly — no API contract to design, version, and keep in sync with the schema.
- The demo can ship sooner. Stages 1 and 2 are the hard part; the web layer becomes thin.

**What we keep open:**

- The SQLite file is the only durable artifact the web layer touches. If a polished React/Node frontend becomes valuable later, it can be added in a separate repo or a `web/` subdirectory without changing the pipeline — the SQLite schema is the contract. This decision is reversible at the cost of one new component, not a rewrite.

## Rejected: polyglot stack (option 2)

The author has an established TypeScript/Fastify/React stack used in another project, and it would have produced a more polished UI for the same time investment in the frontend half. Rejected because the project's current scope (stages 1 + 2 + a thin demo) doesn't justify a second language, a second deployment target, and a second test suite — and the "more polished UI" benefit is small compared to the cost in coordination surface for one developer.

If the project grows into something that actually wants a rich client-side experience (visualizations of the knowledge graph, complex query builder, multi-pane document explorer), revisit this decision.

## Rejected: split-host SPA (option 3)

No reason to introduce a second deployment target before the project has any users.
