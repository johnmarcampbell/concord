# Concord

A pipeline for collecting the daily [Congressional Record](https://www.congress.gov/congressional-record) via [api.congress.gov](https://api.congress.gov/).

## Status

Under active rebuild. The original 2017-era Scrapy implementation has been removed in favor of an API-first pipeline backed by `httpx` and Pydantic. See [docs/rebuild-plan.md](docs/rebuild-plan.md) for the full architecture, issue breakdown, and rationale.

Full usage documentation will land with the MVP. Until then, this README is intentionally minimal.

## Development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run ruff check
uv run mypy src
uv run pytest
```
