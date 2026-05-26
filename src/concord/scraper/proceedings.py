"""Stage 0 — Proceedings scraper.

Wires the api.congress.gov :class:`Client`, the article-text fetcher, and
a :class:`Storage` backend together for a single date-range pull. This is
the orchestration that used to live as ``_run_pull`` in ``concord.cli``;
the CLI now delegates to :func:`scrape` so the per-entity scraper
modules from ADR 0007 have a consistent shape.
"""

from __future__ import annotations

from datetime import date

import httpx
import typer

from ..api import ApiError, Client
from ..pipeline.load_proceedings import ProgressEvent, PullResult, pull
from ..storage.base import Storage
from ..text import fetch_text

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
    show_progress: bool,
) -> PullResult:
    """Pull every in-range article into ``storage`` and print a summary.

    Wraps :func:`concord.pipeline.load_proceedings.pull` with the live
    ``api.congress.gov`` :class:`Client` and a real ``httpx.Client`` for
    the article-text fetch. Errors that prevent any work (missing API key,
    failed client construction) exit with code 2 via :class:`typer.Exit`.
    """
    # Local import to avoid a circular dep on the CLI's Progress helper.
    from ..cli import Progress

    try:
        api_client = Client()
    except ApiError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    http_client = httpx.Client(timeout=TEXT_FETCH_TIMEOUT)
    progress = Progress(enabled=show_progress)

    def _on_progress(event: ProgressEvent) -> None:
        progress.update(
            f"  {event.issue.issue_date}  "
            f"vol {event.issue.volume} iss {event.issue.issue_number:>4}  "
            f"+{event.issue_written:>4} written, "
            f"{event.issue_skipped:>4} skipped, "
            f"{event.issue_failed:>3} failed  "
            f"(total: {event.total_written} / "
            f"{event.total_skipped} / "
            f"{event.total_failed})"
        )

    try:
        with api_client:
            result = pull(
                start,
                end,
                client=api_client,
                fetch=lambda url: fetch_text(url, http_client),
                storage=storage,
                limit=limit,
                progress=_on_progress if show_progress else None,
            )
    finally:
        http_client.close()
        progress.commit()

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
