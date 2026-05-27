"""CLI commands for the Proceedings entity (scrape / load / index / run)."""

import json
import logging
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from ..api import ENV_API_KEY
from ..chunking import Chunker
from ..embedding import Embedder
from ..models import Proceeding
from ..pipeline.index_proceedings import IndexResult, index
from ..pipeline.index_proceedings import ProgressEvent as IndexProgressEvent
from ..pipeline.load_proceedings import ProgressEvent as ScrapeProgressEvent
from ..scraper.proceedings import scrape as _run_pull
from ..storage import JsonlStorage, MongoStorage, SqliteStorage
from ..storage.base import Storage
from ._apps import index_app, load_app, run_app, scrape_app
from ._common import (
    DEFAULT_DB,
    ENV_OPENAI_API_KEY,
    LOAD_PROGRESS_EVERY,
    Progress,
    _parse_date,
    _require_openai_key,
    _today,
)

_log = logging.getLogger("concord.cli.proceedings")

DEFAULT_JSONL = Path("./data/proceedings.jsonl")


# ---------------------------------------------------------------------------
# Stage workers
# ---------------------------------------------------------------------------


def _run_scrape_proceedings(
    *,
    start: date,
    end: date,
    storage: Storage,
    storage_label: str,
    limit: int | None,
    show_progress: bool,
) -> None:
    """Wrap :func:`_run_pull` with a :class:`Progress` display.

    Matches the ``_run_scrape_*`` pattern used by every other entity module:
    the CLI layer owns the ``Progress`` instance and passes a callback down
    to the scraper, keeping ``scraper.proceedings`` free of any CLI imports.
    """
    progress = Progress(enabled=show_progress)

    def _on_progress(event: ScrapeProgressEvent) -> None:
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
        _run_pull(
            start=start,
            end=end,
            storage=storage,
            storage_label=storage_label,
            limit=limit,
            progress=_on_progress if show_progress else None,
        )
    finally:
        progress.commit()


def _run_load(
    *,
    jsonl_path: Path,
    db_path: Path,
    limit: int | None,
    show_progress: bool,
) -> tuple[int, int, int]:
    """Return ``(written, skipped, malformed)``."""
    if not jsonl_path.exists():
        typer.echo(f"error: input file not found: {jsonl_path}", err=True)
        raise typer.Exit(code=2)

    storage = SqliteStorage(db_path)
    progress = Progress(enabled=show_progress)
    written = 0
    skipped = 0
    malformed = 0
    seen = 0

    try:
        with jsonl_path.open("r", encoding="utf-8") as fh:
            for line_no, raw in enumerate(fh, 1):
                line = raw.strip()
                if not line:
                    continue
                seen += 1
                try:
                    proceeding = Proceeding.model_validate_json(line)
                except (ValidationError, json.JSONDecodeError) as exc:
                    malformed += 1
                    _log.warning("skipping malformed line %d: %s", line_no, exc)
                    continue
                if storage.has(proceeding.granule_id):
                    skipped += 1
                else:
                    storage.write(proceeding)
                    written += 1
                if seen % LOAD_PROGRESS_EVERY == 0:
                    progress.update(
                        f"  loaded {seen:>6} records ({written} new, {skipped} skipped"
                        + (f", {malformed} malformed)" if malformed else ")")
                    )
                if limit is not None and written >= limit:
                    break
    finally:
        storage.close()
        progress.commit()

    summary = f"Loaded {written} new proceedings into {db_path} (skipped {skipped} already present"
    if malformed:
        summary += f", {malformed} malformed lines skipped"
    summary += ")"
    typer.echo(summary)
    return written, skipped, malformed


def _run_index(
    *,
    db_path: Path,
    limit: int | None,
    show_progress: bool,
) -> IndexResult:
    if not db_path.exists():
        typer.echo(f"error: database not found: {db_path}", err=True)
        raise typer.Exit(code=2)
    _require_openai_key()

    # Lazy import so `concord --help` doesn't pay the openai SDK import cost.
    import openai

    storage = SqliteStorage(db_path)
    progress = Progress(enabled=show_progress)
    last_phase: str | None = None

    # Per-batch counter so the embed line keeps growing instead of
    # restarting from each batch's `processed` value (which is per-event).
    embed_total = 0

    def _on_progress(event: IndexProgressEvent) -> None:
        nonlocal last_phase, embed_total
        if event.phase != last_phase:
            # Phase boundary: lock in whatever line was being updated.
            progress.commit()
            last_phase = event.phase
            if event.phase == "embed":
                embed_total = 0
        if event.phase == "chunk":
            progress.update(
                f"  [chunk] {event.processed:>6} proceedings chunked"
                + (
                    f"  (latest: {event.most_recent_granule_id})"
                    if event.most_recent_granule_id
                    else ""
                )
            )
        else:  # "embed"
            embed_total += event.processed
            progress.update(
                f"  [embed] {embed_total:>6} chunks embedded"
                + (
                    f"  (latest chunk id: {event.most_recent_chunk_id})"
                    if event.most_recent_chunk_id is not None
                    else ""
                )
            )

    try:
        result = index(
            storage,
            chunker=Chunker(),
            embedder=Embedder(openai.OpenAI()),
            limit=limit,
            progress=_on_progress if show_progress else None,
        )
    finally:
        storage.close()
        progress.commit()

    typer.echo(
        f"Indexed: chunked {result.chunked_proceedings} new proceedings "
        f"({result.chunks_written} new chunks, "
        f"skipped {result.skipped_chunked} already chunked); "
        f"embedded {result.embedded_chunks} new chunks "
        f"(skipped {result.skipped_embedded} already embedded)"
    )
    return result


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


@scrape_app.command("proceedings")
def scrape_proceedings_command(
    from_: Annotated[
        str,
        typer.Option(
            "--from",
            help="Start date YYYY-MM-DD (inclusive).",
            show_default=False,
        ),
    ],
    to: Annotated[
        str | None,
        typer.Option(
            "--to",
            help="End date YYYY-MM-DD (inclusive). Defaults to today (UTC).",
            show_default=False,
        ),
    ] = None,
    storage_path: Annotated[
        Path,
        typer.Option("--storage", help="JSONL output file. Created if missing."),
    ] = DEFAULT_JSONL,
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="Maximum number of new proceedings to write."),
    ] = None,
    show_progress: Annotated[
        bool,
        typer.Option(
            "--progress/--no-progress",
            help="Print a line per issue to stderr as the scrape proceeds.",
        ),
    ] = True,
    mongo_uri: Annotated[
        str | None,
        typer.Option(
            "--mongo-uri",
            help="MongoDB connection URI. When set, --storage is ignored.",
        ),
    ] = None,
    mongo_db: Annotated[
        str,
        typer.Option("--mongo-db", help="MongoDB database name (with --mongo-uri)."),
    ] = "concord",
    mongo_collection: Annotated[
        str,
        typer.Option(
            "--mongo-collection",
            help="MongoDB collection name (with --mongo-uri).",
        ),
    ] = "proceedings",
) -> None:
    """Scrape every article in every issue between --from and --to (inclusive).

    Re-running the same command after a crash, network drop, or Ctrl+C is
    safe: already-stored articles are detected by their granule ID and
    skipped without re-fetching.
    """
    start = _parse_date(from_)
    end = _parse_date(to or _today())

    storage: Storage
    storage_label: str
    if mongo_uri is not None:
        try:
            storage = MongoStorage.from_uri(mongo_uri, db=mongo_db, collection=mongo_collection)
        except ImportError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        storage_label = f"mongodb://{mongo_db}.{mongo_collection}"
    else:
        storage = JsonlStorage(storage_path)
        storage_label = str(storage_path)

    _run_scrape_proceedings(
        start=start,
        end=end,
        storage=storage,
        storage_label=storage_label,
        limit=limit,
        show_progress=show_progress,
    )


@load_app.command("proceedings")
def load_proceedings_command(
    jsonl_path: Annotated[
        Path,
        typer.Option("--jsonl", help="Input JSONL file (from `concord scrape proceedings`)."),
    ] = DEFAULT_JSONL,
    db_path: Annotated[
        Path,
        typer.Option("--db", help="Output SQLite database. Created if missing."),
    ] = DEFAULT_DB,
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="Maximum number of new proceedings to write."),
    ] = None,
    show_progress: Annotated[
        bool,
        typer.Option(
            "--progress/--no-progress",
            help=f"Print a stderr line every {LOAD_PROGRESS_EVERY} records.",
        ),
    ] = True,
) -> None:
    """Mirror a JSONL file into the SQLite ``proceedings`` table.

    Reads the JSONL line by line, validates each entry as a Proceeding, and
    writes it to SQLite via SqliteStorage. Re-running over the same JSONL is
    a no-op — dedup is enforced by ``granule_id``.

    Schema changes are not handled by migrations; if the schema ever changes,
    delete the SQLite file and re-run ``concord load proceedings``.
    """
    _run_load(
        jsonl_path=jsonl_path,
        db_path=db_path,
        limit=limit,
        show_progress=show_progress,
    )


@index_app.command("proceedings")
def index_proceedings_command(
    db_path: Annotated[
        Path,
        typer.Option("--db", help="SQLite database written by `concord load proceedings`."),
    ] = DEFAULT_DB,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            help="Cap on new chunks written in the chunk pass (the embed pass still runs).",
        ),
    ] = None,
    show_progress: Annotated[
        bool,
        typer.Option(
            "--progress/--no-progress",
            help="Emit a stderr line per chunked proceeding and per embedding batch.",
        ),
    ] = True,
) -> None:
    """Chunk every proceeding and embed every chunk via OpenAI.

    Two passes, both idempotent. Safe to interrupt and re-run — a crashed
    run loses at most one proceeding's chunks + one embedding batch's worth
    of OpenAI calls (~$0.002).
    """
    _run_index(db_path=db_path, limit=limit, show_progress=show_progress)


@run_app.command("proceedings")
def run_proceedings_command(
    from_: Annotated[
        str,
        typer.Option("--from", help="Start date YYYY-MM-DD (inclusive)."),
    ],
    to: Annotated[
        str | None,
        typer.Option(
            "--to",
            help="End date YYYY-MM-DD (inclusive). Defaults to today (UTC).",
            show_default=False,
        ),
    ] = None,
    storage_path: Annotated[
        Path,
        typer.Option("--storage", help="JSONL canonical store. Created if missing."),
    ] = DEFAULT_JSONL,
    db_path: Annotated[
        Path,
        typer.Option("--db", help="SQLite derived store. Created if missing."),
    ] = DEFAULT_DB,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            help="Cap on new proceedings to scrape (also limits the index chunk pass).",
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
    """Run all three stages for Proceedings: scrape → load → index.

    Equivalent to ``concord scrape proceedings --from … --to …`` followed
    by ``concord load proceedings`` and ``concord index proceedings``.

    ``OPENAI_API_KEY`` and ``CONGRESS_API_KEY`` must both be set; the
    command fails fast if either is missing.
    """
    if not os.environ.get(ENV_API_KEY):
        typer.echo(
            f"error: {ENV_API_KEY} is not set; required for `concord scrape proceedings`",
            err=True,
        )
        raise typer.Exit(code=2)
    _require_openai_key()

    start = _parse_date(from_)
    end = _parse_date(to or _today())

    typer.echo("→ Stage 0: scrape", err=True)
    _run_scrape_proceedings(
        start=start,
        end=end,
        storage=JsonlStorage(storage_path),
        storage_label=str(storage_path),
        limit=limit,
        show_progress=show_progress,
    )

    typer.echo("→ Stage 1: load", err=True)
    _run_load(
        jsonl_path=storage_path,
        db_path=db_path,
        limit=None,  # load reads from JSONL; limit only constrains the scrape
        show_progress=show_progress,
    )

    typer.echo("→ Stage 2: index", err=True)
    _run_index(
        db_path=db_path,
        limit=limit,
        show_progress=show_progress,
    )

    typer.echo("✓ Done.", err=True)
