"""Command-line interface for Concord.

Subcommands:

- ``concord pull`` — Stage 0. Scrape api.congress.gov + congress.gov into
  the canonical JSONL store.
- ``concord load`` — Stage 1. Mirror the JSONL into a ``proceedings`` table
  in SQLite. Idempotent on ``granule_id``.
- ``concord index`` — Stage 2. Chunk + embed every proceeding into FTS5
  and ``sqlite-vec`` indexes. Idempotent per chunk and per embedding.
- ``concord serve`` — Web layer. Run the FastAPI search demo via uvicorn.
"""

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Annotated

import httpx
import typer
from pydantic import ValidationError

from .api import ENV_API_KEY, ApiError, Client
from .chunking import Chunker
from .embedding import Embedder
from .indexing import index
from .models import Proceeding
from .pipeline import ProgressEvent, pull
from .storage import JsonlStorage, MongoStorage, SqliteStorage
from .storage.base import Storage
from .text import fetch_text

_log = logging.getLogger("concord.cli")

ENV_OPENAI_API_KEY = "OPENAI_API_KEY"

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Pull Congressional Record articles from api.congress.gov.",
)


@app.callback()
def _root() -> None:
    """Concord — Congressional Record collection pipeline.

    The empty callback exists so Typer treats this as a multi-command app
    (with ``pull`` as a real subcommand) rather than collapsing the lone
    command into the root.
    """


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"expected YYYY-MM-DD, got {value!r}") from exc


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
        str,
        typer.Option(
            "--to",
            help="End date YYYY-MM-DD (inclusive).",
            show_default=False,
        ),
    ],
    storage_path: Annotated[
        Path,
        typer.Option(
            "--storage",
            help="JSONL output file. Created if missing.",
        ),
    ] = Path("./proceedings.jsonl"),
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            help="Maximum number of new proceedings to write.",
        ),
    ] = None,
    show_progress: Annotated[
        bool,
        typer.Option(
            "--progress/--no-progress",
            help="Print a line per issue as the pull proceeds. Useful for backfills.",
        ),
    ] = False,
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
        typer.Option("--mongo-collection", help="MongoDB collection name (with --mongo-uri)."),
    ] = "proceedings",
) -> None:
    """Pull every article in every issue between --from and --to (inclusive).

    Re-running the same command after a crash, network drop, or Ctrl+C is
    safe: already-stored articles are detected by their granule ID and
    skipped without re-fetching.
    """
    start = _parse_date(from_)
    end = _parse_date(to)

    try:
        api_client = Client()  # reads CONGRESS_API_KEY from env
    except ApiError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

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
    http_client = httpx.Client()

    def _print_progress(event: ProgressEvent) -> None:
        typer.echo(
            f"{event.issue.issue_date}  "
            f"vol {event.issue.volume} iss {event.issue.issue_number:>4}  "
            f"+{event.issue_written:>4} written, {event.issue_skipped:>4} skipped  "
            f"(total: {event.total_written} / {event.total_skipped})",
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
                progress=_print_progress if show_progress else None,
            )
    finally:
        http_client.close()

    typer.echo(
        f"Wrote {result.written} new proceedings to {storage_label} "
        f"(skipped {result.skipped} already present)"
    )


@app.command("load")
def load_command(
    jsonl_path: Annotated[
        Path,
        typer.Option(
            "--jsonl",
            help="Input JSONL file produced by `concord pull` (one Proceeding per line).",
            show_default=False,
        ),
    ],
    db_path: Annotated[
        Path,
        typer.Option(
            "--db",
            help="Output SQLite database. Created if missing.",
        ),
    ] = Path("./proceedings.db"),
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            help="Maximum number of new proceedings to write.",
        ),
    ] = None,
) -> None:
    """Mirror a JSONL file into the SQLite ``proceedings`` table.

    Reads the JSONL line by line, validates each entry as a Proceeding, and
    writes it to SQLite via SqliteStorage. Re-running over the same JSONL is
    a no-op — dedup is enforced by ``granule_id``.

    Schema changes are not handled by migrations; if the schema ever changes,
    delete the SQLite file and re-run ``concord load``.
    """
    if not jsonl_path.exists():
        typer.echo(f"error: input file not found: {jsonl_path}", err=True)
        raise typer.Exit(code=2)

    storage = SqliteStorage(db_path)
    written = 0
    skipped = 0
    malformed = 0

    try:
        with jsonl_path.open("r", encoding="utf-8") as fh:
            for line_no, raw in enumerate(fh, 1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    proceeding = Proceeding.model_validate_json(line)
                except (ValidationError, json.JSONDecodeError) as exc:
                    malformed += 1
                    _log.warning("skipping malformed line %d: %s", line_no, exc)
                    continue
                if storage.has(proceeding.granule_id):
                    skipped += 1
                    continue
                storage.write(proceeding)
                written += 1
                if limit is not None and written >= limit:
                    break
    finally:
        storage.close()

    summary = f"Loaded {written} new proceedings into {db_path} (skipped {skipped} already present"
    if malformed:
        summary += f", {malformed} malformed lines skipped"
    summary += ")"
    typer.echo(summary)


@app.command("index")
def index_command(
    db_path: Annotated[
        Path,
        typer.Option(
            "--db",
            help="SQLite database written by `concord load`.",
            show_default=False,
        ),
    ],
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            help="Cap on new chunks written in the chunk pass (the embed pass still runs).",
        ),
    ] = None,
) -> None:
    """Chunk every proceeding and embed every chunk via OpenAI.

    Two passes, both idempotent. Safe to interrupt and re-run — a crashed
    run loses at most one proceeding's chunks + one embedding batch's worth
    of OpenAI calls (~$0.002).
    """
    if not db_path.exists():
        typer.echo(f"error: database not found: {db_path}", err=True)
        raise typer.Exit(code=2)

    if not os.environ.get(ENV_OPENAI_API_KEY):
        typer.echo(
            f"error: {ENV_OPENAI_API_KEY} is not set; required for embedding calls",
            err=True,
        )
        raise typer.Exit(code=2)

    # Lazy import so `concord --help` doesn't pay the openai SDK import cost.
    import openai

    storage = SqliteStorage(db_path)
    try:
        result = index(
            storage,
            chunker=Chunker(),
            embedder=Embedder(openai.OpenAI()),
            limit=limit,
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


@app.command("serve")
def serve_command(
    db_path: Annotated[
        Path,
        typer.Option(
            "--db",
            help="SQLite database produced by `concord load` + `concord index`.",
            show_default=False,
        ),
    ],
    host: Annotated[
        str,
        typer.Option(
            "--host",
            help="Bind address. Use 127.0.0.1 behind a reverse proxy.",
        ),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option(
            "--port",
            help="TCP port for the web server.",
        ),
    ] = 8000,
    reload: Annotated[
        bool,
        typer.Option(
            "--reload/--no-reload",
            help="Enable uvicorn auto-reload (dev only).",
        ),
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

    if not os.environ.get(ENV_OPENAI_API_KEY):
        typer.echo(
            f"error: {ENV_OPENAI_API_KEY} is not set; required for embedding queries",
            err=True,
        )
        raise typer.Exit(code=2)

    # Lazy imports so `concord --help` doesn't pay the FastAPI/uvicorn cost.
    import uvicorn

    from .web.app import create_app

    app_instance = create_app(db_path)
    uvicorn.run(app_instance, host=host, port=port, reload=reload)


def main() -> None:  # pragma: no cover - entry point shim
    app()


if __name__ == "__main__":  # pragma: no cover
    main()


# re-export for any callers who want to introspect the env-var name
__all__ = [
    "ENV_API_KEY",
    "ENV_OPENAI_API_KEY",
    "app",
    "index_command",
    "load_command",
    "main",
    "pull_command",
    "serve_command",
]
