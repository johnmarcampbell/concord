# Concord

A pipeline for collecting the daily [Congressional Record](https://www.congress.gov/congressional-record) via [api.congress.gov](https://api.congress.gov/). For any date range, produces one record per proceeding — full text plus metadata — as a local JSON Lines file.

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/johnmarcampbell/concord
cd concord
uv sync

export CONGRESS_API_KEY=...  # free key from https://api.data.gov/signup/
uv run concord pull --from 2026-05-22 --to 2026-05-22
```

Output:

```
Wrote 20 new proceedings to proceedings.jsonl (skipped 0 already present)
```

Re-running the same command is a no-op — already-pulled articles are detected by their granule ID and skipped without re-fetching their text. This is the resume contract: kill the process at any point and the next run picks up only what's missing.

## Getting an API key

`api.congress.gov` requires a free key from api.data.gov. Sign up at https://api.data.gov/signup/ — the key arrives by email immediately. Rate limit is 5,000 requests per hour, which is more than enough for any practical backfill (a full ~30-year pull is roughly 6,000 API calls).

Pass the key via the `CONGRESS_API_KEY` environment variable. There is no `--api-key` flag — keys belong in your environment, not your shell history.

## What gets produced

Concord writes one `Proceeding` per line to a `.jsonl` file. Each record carries the issue-level metadata, the article-level metadata, the granule ID (a stable identifier from govinfo), the plain-text body, and the fetch timestamp.

Fields:

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

A sample line, formatted for readability (on disk it's one line):

```json
{
  "issue_date": "2026-05-22",
  "congress": 119,
  "session": 2,
  "volume": 172,
  "issue_number": 88,
  "update_date": "2026-05-23T06:44:22Z",
  "section": "Daily Digest",
  "title": "Daily Digest/Next Meeting of the SENATE + Next Meeting of the HOUSE OF REPRESENTATIVES + Other End Matter; Congressional Record Vol. 172, No. 88",
  "start_page": "D551",
  "end_page": "D552",
  "text_url": "https://www.congress.gov/119/crec/2026/05/22/172/88/modified/CREC-2026-05-22-pt1-PgD551-6.htm",
  "pdf_url": "https://www.congress.gov/119/crec/2026/05/22/172/88/CREC-2026-05-22-pt1-PgD551-6.pdf",
  "granule_id": "CREC-2026-05-22-pt1-PgD551-6",
  "text": "[Daily Digest]\n[Pages D551-D552]\nFrom the Congressional Record Online through the Government Publishing Office [www.gpo.gov]\n\n…\n\nNext Meeting of the SENATE\n8 a.m., Tuesday, May 26\n…",
  "fetched_at": "2026-05-24T21:42:25.525669Z"
}
```

## CLI

```
concord pull --from YYYY-MM-DD --to YYYY-MM-DD [--storage PATH] [--limit N]
```

| Flag | Default | Description |
| ---- | ------- | ----------- |
| `--from` | _required_ | Inclusive start date. |
| `--to` | _required_ | Inclusive end date. |
| `--storage` | `./proceedings.jsonl` | Output JSONL path. Parent directories are created on first write. |
| `--limit` | _none_ | Cap on new writes. Useful for smoke tests (`--limit 1`). |

`concord pull --help` shows the same information.

## Backfill

The Daily Congressional Record is available via the API from **1995 onward**. (Older material lives under the Bound Congressional Record endpoint with a multi-year publication lag, and is deliberately out of scope for the current rebuild — see [docs/rebuild-plan.md](docs/rebuild-plan.md#out-of-scope).)

A full 1995-to-present backfill is roughly:
- **~5,800 issues** to enumerate (≈24 paginated list calls at the API's 250-per-page max)
- **~5,800 articles-list calls**, one per in-range issue
- **~290,000 text fetches** to congress.gov (these don't count against the API rate limit)

In practice that's several hours, network-bound. Recommended pattern:

```sh
tmux new -s concord
export CONGRESS_API_KEY=...
uv run concord pull --from 1995-01-01 --to 2026-12-31 --storage ./record.jsonl
# detach: Ctrl+b d
```

The JSONL file is safe to `tail` or `wc -l` while the pull is in progress. Killing the process (Ctrl+C, OOM, machine reboot) loses at worst the single in-flight record; the next invocation resumes via the dedup index built from the file on disk.

## Architecture

API client → text fetcher → storage. Each is a small module with a tight surface:

```
src/concord/
  api.py           # typed wrapper for api.congress.gov
  text.py          # fetch_text(url, client) — pulls plain text from <pre>-wrapped HTML
  models.py        # Pydantic: Issue, Article, Proceeding
  pipeline.py      # pull(start, end, *, client, fetch, storage, limit) -> PullResult
  storage/
    base.py        # Storage Protocol
    jsonl.py       # default backend; future Mongo backend will plug in here
  cli.py           # typer entry point
```

See [docs/rebuild-plan.md](docs/rebuild-plan.md) for the rebuild rationale and the full issue roadmap (the original Scrapy implementation was retired in May 2026).

## Development

```sh
uv sync
uv run ruff check
uv run ruff format --check
uv run mypy src
uv run pytest
```

CI runs all four on every PR.

## License

MIT.
