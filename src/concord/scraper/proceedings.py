"""Stage 0 — Proceedings scraper.

Wires the api.congress.gov :class:`Client`, the article-text fetcher, and
a :class:`Storage` backend together for a single date-range pull. This is
the orchestration that used to live as ``_run_pull`` in ``concord.cli``;
the CLI now delegates to :func:`scrape` so the per-entity scraper
modules from ADR 0007 have a consistent shape.
"""

from collections.abc import Callable
from datetime import date

import httpx
import typer

from concord.api import USER_AGENT, ApiError, Client
from concord.pipeline.load_proceedings import ProgressEvent, PullResult, pull
from concord.storage.base import Storage
from concord.text import AdaptiveThrottle, fetch_text

#: Per-request timeout (seconds) for fetching article text from congress.gov.
#: The default httpx timeout (5s) is too aggressive for occasional slow
#: responses and triggers transport errors that abort multi-day pulls.
TEXT_FETCH_TIMEOUT = 60.0


def scrape(
    *,
    start: date,
    end: date,
    storage: Storage,
    storage_label: str,
    limit: int | None,
    progress: Callable[[ProgressEvent], None] | None = None,
) -> PullResult:
    """Pull every in-range article into ``storage`` and print a summary.

    Wraps :func:`concord.pipeline.load_proceedings.pull` with the live
    ``api.congress.gov`` :class:`Client` and a real ``httpx.Client`` for
    the article-text fetch. Errors that prevent any work (missing API key,
    failed client construction) exit with code 2 via :class:`typer.Exit`.

    ``progress`` is an optional callback invoked after each issue is
    processed, matching the convention used by all other entity scrapers.
    The CLI layer is responsible for constructing and committing the
    :class:`concord.cli._common.Progress` display object.
    """
    try:
        api_client = Client()
    except ApiError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    # Identify ourselves honestly: the default httpx UA ("python-httpx/…") is a
    # known trigger for the Cloudflare bot management in front of congress.gov,
    # which surfaces as the 403s that the AdaptiveThrottle then has to absorb.
    # Matching the UA the api/senate clients already send cuts those at source.
    http_client = httpx.Client(timeout=TEXT_FETCH_TIMEOUT, headers={"User-Agent": USER_AGENT})

    # One throttle for the whole pull: congress.gov rate-limits per client across
    # every URL, so backoff state must persist across fetches, not reset per URL.
    throttle = AdaptiveThrottle()

    try:
        with api_client:
            result = pull(
                start,
                end,
                client=api_client,
                fetch=lambda url: fetch_text(url, http_client, throttle=throttle),
                storage=storage,
                limit=limit,
                progress=progress,
            )
    finally:
        http_client.close()

    summary = (
        f"Wrote {result.written} new proceedings to {storage_label} "
        f"(skipped {result.skipped} already present"
    )
    if result.failed:
        summary += f", {result.failed} failed to fetch — will retry on next run"
    summary += ")"
    typer.echo(summary)
    return result


__all__ = ["TEXT_FETCH_TIMEOUT", "scrape"]
