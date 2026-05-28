# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read these before touching anything

- [CONTEXT.md](CONTEXT.md) is the **domain glossary**. The terms it defines (Issue, Article, Granule ID, Proceeding, Member, Term, Bill, Vote, Chunk, Party Unity Score, Stage 0/1/2/3, …) are load-bearing — use them verbatim in code, comments, and prose. Don't invent synonyms.
- [docs/adr/](docs/adr/) records the project's **non-obvious design decisions** (one decision per file). Skim the index before architectural work; read the relevant ADR(s) in full before changing anything they touch. The most foundational:
  - 0002 — JSONL is the canonical raw store; SQLite is derived/rebuildable.
  - 0003 / 0013 — SQLite + FTS5 + sqlite-vec is the derived store; the Mongo backend that 0003 left in place was removed by 0013.
  - 0005 / 0008 — Chunks are the unit of retrieval; one `chunks` table discriminated by `source_type` spans Proceedings, Bills, etc.
  - 0006 / 0009 — Mutable entities use snapshot-on-fetch (`{fetched_at, key, payload}`); multi-endpoint entities (Bills) split into one JSONL per sub-endpoint.
  - 0007 — Stage 0 + Stage 1 are parallel per entity type; Stage 2/3 are shared.
  - 0010 / 0011 — Votes phased by chamber (3a House via api.congress.gov, 3b Senate via senate.gov LIS XML); Party Unity Score is the CQ-style methodology, chamber-scoped.
  - 0012 — `concord serve` bootstraps an empty SQLite schema on startup.
  - 0014 — Published on PyPI as `congress-concord`; CLI shape is the stable contract, Python imports are best-effort.
- [CONTRIBUTING.md](CONTRIBUTING.md) — style rules the linter enforces (line 100, absolute imports only, type-hint everything in `src/`, `# noqa` requires an inline reason, etc.). Read it before silencing a lint warning.
- [docs/releases.md](docs/releases.md) — recurring release workflow for `congress-concord` on PyPI. Read it before cutting a release; the TestPyPI dry-run has a non-obvious dependency-resolution footgun that the doc documents.

If a proposed change conflicts with an ADR, the right move is to discuss whether to write a new ADR (or amend the existing one) — not to silently diverge. ADRs are appended, not edited away.

## Never write `from __future__ import annotations`

This project is Python 3.12+ and bans that import — see [CONTRIBUTING.md](CONTRIBUTING.md). A pre-commit hook and CI step block it; don't add it back.

## Branch naming

The harness spawns Claude sessions on randomly-named branches like `claude/funny-kirch-3bad54`. **Rename to something descriptive before doing real work** (`git branch -m claude/<short-task-name>`). This is a project convention, not a hard tooling requirement.

## Commands

Development uses [`uv`](https://docs.astral.sh/uv/) (Python 3.12+).

```sh
uv sync                         # install deps + project (idempotent)
uv run pre-commit install       # one-time per clone; wires up ruff hook

uv run ruff check               # lint
uv run ruff format --check      # format check (no rewriting)
uv run ruff format              # apply formatting
uv run mypy src                 # strict type-check src/concord/
uv run pytest                   # full test suite
uv run pytest tests/test_pipeline_bills.py::test_name -x   # single test
```

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs the four checks above + pytest. The pre-commit hook runs `ruff format` and `ruff check --fix` only — running mypy and pytest locally before pushing is on you.

Running the pipeline (CLI shape: `concord <stage> <entity>`):

```sh
export CONGRESS_API_KEY=...     # required for any scrape (api.data.gov key)
export OPENAI_API_KEY=...       # required for `index proceedings` and `serve`

uv run concord run proceedings --from 2026-05-22 --to 2026-05-22
uv run concord run members --congresses 119
uv run concord scrape bills --congresses 119
uv run concord serve            # FastAPI demo at 127.0.0.1:8000
```

Stage commands accept `--progress / --no-progress`; the data dir defaults to `./data/`. Every command is idempotent — natural-key dedup (`granule_id`, `bioguide_id`, `(chamber, congress, session, roll)` etc.) makes re-runs no-ops.

### API keys

- `CONGRESS_API_KEY` is an api.data.gov key (free signup at https://api.data.gov/signup/, 5,000 req/hr). For ad-hoc one-off queries you can use the literal value `DEMO_KEY` — api.data.gov accepts it without registration but rate-limits it hard (**30 req/hr, 50 req/day per IP**), so it's only useful for a quick smoke test, not anything resembling a real scrape. Do not use it in CI or fixtures.
- `OPENAI_API_KEY` has no equivalent demo path; you need a real key for `index proceedings` and `serve`.
- `CONCORD_ENABLE_WEB_ENRICHMENT=1` is an opt-in toggle that, *in addition* to `CONGRESS_API_KEY` being set, surfaces a "Request enrichment" button on the Bill profile page (ADR 0016). The button POSTs to a route that runs `scrape_enrichment` → `load_one` → `reindex_one` for that one Bill via `fastapi.BackgroundTasks`. Off by default; rate limiting is a follow-up.
- A `.env` file at the repo root is the convention for storing these locally — **not committed, not auto-loaded** (no `python-dotenv` dependency, and `.env` isn't in `.gitignore` yet, so be careful). The code reads `os.environ` directly. To use one, source it before running: `set -a; source .env; set +a; uv run concord …`. Don't add a dotenv autoload — it would change the env-var contract that `concord.api` and the CLI gate keys on.

## Architecture at a glance

```
src/concord/
  api.py, text.py, senate_xml.py     # HTTP clients (api.congress.gov, congress.gov text, senate.gov XML)
  models.py                          # Pydantic: Proceeding, Member, Term, Bill, Vote, MemberSnapshot…
  chunking.py, embedding.py          # Stage 2 building blocks
  scraper/<entity>.py                # Stage 0 — write JSONL (one module per entity, ADR 0007)
  pipeline/load_<entity>.py          # Stage 1 — JSONL → SQLite (per entity)
  pipeline/index_<entity>.py         # Stage 2 — chunks + FTS5 + vec (shared chunks table per ADR 0008)
  storage/
    base.py                          # Storage Protocol
    jsonl.py                         # raw-store backend (Proceedings)
    sqlite.py                        # derived store — all entities + indexes; owns ensure_schema()
  web/                               # FastAPI + Jinja2 + HTMX; reads the SQLite the pipeline writes
  cli/                               # Typer entry point (one module per entity)
```

Cross-cutting things worth knowing:

- **Stages are not classes; they're modules.** Each entity has its own `scrape`/`load`/`index` module pair. Don't introduce a base class to "DRY" them up — ADR 0007 explicitly rejects that. Shared utilities live in thin `_common.py` helpers.
- **The canonical store is JSONL on disk.** SQLite is rebuildable from it. If you're tempted to write data only to SQLite, you're probably breaking ADR 0002.
- **Mutable entities (Members, Bills, Votes) use the snapshot envelope** `{"fetched_at": ..., "key": {...}, "payload": ...}` (ADR 0006). Proceedings predate that envelope and are flat — don't try to unify them.
- **Multi-endpoint entities split their JSONL** (ADR 0009). A Bill's identity, cosponsors, actions, subjects, titles, summaries live in six separate `bill_*.jsonl` files. The Stage 1 loader joins them by `(congress, bill_type, bill_number)`.
- **Chunks are the unit of retrieval and they span source types** (ADRs 0005, 0008). One `chunks` table with `(source_type, source_id, ord)`; both FTS5 and `sqlite-vec` key on `chunk_id`. Hybrid search uses RRF.
- **Web layer owns its own schema bootstrap** (ADR 0012). `create_app()` calls `storage.sqlite.ensure_schema(db_path)`; don't add existence checks at the CLI layer that re-introduce the "DB missing → exit" footgun.
- **CLI defers heavy imports.** `concord/cli/proceedings.py` and `cli/serve.py` import `openai` / `uvicorn` inside functions so `concord --help` stays snappy. The `PLC0415` lint waiver in `pyproject.toml` is intentional — don't hoist those imports to the top.
- **The SQL-string lint waiver (`S608`) in `storage/sqlite.py` and `web/search.py` is also intentional** — those modules build SQL from module-level column tuples + `?` placeholders, never user input.

## Things that bite

- `pytest.mark.parametrize` takes a **tuple of names**, not a comma string: `("url", "expected")`, not `"url,expected"`. Ruff (`PT006`) will flag the wrong form.
- `mypy --strict` runs on `src/concord/` only; tests are looser via the override in `pyproject.toml`. Don't relax `src/` to match tests.
- Re-running the pipeline is always safe — dedup keys handle it. Don't add "delete first, then ingest" workflows; idempotency is the contract.
