---
status: accepted
---

# Single network-only fetch seam

Concord had three upstream HTTP clients with near-identical resilience loops:
`concord.api` for `api.congress.gov`, `concord.text` for congress.gov article
HTML, and `concord.senate_xml` for senate.gov LIS XML. Each owned its own
retry loop, exponential backoff, indefinite waiting on rate-limit signals, and
Scrape Run recording against the ADR 0021 recorder contract.

That duplication had already started to drift:

- `api.py`, `text.py`, and `senate_xml.py` each reimplemented the same
  `Attempt` recording helpers.
- Retry limits and backoff behavior were no longer aligned.
- `text.py` imported HTTP status constants from `concord.api`, which was a
  symptom that the shared behavior lived in the wrong place.

The key design clarification was that the real variation between the three
clients is narrow. They all need the same network-resilience spine, while the
client-specific differences reduce to rate-limit or sentinel policy and the
body parsing layered above the fetch.

## What we decided

- Add one deep helper module, `concord.fetch`, that owns the network-only fetch
  loop: transport retries, transient 5xx retries, exponential backoff, and the
  ADR 0021 Scrape Run success and Run Event recording contract.
- The module returns raw bytes on a successful 2xx response and never inspects
  the response body for JSON, XML, HTML structure, or domain shape.
- Client-specific rate-limit behavior is expressed as a small policy seam:
  `RateLimitPolicy` with `before_request`, `classify`, and `on_success` hooks.
  The first concrete adapter is `RetryAfterPolicy` for `api.congress.gov`.
- Client adapters stay shallow and explicit. `concord.api`, `concord.text`, and
  `concord.senate_xml` keep their own URL building, parsing, and public error
  types. This is a thin shared helper, not a base class hierarchy, consistent
  with ADR 0007.
- Content validation remains outside the fetch seam. Wire-shape and domain
  validation continue to live at Stage 1 in `from_<source>` factories per
  ADR 0018.

## Failure taxonomy

The extraction only works cleanly if these three failure classes stay distinct:

- Network failure: the upstream did not successfully return the bytes we asked
  for. This includes transport errors, exhausted transient retries, terminal
  non-success HTTP responses, and policy-rejected responses. Owned by
  `concord.fetch` and recorded through ADR 0021.
- Structural failure: the fetch succeeded, but the body is unusable in a way
  that only the caller can detect, such as congress.gov HTML without the
  expected `<pre>` block. This stays with the caller.
- Load Validation Failure: Stage 1 rejects upstream payload shape through a
  `from_<source>` factory. This remains a loader concern per ADR 0023 and
  ADR 0018.

## Consequences

- `concord.api` now delegates its retry and Run Event behavior to
  `concord.fetch`, then performs only JSON parsing and pagination-envelope
  handling above it.
- Shared HTTP status constants and retry helpers live in `concord.fetch`
  instead of being anchored to one specific client.
- The resilience contract now has one test surface, `tests/test_fetch.py`,
  driven through `httpx.MockTransport`.
- Two behavior clarifications are explicit in the extracted design:
  successful 2xx responses count as network successes even if later JSON
  envelope parsing fails, and repeated 429s without `Retry-After` use the
  policy's own exponential schedule rather than borrowing the transient 5xx
  counter.
- Later migrations of `concord.text` and `concord.senate_xml` should add their
  own policy adapters or sentinel handling on top of the same seam, rather than
  reintroducing duplicated retry loops.

## Rejected

- A shared base client class. Rejected because ADR 0007 keeps stages and
  entity clients explicit modules rather than inheritance trees.
- Content-aware fetching. Rejected because JSON or XML body validation belongs
  to the caller or Stage 1, not the network chokepoint.
- Keeping three separate retry loops. Rejected because the differences were too
  small to justify continued drift, duplicated tests, and cross-client constant
  leakage.