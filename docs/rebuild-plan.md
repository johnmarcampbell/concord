# Concord rebuild plan

## Context

Concord was a Scrapy-based scraper for the daily [Congressional Record](https://www.congress.gov/congressional-record), last touched in 2019. The codebase targeted the HTML structure of congress.gov at that time. In the intervening years:

- congress.gov has redesigned its site, almost certainly breaking the existing selectors
- The Library of Congress now publishes a free, documented JSON API at [api.congress.gov](https://api.congress.gov/) with full Congressional Record coverage from 1995–present
- Scrapy is still maintained but is overkill for the actual workload — there is no spider-style crawl to do once you have an API enumerating issues for you

The rebuild replaces the scraper with an API-first pipeline. The old code (`concord/`, `congress_gov/`, `runSpider.py`, `scrapy.cfg`, the pinned 2019 `requirements.txt`) is deleted in the first issue of the plan. The repo and its history are kept; only the implementation changes.

## Goal

A pipeline that, given a date range, produces a complete copy of the Congressional Record for that range — one record per article ("proceeding"), with full text and metadata, written to local storage (default) or MongoDB (optional).

## Decisions

### Data source: api.congress.gov + per-article HTML fetch

The API gives all structured metadata (volume, issue, congress, session, article title, page span, section) and a stable URL for each article's text. The text itself lives at:

```
https://www.congress.gov/{congress}/crec/{yyyy}/{mm}/{dd}/{volume}/{issue}/modified/{granule_id}.htm
```

The HTML at that URL is a `<pre>`-wrapped plain-text document — extraction is one stdlib HTML parser, no fragile selectors. A throwaway end-to-end spike validated this two-stage flow against 2026-05-22 (20 articles, ~3KB plain text per article, two API calls + N text fetches per issue).

API limits: 5,000 requests/hour per key, free instant signup at api.data.gov. A full ~30-year backfill costs roughly 15,000 API calls (≈3 hours under the limit) — not a real constraint.

### No Scrapy

The pipeline is linear: enumerate issues for a date range → list articles per issue → fetch each article's text → write. There is no recursive crawl, no need for spider middleware, no JavaScript rendering. Plain `httpx` with optional concurrency suffices. Dropping Scrapy removes the `Twisted` dependency tree and a substantial amount of incidental complexity.

### Pydantic at every boundary

API responses are parsed into Pydantic v2 models. Storage writes serialize from those same models. The intermediate code never handles untyped dicts. This gives free coercion (date strings, integer-typed strings), validation at the network boundary, and `mypy`-checkable types throughout.

### Storage as a Protocol

A `Storage` Protocol with two methods (`has(granule_id)`, `write(proceeding)`) lets the default JSONL backend ship with zero infra requirements while keeping MongoDB available as an optional extra (`pip install concord[mongo]`). Idempotency is built in: `has` is checked before every write, so re-runs and resumed backfills produce no duplicates.

### Python 3.12+, uv, ruff, mypy, pytest

Modern toolchain across the board. CI runs lint + type-check + tests on every PR. Tests are written alongside the code they cover, not deferred to a hardening phase.

## Target architecture

```
concord/
  pyproject.toml          # uv-managed, py3.12+, ruff + mypy + pytest
  src/concord/
    cli.py                # typer: `concord pull --from YYYY-MM-DD --to YYYY-MM-DD`
    api.py                # httpx client for api.congress.gov (retry, rate-limit)
    text.py               # article HTML → plain text
    models.py             # pydantic: Issue, Article, Proceeding
    pipeline.py           # date range → issues → articles → text → storage
    storage/
      base.py             # Storage protocol
      jsonl.py            # default backend
      mongo.py            # optional, behind [mongo] extra
  tests/
    fixtures/             # recorded API JSON and article HTML
  docs/
    rebuild-plan.md       # this document
  .github/workflows/      # ci.yml: ruff + mypy + pytest
```

Data flow for `concord pull --from D1 --to D2`:

1. **Enumerate**: `api.list_issues()` paginates `/v3/daily-congressional-record` newest-first, stops once dates fall before `D1`, keeps issues in `[D1, D2]`. (The endpoint has no date filter — pagination is the only option.)
2. **Expand**: for each in-range issue, `api.list_articles(volume, issue_number)` returns the flat list of articles with metadata + text URL.
3. **Dedup**: for each article, `storage.has(granule_id)` short-circuits already-fetched work.
4. **Fetch + write**: `text.fetch_text(url)` returns plain text; combine with the article metadata into a `Proceeding`; `storage.write(proceeding)`.

A `Proceeding` carries: `issue_date`, `congress`, `session`, `volume`, `issue_number`, `section`, `title`, `start_page`, `end_page`, `text_url`, `pdf_url`, `granule_id`, `text`, `fetched_at`.

## Issue plan

11 GitHub issues on [johnmarcampbell/concord](https://github.com/johnmarcampbell/concord/issues). Numbers below are the real GitHub numbers (the plan was originally drafted as #1–#11; PRs and prior issues consumed 1–15 so the rebuild starts at #16).

### MVP rebuild (milestone)

| #   | Title                          | Blocked by      |
| --- | ------------------------------ | --------------- |
| [16](https://github.com/johnmarcampbell/concord/issues/16) | Wipe legacy code, init new project structure | —               |
| [17](https://github.com/johnmarcampbell/concord/issues/17) | Pydantic models: Issue, Article, Proceeding  | 16              |
| [18](https://github.com/johnmarcampbell/concord/issues/18) | API client for api.congress.gov              | 17              |
| [19](https://github.com/johnmarcampbell/concord/issues/19) | Article text fetcher                         | 17              |
| [20](https://github.com/johnmarcampbell/concord/issues/20) | JSONL storage backend                        | 17              |
| [21](https://github.com/johnmarcampbell/concord/issues/21) | Pipeline orchestrator                        | 18, 19, 20      |
| [22](https://github.com/johnmarcampbell/concord/issues/22) | CLI: `concord pull`                          | 21              |

After #16 lands, #17 unblocks the three parallel tracks (#18, #19, #20). All three feed into #21, which unblocks #22 to ship the MVP.

### Post-MVP

| #   | Title                                        | Blocked by |
| --- | -------------------------------------------- | ---------- |
| [23](https://github.com/johnmarcampbell/concord/issues/23) | Retry and rate-limit handling in API client  | 18         |
| [24](https://github.com/johnmarcampbell/concord/issues/24) | Resume and backfill mode                     | 20, 21     |
| [25](https://github.com/johnmarcampbell/concord/issues/25) | Mongo storage backend (optional)             | 20         |
| [26](https://github.com/johnmarcampbell/concord/issues/26) | README and usage documentation               | 22         |

### Dependency graph

```
#16 ──→ #17 ──┬──→ #18 ──┬──→ #21 ──→ #22 ──→ #26
              │           │
              ├──→ #19 ───┤
              │           │
              └──→ #20 ───┘

         #18 ──→ #23
         #20, #21 ──→ #24
         #20 ──→ #25
```

Each issue body lists its blockers as GitHub task-list checkboxes that auto-strike-through when blockers close. Each issue has tests in its acceptance criteria — testing is not a separate phase.

## Out of scope

- **Bound Congressional Record (pre-1995)**: a different API endpoint with a different schema. Considered but deliberately deferred — the daily edition covers 1995–present, which is the realistic backfill horizon. Can be added as a future issue if needed.
- **Backwards compatibility with the old scrapy item schema**: the project has been defunct for years; no downstream consumers to support. The new `Proceeding` model is designed fresh.
- **Web UI / search / analysis**: this project's scope is collection, not consumption.

## Status

Plan is captured here and in the linked issues. Implementation begins at #16.
