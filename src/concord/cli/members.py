"""CLI commands for the Members entity (scrape / load / index / run)."""

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from concord.api import ENV_API_KEY, ApiError, Client
from concord.pipeline.index_members import index as index_members
from concord.pipeline.load_members import load as load_members
from concord.scraper import members as members_scraper

from ._apps import index_app, load_app, run_app, scrape_app
from ._common import DEFAULT_DB, Progress, RateTracker, _parse_congresses

DEFAULT_MEMBERS_JSONL = Path("./data/members.jsonl")

#: Congresses scraped by ``concord scrape members`` when ``--congresses``
#: is not passed. Matches the Phase 1 roadmap scope.
DEFAULT_MEMBER_CONGRESSES = (117, 118, 119)


# ---------------------------------------------------------------------------
# Stage workers
# ---------------------------------------------------------------------------


def _run_scrape_members(
    *,
    congresses: list[int],
    storage_path: Path,
    show_progress: bool,
) -> int:
    try:
        api_client = Client()
    except ApiError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    progress = Progress(enabled=show_progress)
    tracker = RateTracker()

    def _on_progress(event: members_scraper.ScrapeProgressEvent) -> None:
        if not progress.interactive and not event.is_congress_done:
            return
        tracker.update_total(event.category_total)
        label = f"  congress {event.congress:>3}  "
        if event.is_congress_done:
            progress.update(label + tracker.finish(event.written_in_congress))
            progress.commit()
            tracker.reset()
        else:
            progress.update(label + tracker.tick(event.written_in_congress))

    try:
        with api_client:
            written = members_scraper.scrape(
                client=api_client,
                congresses=congresses,
                storage_path=storage_path,
                fetched_at=datetime.now(UTC),
                progress=_on_progress if show_progress else None,
            )
    finally:
        progress.commit()

    typer.echo(
        f"Wrote {written} member snapshot(s) to {storage_path} "
        f"across {len(congresses)} congress(es)."
    )
    return written


def _run_load_members(
    *,
    storage_path: Path,
    db_path: Path,
) -> tuple[int, int]:
    """Return ``(members_written, terms_written)``."""
    if not storage_path.exists():
        # No input file is a no-op, not an error: load is idempotent and
        # callable in any order. The user probably hasn't run scrape yet —
        # tell them what they need.
        typer.echo(
            f"No input file at {storage_path} — "
            f"run `concord scrape members` first. Nothing to load."
        )
        return 0, 0

    result = load_members(jsonl_path=storage_path, db_path=db_path)
    typer.echo(
        f"Loaded {result.members_written} member(s) and "
        f"{result.terms_written} term(s) into {db_path}."
    )
    return result.members_written, result.terms_written


def _run_index_members(
    *,
    db_path: Path,
) -> int:
    if not db_path.exists():
        typer.echo(f"error: database not found: {db_path}", err=True)
        raise typer.Exit(code=2)

    result = index_members(db_path=db_path)
    typer.echo(f"Indexed {result.indexed_members} member(s) into members_fts.")
    return result.indexed_members


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


@scrape_app.command("members")
def scrape_members_command(
    congresses: Annotated[
        str,
        typer.Option(
            "--congresses",
            help="Comma-separated list of congress numbers to scrape.",
        ),
    ] = ",".join(str(c) for c in DEFAULT_MEMBER_CONGRESSES),
    storage_path: Annotated[
        Path,
        typer.Option("--storage", help="JSONL output file. Created if missing."),
    ] = DEFAULT_MEMBERS_JSONL,
    show_progress: Annotated[
        bool,
        typer.Option(
            "--progress/--no-progress",
            help="Print a stderr line per congress as the scrape proceeds.",
        ),
    ] = True,
) -> None:
    """Snapshot every Member of the given Congresses into JSONL.

    Each fetched Member appends one snapshot line per ADR 0006:
    ``{"fetched_at": …, "key": {"bioguide_id": …}, "payload": {…}}``.
    Re-running the command appends new snapshots; the Stage 1 loader
    projects the latest snapshot per Bioguide ID into SQLite.
    """
    parsed = _parse_congresses(congresses)
    _run_scrape_members(
        congresses=parsed,
        storage_path=storage_path,
        show_progress=show_progress,
    )


@load_app.command("members")
def load_members_command(
    storage_path: Annotated[
        Path,
        typer.Option("--storage", help="Input JSONL file (from `concord scrape members`)."),
    ] = DEFAULT_MEMBERS_JSONL,
    db_path: Annotated[
        Path,
        typer.Option("--db", help="SQLite database. Created if missing."),
    ] = DEFAULT_DB,
) -> None:
    """Project the latest Member snapshot per Bioguide ID into SQLite.

    Populates the ``members`` and ``member_terms`` tables. Re-running is
    safe: an UPSERT on ``bioguide_id`` and DELETE-then-INSERT on Terms
    keeps the projection consistent with the JSONL's latest snapshot.
    """
    _run_load_members(storage_path=storage_path, db_path=db_path)


@index_app.command("members")
def index_members_command(
    db_path: Annotated[
        Path,
        typer.Option("--db", help="SQLite database written by `concord load members`."),
    ] = DEFAULT_DB,
) -> None:
    """Populate the ``members_fts`` FTS5 virtual table.

    Truncates and repopulates ``members_fts`` from the current ``members``
    table. Idempotent — running twice gives the same final state.
    """
    _run_index_members(db_path=db_path)


@run_app.command("members")
def run_members_command(
    congresses: Annotated[
        str,
        typer.Option(
            "--congresses",
            help="Comma-separated list of congress numbers to scrape.",
        ),
    ] = ",".join(str(c) for c in DEFAULT_MEMBER_CONGRESSES),
    storage_path: Annotated[
        Path,
        typer.Option("--storage", help="JSONL canonical store. Created if missing."),
    ] = DEFAULT_MEMBERS_JSONL,
    db_path: Annotated[
        Path,
        typer.Option("--db", help="SQLite derived store. Created if missing."),
    ] = DEFAULT_DB,
    show_progress: Annotated[
        bool,
        typer.Option(
            "--progress/--no-progress",
            help="Print progress to stderr throughout all three stages.",
        ),
    ] = True,
) -> None:
    """Run all three stages for Members: scrape → load → index.

    ``CONGRESS_API_KEY`` must be set. Unlike ``run proceedings``, Stage 2
    here is FTS5-only — no OpenAI calls and no ``OPENAI_API_KEY`` required.
    """
    if not os.environ.get(ENV_API_KEY):
        typer.echo(
            f"error: {ENV_API_KEY} is not set; required for `concord scrape members`",
            err=True,
        )
        raise typer.Exit(code=2)

    parsed = _parse_congresses(congresses)

    typer.echo("→ Stage 0: scrape", err=True)
    _run_scrape_members(
        congresses=parsed,
        storage_path=storage_path,
        show_progress=show_progress,
    )

    typer.echo("→ Stage 1: load", err=True)
    _run_load_members(storage_path=storage_path, db_path=db_path)

    typer.echo("→ Stage 2: index", err=True)
    _run_index_members(db_path=db_path)

    typer.echo("✓ Done.", err=True)
