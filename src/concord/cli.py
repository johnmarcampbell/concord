"""Command-line interface for Concord.

Subcommands:

- ``concord pull`` — Stage 0. Scrape api.congress.gov + congress.gov into
  the canonical JSONL store.
- ``concord load`` — Stage 1. Mirror the JSONL into a ``proceedings`` table
  in SQLite. Idempotent on ``granule_id``.
"""

import json
import logging
from datetime import date
from pathlib import Path
from typing import Annotated

import httpx
import typer
from pydantic import ValidationError

from .api import ENV_API_KEY, ApiError, Client
from .models import Proceeding
from .pipeline import ProgressEvent, pull
from .storage import JsonlStorage, MongoStorage, SqliteStorage
from .storage.base import Storage
from .text import fetch_text

_log = logging.getLogger("concord.cli")

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


def main() -> None:  # pragma: no cover - entry point shim
    app()


if __name__ == "__main__":  # pragma: no cover
    main()


# re-export for any callers who want to introspect the env-var name
__all__ = ["ENV_API_KEY", "app", "load_command", "main", "pull_command"]
