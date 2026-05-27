# Concord

A pipeline for collecting U.S. Congress data — currently the daily [Congressional Record](https://www.congress.gov/congressional-record) (proceedings) and the directory of [Members](https://www.congress.gov/members) — via [api.congress.gov](https://api.congress.gov/). Stores everything locally as JSON Lines + SQLite, with a FastAPI search demo on top.

Distributed on PyPI as **`congress-concord`**; imported in Python as **`concord`** (the bare `concord` name on PyPI was already taken).

## Install

```sh
pip install congress-concord
```

Requires Python 3.12+. The install is batteries-included — every `concord` subcommand (`scrape`, `load`, `index`, `run`, `serve`) works out of the box.

## Quick start

```sh
export CONGRESS_API_KEY=...  # free key from https://api.data.gov/signup/
export OPENAI_API_KEY=...    # required for proceedings indexing (semantic search)

# Proceedings — one day's articles, end-to-end:
concord run proceedings --from 2026-05-22 --to 2026-05-22

# Members — current Congress, end-to-end:
concord run members --congresses 119

# Serve the web demo:
concord serve
```

To work on Concord itself (rather than just use it), clone the repo and `uv sync` instead:

```sh
git clone https://github.com/johnmarcampbell/concord
cd concord
uv sync
uv run concord run proceedings --from 2026-05-22 --to 2026-05-22
```

Output for `run proceedings`:

```
→ Stage 0: scrape
Wrote 20 new proceedings to data/proceedings.jsonl (skipped 0 already present)
→ Stage 1: load
Loaded 20 new proceedings into data/proceedings.db (skipped 0 already present)
→ Stage 2: index
Indexed: chunked 20 new proceedings (98 new chunks, …); embedded 98 new chunks
✓ Done.
```

Re-running any command is a no-op — already-stored records are detected by their natural key (`granule_id` for proceedings, `bioguide_id` for members) and skipped. Kill the process at any point and the next run resumes.

## Getting an API key

`api.congress.gov` requires a free key from api.data.gov. Sign up at https://api.data.gov/signup/ — the key arrives by email immediately. Rate limit is 5,000 requests per hour.

`OPENAI_API_KEY` is only needed for `concord index proceedings` (chunk embeddings) and `concord serve` (query embedding). Member name search uses FTS5 only, no embeddings.

Pass keys via environment variables; there are no `--api-key` flags.

## CLI

Concord follows a `<stage> <entity>` shape — every entity type goes through Stage 0 (scrape), Stage 1 (load), Stage 2 (index), and there's a `run` that chains all three.

| Command | What it does |
| ------- | ------------ |
| `concord scrape proceedings --from YYYY-MM-DD --to YYYY-MM-DD` | Stage 0 — fetch articles, write JSONL. |
| `concord load proceedings`   | Stage 1 — mirror the JSONL into the `proceedings` SQLite table. |
| `concord index proceedings`  | Stage 2 — chunk + embed every proceeding into FTS5 and `sqlite-vec`. |
| `concord run proceedings --from … --to …` | All three back-to-back. |
| `concord scrape members --congresses 117,118,119` | Stage 0 — snapshot members of those Congresses. |
| `concord load members`   | Stage 1 — project the latest snapshot per Bioguide ID into `members` + `member_terms`. |
| `concord index members`  | Stage 2 — populate the `members_fts` FTS5 table. |
| `concord run members --congresses …` | All three back-to-back. |
| `concord serve` | Run the FastAPI search demo via uvicorn. |

Every command supports `--help` for its full flag list. Stage commands accept `--progress / --no-progress` (progress is on by default and overwrites itself in place on a TTY) and store files default to `./data/`.

## Entities

### Proceedings

One `Proceeding` record per article in the daily Congressional Record. Written one-per-line to `data/proceedings.jsonl`.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `issue_date` | date (`YYYY-MM-DD`) | The day the issue was published. |
| `congress` | int | Which Congress (e.g. 119). |
| `session` | int (1 or 2) | First or second session of that Congress. |
| `volume` | int | Daily Record volume number. |
| `issue_number` | int | Issue number within the volume. |
| `update_date` | datetime | When `api.congress.gov` last revised this issue. |
| `section` | string | `Senate Section`, `House Section`, `Extensions of Remarks Section`, or `Daily Digest`. |
| `title` | string | Article title from the API. |
| `start_page`, `end_page` | string | Page range, e.g. `D551`–`D552`. |
| `text_url` | URL | Formatted-text source URL on congress.gov. |
| `pdf_url` | URL | PDF source URL. |
| `granule_id` | string | Stable identifier, e.g. `CREC-2026-05-22-pt1-PgD551-6`. Used for dedup. |
| `text` | string | The full plain text of the proceeding. |
| `fetched_at` | datetime | When Concord retrieved this article. |

### Members

A `Member` is a person who has served in Congress, identified by Bioguide ID. Each fetch appends a snapshot envelope to `data/members.jsonl` (per [ADR 0006](docs/adr/0006-snapshot-on-fetch-for-mutable-entities.md)):

```json
{
  "fetched_at": "2026-05-25T14:02:11+00:00",
  "key": {"bioguide_id": "S000033"},
  "payload": { "...raw /v3/member payload..." }
}
```

The Stage 1 loader keeps the latest snapshot per Bioguide ID and projects it into two SQLite tables:

- **`members`** — identity fields that don't change across a career (name, birth year, photo URL).
- **`member_terms`** — one row per `(bioguide_id, congress, chamber)`. Carries `party`, `state`, `district` (House only), and `start_date`/`end_date`. A Member who switched parties or chambers between Congresses has multiple Term rows with the historical values intact.

Member name search uses an FTS5 index (`members_fts`) over the direct and inverted name forms. No embeddings — BM25 + porter stemming is the right tool for short proper nouns.

See [CONTEXT.md](CONTEXT.md) for the full vocabulary and [docs/plans/phase-1-members.md](docs/plans/phase-1-members.md) for the design rationale.

## Web demo (`concord serve`)

Single-process FastAPI + Jinja2 + HTMX, reading the same SQLite file the pipeline writes. Routes:

- `GET /` — landing page with a search box.
- `GET /search?q=…` — federated search. Renders Members and Proceedings in two grouped sections; checkboxes above the results suppress either independently.
- `GET /members` — browse all currently-serving Members with chamber/party filters.
- `GET /members/{bioguide_id}` — Member profile: photo, current role, biography, term history.
- `GET /proceedings/{granule_id}` — full text of one proceeding.

By default `concord serve` binds to `127.0.0.1:8000` for use behind a reverse proxy.

## Backfill

The Daily Congressional Record is available via the API from **1995 onward**. (Older material lives under the Bound Congressional Record endpoint with a multi-year publication lag, and is deliberately out of scope for the current rebuild — see [docs/rebuild-plan.md](docs/rebuild-plan.md#out-of-scope).)

A full 1995-to-present proceedings backfill is roughly:
- **~5,800 issues** to enumerate (≈24 paginated list calls at the API's 250-per-page max)
- **~5,800 articles-list calls**, one per in-range issue
- **~290,000 text fetches** to congress.gov (these don't count against the API rate limit)

In practice that's several hours, network-bound. Recommended pattern:

```sh
tmux new -s concord
export CONGRESS_API_KEY=...
uv run concord scrape proceedings --from 1995-01-01 --to 2026-12-31
# detach: Ctrl+b d
```

The JSONL file is safe to `tail` or `wc -l` while the pull is in progress. Killing the process (Ctrl+C, OOM, machine reboot) loses at worst the single in-flight record; the next invocation resumes via the dedup index built from the file on disk.

Members are much smaller — the last three Congresses fit in a single `concord run members --congresses 117,118,119` in under a minute.

## Architecture

Stage 0 (scrape) and Stage 1 (load) are parallel per entity type; Stage 2 (index) and the web layer are shared. See [ADR 0007](docs/adr/0007-parallel-pipelines-per-entity.md) for the rationale.

```
src/concord/
  api.py               # typed wrapper for api.congress.gov
  text.py              # fetch_text(url, client) — plain text from <pre>-wrapped HTML
  models.py            # Pydantic: Issue, Article, Proceeding, Member, Term, MemberSnapshot
  chunking.py          # chunk(text) -> Chunk[] for Stage 2 indexing
  embedding.py         # OpenAI Embedder wrapper
  scraper/
    proceedings.py     # Stage 0 — congressional record articles
    members.py         # Stage 0 — /member/congress/{n}
  pipeline/
    load_proceedings.py    # Stage 1 — JSONL -> proceedings table
    load_members.py        # Stage 1 — snapshot JSONL -> members + member_terms
    index_proceedings.py   # Stage 2 — chunks + FTS5 + vector embeddings
    index_members.py       # Stage 2 — members_fts
  storage/
    base.py            # Storage Protocol
    jsonl.py           # raw-store backend for Proceedings
    sqlite.py          # derived store — all entities, all indexes
  web/
    app.py, search.py  # FastAPI routes + federated query layer
    templates/         # Jinja2 + HTMX
  cli.py               # typer entry point
```

See [docs/rebuild-plan.md](docs/rebuild-plan.md) for the rebuild rationale, [docs/plans/](docs/plans/) for per-phase plans, and [docs/adr/](docs/adr/) for the design decisions.

## Development

```sh
uv sync
uv run pre-commit install   # one-time, wires up the local commit hook
uv run ruff check
uv run ruff format --check
uv run mypy src
uv run pytest
```

The `pre-commit` hook runs `ruff format` and `ruff check --fix` on every commit; CI runs all four checks above plus `pytest`.

## Versioning and API stability

Concord is below 1.0 and is *not* committing to a stable Python API yet. What semver does track for `congress-concord` releases:

- `concord <subcommand>` shape, flag names, exit codes, and the format of the success-summary lines printed to stdout
- The on-disk JSONL and SQLite formats that the CLI produces (other tools may read these)

What semver does *not* track (yet):

- Python imports. `from concord.storage.sqlite import ...` and similar internal imports may move between minor versions as the codebase refactors. Build CLI workflows on top of `concord`, not Python integrations, until 1.0.

See [ADR 0014](docs/adr/0014-publish-to-pypi-cli-first.md) for the reasoning. Maintainers: [docs/releases.md](docs/releases.md) is the recipe for cutting a release.

## License

MIT — see [LICENSE](LICENSE).
