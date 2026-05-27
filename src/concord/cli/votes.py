"""CLI commands for the Votes entity (scrape / load / index / run)."""

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from ..api import ENV_API_KEY, Client
from ._apps import index_app, load_app, run_app, scrape_app
from ._common import DEFAULT_DB, Progress, _parse_congresses, _parse_csv

DEFAULT_VOTES_STORAGE_DIR = Path("./data")

#: Congresses scraped by ``concord scrape votes`` when ``--congresses``
#: is not passed. Matches the Phase 3a roadmap scope.
DEFAULT_VOTE_CONGRESSES = (117, 118, 119)

#: Sessions scraped per Congress when ``--sessions`` is not passed.
DEFAULT_VOTE_SESSIONS = (1, 2)

#: Chambers scraped by ``concord scrape votes`` when ``--chambers`` is
#: not passed. Phase 3b runs both House (api.congress.gov) and Senate
#: (senate.gov LIS XML) by default.
DEFAULT_VOTE_CHAMBERS = ("house", "senate")

#: All chamber codes the votes CLI knows about.
VALID_VOTE_CHAMBERS = ("house", "senate")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_sessions(raw: str) -> list[int]:
    parsed = _parse_csv(raw, name="sessions", coerce=int)
    bad = [s for s in parsed if s not in (1, 2)]
    if bad:
        raise typer.BadParameter(f"sessions must be 1 or 2; got {bad}")
    return parsed


def _parse_chambers(raw: str) -> list[str]:
    parsed = _parse_csv(raw, name="chambers", coerce=str.lower)
    bad = [c for c in parsed if c not in VALID_VOTE_CHAMBERS]
    if bad:
        raise typer.BadParameter(
            f"unknown chamber(s): {', '.join(bad)}. Valid: {', '.join(VALID_VOTE_CHAMBERS)}"
        )
    return parsed


# ---------------------------------------------------------------------------
# Stage workers
# ---------------------------------------------------------------------------


def _run_scrape_votes(
    *,
    congresses: list[int],
    sessions: list[int],
    chambers: list[str],
    storage_dir: Path,
    limit: int | None,
    show_progress: bool,
) -> int:
    from ..api import ApiError
    from ..scraper import votes as votes_scraper
    from ..senate_xml import SenateClient

    fetched_at = datetime.now(UTC)
    progress = Progress(enabled=show_progress)

    def _on_progress(event: votes_scraper.ScrapeProgressEvent) -> None:
        progress.update(
            f"  {event.chamber} {event.congress}/{event.session}  "
            f"+{event.votes_written:>4} written  "
            f"({event.votes_seen} seen)"
        )

    total_votes = 0
    total_positions = 0

    try:
        if "house" in chambers:
            try:
                api_client = Client()
            except ApiError as exc:
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(code=2) from exc
            with api_client:
                stats = votes_scraper.scrape_house(
                    client=api_client,
                    congresses=congresses,
                    storage_dir=storage_dir,
                    fetched_at=fetched_at,
                    sessions=tuple(sessions),
                    limit=limit,
                    progress=_on_progress if show_progress else None,
                )
            total_votes += stats.votes_written
            total_positions += stats.positions_written

        if "senate" in chambers:
            with SenateClient() as senate_client:
                stats = votes_scraper.scrape_senate(
                    client_xml=senate_client,
                    congresses=congresses,
                    storage_dir=storage_dir,
                    fetched_at=fetched_at,
                    sessions=tuple(sessions),
                    limit=limit,
                    progress=_on_progress if show_progress else None,
                )
            total_votes += stats.votes_written
    finally:
        progress.commit()

    typer.echo(
        f"Wrote {total_votes} vote detail snapshot(s) and "
        f"{total_positions} member-positions snapshot(s) to {storage_dir}."
    )
    return total_votes


def _run_load_votes(
    *,
    storage_dir: Path,
    db_path: Path,
    limit: int | None,
) -> int:
    from ..pipeline.load_votes import load as load_votes
    from ..scraper.votes import HOUSE_VOTES_JSONL_NAME, SENATE_VOTES_JSONL_NAME

    candidates = (
        storage_dir / HOUSE_VOTES_JSONL_NAME,
        storage_dir / SENATE_VOTES_JSONL_NAME,
    )
    if not any(p.exists() for p in candidates):
        listed = ", ".join(str(p) for p in candidates)
        typer.echo(
            f"No input files found ({listed}) — run `concord scrape votes` first. Nothing to load."
        )
        return 0

    stats = load_votes(storage_dir=storage_dir, db_path=db_path, limit=limit)
    typer.echo(
        f"Loaded {stats.votes_written} vote(s) and "
        f"{stats.positions_written} position(s) into {db_path} "
        f"(read {stats.snapshots_read} snapshot(s)"
        + (f", {stats.malformed} malformed" if stats.malformed else "")
        + ")."
    )
    return stats.votes_written


def _run_index_votes(
    *,
    db_path: Path,
    limit: int | None,
) -> int:
    if not db_path.exists():
        typer.echo(f"error: database not found: {db_path}", err=True)
        raise typer.Exit(code=2)

    from ..pipeline.index_votes import index as index_votes

    stats = index_votes(db_path=db_path, limit=limit)
    typer.echo(
        f"Flagged {stats.votes_flagged_party_unity} party-unity vote(s); "
        f"scored {stats.members_scored} member-congress row(s) into member_party_unity."
    )
    return stats.members_scored


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


@scrape_app.command("votes")
def scrape_votes_command(
    congresses: Annotated[
        str,
        typer.Option(
            "--congresses",
            help="Comma-separated list of congress numbers to scrape.",
        ),
    ] = ",".join(str(c) for c in DEFAULT_VOTE_CONGRESSES),
    sessions: Annotated[
        str,
        typer.Option(
            "--sessions",
            help="Comma-separated list of sessions (1, 2).",
        ),
    ] = ",".join(str(s) for s in DEFAULT_VOTE_SESSIONS),
    chambers: Annotated[
        str,
        typer.Option(
            "--chambers",
            help="Comma-separated chambers (house, senate).",
        ),
    ] = ",".join(DEFAULT_VOTE_CHAMBERS),
    storage_dir: Annotated[
        Path,
        typer.Option(
            "--storage-dir",
            help="Directory holding house_votes.jsonl + senate_votes.jsonl + sidecar files.",
        ),
    ] = DEFAULT_VOTES_STORAGE_DIR,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            help="Maximum number of detail snapshots to write per chamber.",
        ),
    ] = None,
    show_progress: Annotated[
        bool,
        typer.Option(
            "--progress/--no-progress",
            help="Print a stderr line per (congress, session) pair.",
        ),
    ] = True,
) -> None:
    """Snapshot roll-call votes into ``<storage-dir>/{house,senate}_votes*.jsonl``."""
    parsed_congresses = _parse_congresses(congresses)
    parsed_sessions = _parse_sessions(sessions)
    parsed_chambers = _parse_chambers(chambers)
    _run_scrape_votes(
        congresses=parsed_congresses,
        sessions=parsed_sessions,
        chambers=parsed_chambers,
        storage_dir=storage_dir,
        limit=limit,
        show_progress=show_progress,
    )


@load_app.command("votes")
def load_votes_command(
    storage_dir: Annotated[
        Path,
        typer.Option(
            "--storage-dir",
            help="Directory holding house_votes.jsonl + house_vote_positions.jsonl.",
        ),
    ] = DEFAULT_VOTES_STORAGE_DIR,
    db_path: Annotated[
        Path,
        typer.Option("--db", help="SQLite database. Created if missing."),
    ] = DEFAULT_DB,
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="Maximum number of vote rows to UPSERT."),
    ] = None,
) -> None:
    """Project the latest vote + positions snapshot per key into SQLite."""
    _run_load_votes(storage_dir=storage_dir, db_path=db_path, limit=limit)


@index_app.command("votes")
def index_votes_command(
    db_path: Annotated[
        Path,
        typer.Option("--db", help="SQLite database written by `concord load votes`."),
    ] = DEFAULT_DB,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            help=(
                "Spot-check only: caps the numerator-pass vote_positions "
                "row set. The party-unity flag pass always runs over all "
                "rows. With a small limit, some Members' denominators "
                "are truncated mid-Member; not for production."
            ),
        ),
    ] = None,
) -> None:
    """Compute ``votes.is_party_unity`` + ``member_party_unity``.

    Truncate-then-repopulate. Re-running converges to the latest
    snapshot of ``votes`` + ``vote_positions``.
    """
    _run_index_votes(db_path=db_path, limit=limit)


@run_app.command("votes")
def run_votes_command(
    congresses: Annotated[
        str,
        typer.Option("--congresses", help="Comma-separated congress numbers."),
    ] = ",".join(str(c) for c in DEFAULT_VOTE_CONGRESSES),
    sessions: Annotated[
        str,
        typer.Option("--sessions", help="Comma-separated sessions (1, 2)."),
    ] = ",".join(str(s) for s in DEFAULT_VOTE_SESSIONS),
    chambers: Annotated[
        str,
        typer.Option("--chambers", help="Comma-separated chambers."),
    ] = ",".join(DEFAULT_VOTE_CHAMBERS),
    storage_dir: Annotated[
        Path,
        typer.Option("--storage-dir", help="JSONL canonical store directory."),
    ] = DEFAULT_VOTES_STORAGE_DIR,
    db_path: Annotated[
        Path,
        typer.Option("--db", help="SQLite derived store. Created if missing."),
    ] = DEFAULT_DB,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            help="Cap on new votes scraped (load and index run unbounded).",
        ),
    ] = None,
    show_progress: Annotated[
        bool,
        typer.Option(
            "--progress/--no-progress",
            help="Print progress to stderr throughout all three stages.",
        ),
    ] = True,
) -> None:
    """Run all three stages for Votes: scrape → load → index."""
    parsed_congresses = _parse_congresses(congresses)
    parsed_sessions = _parse_sessions(sessions)
    parsed_chambers = _parse_chambers(chambers)

    if "house" in parsed_chambers and not os.environ.get(ENV_API_KEY):
        typer.echo(
            f"error: {ENV_API_KEY} is not set; required for House votes",
            err=True,
        )
        raise typer.Exit(code=2)

    typer.echo("→ Stage 0: scrape", err=True)
    _run_scrape_votes(
        congresses=parsed_congresses,
        sessions=parsed_sessions,
        chambers=parsed_chambers,
        storage_dir=storage_dir,
        limit=limit,
        show_progress=show_progress,
    )

    typer.echo("→ Stage 1: load", err=True)
    _run_load_votes(storage_dir=storage_dir, db_path=db_path, limit=None)

    typer.echo("→ Stage 2: index", err=True)
    _run_index_votes(db_path=db_path, limit=None)

    typer.echo("✓ Done.", err=True)
