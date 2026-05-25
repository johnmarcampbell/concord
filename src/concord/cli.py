"""Command-line interface for Concord.

Subcommands:

- ``concord pull``  — Stage 0. Scrape api.congress.gov + congress.gov into
  the canonical JSONL store.
- ``concord load``  — Stage 1. Mirror the JSONL into a ``proceedings`` table
  in SQLite. Idempotent on ``granule_id``.
- ``concord index`` — Stage 2. Chunk + embed every proceeding into FTS5 and
  ``sqlite-vec`` indexes. Idempotent per chunk and per embedding.
- ``concord run``   — Run all three stages back-to-back.
- ``concord serve`` — Web layer. Run the FastAPI search demo via uvicorn.

Default paths:

- ``--storage`` (pull, run): ``./data/proceedings.jsonl``
- ``--db``      (load, index, run, serve): ``./data/proceedings.db``
- ``--to``      (pull, run): today's date (UTC)

Progress is on by default for long-running commands (``--no-progress``
to disable). Output goes to stderr so success summaries on stdout stay
scriptable.
"""

import json
import logging
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated

import httpx
import typer
from pydantic import ValidationError

from .api import ENV_API_KEY, ApiError, Client
from .chunking import Chunker
from .embedding import Embedder
from .indexing import IndexResult, index
from .indexing import ProgressEvent as IndexProgressEvent
from .models import Proceeding
from .pipeline import ProgressEvent, PullResult, pull
from .storage import JsonlStorage, MongoStorage, SqliteStorage
from .storage.base import Storage
from .text import fetch_text

_log = logging.getLogger("concord.cli")

ENV_OPENAI_API_KEY = "OPENAI_API_KEY"

#: Per-request timeout (seconds) for fetching article text from congress.gov.
#: The default httpx timeout (5s) is too aggressive for occasional slow
#: responses and triggers transport errors that abort multi-day pulls.
TEXT_FETCH_TIMEOUT = 60.0

#: How often (in lines processed) the load command emits a progress line.
LOAD_PROGRESS_EVERY = 100

#: Default canonical store paths.
DEFAULT_JSONL = Path("./data/proceedings.jsonl")
DEFAULT_DB = Path("./data/proceedings.db")

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    # Plain Python tracebacks; Rich's pretty formatter is verbose and harder
    # to read for unfamiliar code paths.
    pretty_exceptions_enable=False,
    help="Concord — collect, index, and search the Congressional Record.",
)


@app.callback()
def _root() -> None:
    """Concord — Congressional Record collection pipeline.

    The empty callback exists so Typer treats this as a multi-command app
    (with ``pull`` as a real subcommand) rather than collapsing the lone
    command into the root.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"expected YYYY-MM-DD, got {value!r}") from exc


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _require_openai_key() -> None:
    if not os.environ.get(ENV_OPENAI_API_KEY):
        typer.echo(
            f"error: {ENV_OPENAI_API_KEY} is not set; required for embedding calls",
            err=True,
        )
        raise typer.Exit(code=2)


# ---------------------------------------------------------------------------
# Stage workers — invoked by both `concord <stage>` and `concord run`.
# ---------------------------------------------------------------------------


def _run_pull(
    *,
    start: date,
    end: date,
    storage: Storage,
    storage_label: str,
    limit: int | None,
    show_progress: bool,
) -> PullResult:
    try:
        api_client = Client()
    except ApiError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    http_client = httpx.Client(timeout=TEXT_FETCH_TIMEOUT)

    def _on_progress(event: ProgressEvent) -> None:
        typer.echo(
            f"{event.issue.issue_date}  "
            f"vol {event.issue.volume} iss {event.issue.issue_number:>4}  "
            f"+{event.issue_written:>4} written, "
            f"{event.issue_skipped:>4} skipped, "
            f"{event.issue_failed:>3} failed  "
            f"(total: {event.total_written} / "
            f"{event.total_skipped} / "
            f"{event.total_failed})",
            err=True,
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

    summary = (
        f"Wrote {result.written} new proceedings to {storage_label} "
        f"(skipped {result.skipped} already present"
    )
    if result.failed:
        summary += f", {result.failed} failed to fetch — will retry on next run"
    summary += ")"
    typer.echo(summary)
    return result


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
                if show_progress and seen % LOAD_PROGRESS_EVERY == 0:
                    typer.echo(
                        f"  loaded {seen:>6} records ({written} new, {skipped} skipped"
                        + (f", {malformed} malformed)" if malformed else ")"),
                        err=True,
                    )
                if limit is not None and written >= limit:
                    break
    finally:
        storage.close()

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

    def _on_progress(event: IndexProgressEvent) -> None:
        if event.phase == "chunk":
            typer.echo(
                f"  [chunk] {event.processed:>6} proceedings chunked"
                + (
                    f"  (latest: {event.most_recent_granule_id})"
                    if event.most_recent_granule_id
                    else ""
                ),
                err=True,
            )
        else:  # "embed"
            typer.echo(
                f"  [embed] +{event.processed:>3} chunks embedded"
                + (
                    f"  (latest chunk id: {event.most_recent_chunk_id})"
                    if event.most_recent_chunk_id is not None
                    else ""
                ),
                err=True,
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


@app.command("pull")
def pull_command(
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
            help="Print a line per issue to stderr as the pull proceeds.",
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
    """Pull every article in every issue between --from and --to (inclusive).

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

    _run_pull(
        start=start,
        end=end,
        storage=storage,
        storage_label=storage_label,
        limit=limit,
        show_progress=show_progress,
    )


@app.command("load")
def load_command(
    jsonl_path: Annotated[
        Path,
        typer.Option("--jsonl", help="Input JSONL file (from `concord pull`)."),
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
    delete the SQLite file and re-run ``concord load``.
    """
    _run_load(
        jsonl_path=jsonl_path,
        db_path=db_path,
        limit=limit,
        show_progress=show_progress,
    )


@app.command("index")
def index_command(
    db_path: Annotated[
        Path,
        typer.Option("--db", help="SQLite database written by `concord load`."),
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


@app.command("run")
def run_command(
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
    """Run all three stages: pull → load → index.

    Equivalent to ``concord pull --from … --to …`` followed by ``concord
    load`` and ``concord index``. Convenient for daily cron jobs and for
    one-shot backfills against fresh ``data/`` directories.

    ``OPENAI_API_KEY`` and ``CONGRESS_API_KEY`` must both be set; the
    command fails fast if either is missing.
    """
    # Fail-fast key checks before any work happens.
    if not os.environ.get(ENV_API_KEY):
        typer.echo(f"error: {ENV_API_KEY} is not set; required for `concord pull`", err=True)
        raise typer.Exit(code=2)
    _require_openai_key()

    start = _parse_date(from_)
    end = _parse_date(to or _today())

    typer.echo("→ Stage 0: pull", err=True)
    _run_pull(
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
        limit=None,  # load reads from JSONL; limit only constrains the pull
        show_progress=show_progress,
    )

    typer.echo("→ Stage 2: index", err=True)
    _run_index(
        db_path=db_path,
        limit=limit,
        show_progress=show_progress,
    )

    typer.echo("✓ Done.", err=True)


@app.command("serve")
def serve_command(
    db_path: Annotated[
        Path,
        typer.Option("--db", help="SQLite database (from `concord load` + `concord index`)."),
    ] = DEFAULT_DB,
    host: Annotated[
        str,
        typer.Option("--host", help="Bind address. Use 127.0.0.1 behind a reverse proxy."),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option("--port", help="TCP port for the web server."),
    ] = 8000,
    reload: Annotated[
        bool,
        typer.Option("--reload/--no-reload", help="Enable uvicorn auto-reload (dev only)."),
    ] = False,
) -> None:
    """Run the public-facing search demo via uvicorn.

    Reads ``OPENAI_API_KEY`` from the environment. Production deployments
    bind to ``127.0.0.1`` and live behind Hostinger's TLS-terminating
    reverse proxy.
    """
    if not db_path.exists():
        typer.echo(f"error: database not found: {db_path}", err=True)
        raise typer.Exit(code=2)
    _require_openai_key()

    # Lazy imports so `concord --help` doesn't pay the FastAPI/uvicorn cost.
    import uvicorn

    from .web.app import create_app

    app_instance = create_app(db_path)
    uvicorn.run(app_instance, host=host, port=port, reload=reload)


def main() -> None:  # pragma: no cover - entry point shim
    app()


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "DEFAULT_DB",
    "DEFAULT_JSONL",
    "ENV_API_KEY",
    "ENV_OPENAI_API_KEY",
    "app",
    "index_command",
    "load_command",
    "main",
    "pull_command",
    "run_command",
    "serve_command",
]
