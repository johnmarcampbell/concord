"""Command-line interface for Concord.

Subcommands follow the pattern ``concord <stage> <entity>``:

- ``concord scrape proceedings`` — Stage 0. Scrape api.congress.gov +
  congress.gov into the canonical JSONL store.
- ``concord load proceedings``   — Stage 1. Mirror the JSONL into a
  ``proceedings`` table in SQLite. Idempotent on ``granule_id``.
- ``concord index proceedings``  — Stage 2. Chunk + embed every
  proceeding into FTS5 and ``sqlite-vec`` indexes. Idempotent per chunk
  and per embedding.
- ``concord run proceedings``    — Run all three stages back-to-back.

The same shape applies to Members:

- ``concord scrape members`` / ``concord load members`` /
  ``concord index members`` / ``concord run members``.

``concord serve`` is unchanged — it isn't stage-scoped.

Default paths:

- ``--storage`` (scrape, run): ``./data/proceedings.jsonl`` /
  ``./data/members.jsonl``
- ``--db``      (load, index, run, serve): ``./data/proceedings.db``
- ``--to``      (scrape proceedings, run proceedings): today's date (UTC)

Progress is on by default for long-running commands (``--no-progress``
to disable). Output goes to stderr so success summaries on stdout stay
scriptable.
"""

import json
import logging
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import IO, Annotated

import typer
from pydantic import ValidationError

from .api import ENV_API_KEY, Client
from .chunking import Chunker
from .embedding import Embedder
from .models import Proceeding
from .pipeline.index_proceedings import IndexResult, index
from .pipeline.index_proceedings import ProgressEvent as IndexProgressEvent
from .pipeline.load_proceedings import ProgressEvent, PullResult, pull
from .scraper.proceedings import scrape as scrape_proceedings_runner
from .storage import JsonlStorage, MongoStorage, SqliteStorage
from .storage.base import Storage

_log = logging.getLogger("concord.cli")

ENV_OPENAI_API_KEY = "OPENAI_API_KEY"

#: How often (in lines processed) the load command emits a progress line.
LOAD_PROGRESS_EVERY = 100

#: Default canonical store paths.
DEFAULT_JSONL = Path("./data/proceedings.jsonl")
DEFAULT_MEMBERS_JSONL = Path("./data/members.jsonl")
DEFAULT_DB = Path("./data/proceedings.db")

#: Congresses scraped by ``concord scrape members`` when ``--congresses``
#: is not passed. Matches the Phase 1 roadmap scope.
DEFAULT_MEMBER_CONGRESSES = (117, 118, 119)


class Progress:
    """Progress lines that overwrite themselves on a TTY.

    On an interactive terminal, ``update`` rewrites the same line via
    carriage-return + clear-to-end-of-line, so a long pull doesn't scroll
    the screen with one row per issue. On a non-TTY stream (cron logs,
    pipes, file redirection) it falls back to one line per update so the
    log file still has a useful record.

    Call ``commit`` between phases to end the in-place line with a newline
    — the next ``update`` then starts a fresh line. ``close`` is the same
    thing; use whichever reads better in context.
    """

    def __init__(self, enabled: bool, *, stream: IO[str] | None = None) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._enabled = enabled
        self._inplace = enabled and getattr(self._stream, "isatty", lambda: False)()
        self._open = False

    def update(self, msg: str) -> None:
        if not self._enabled:
            return
        if self._inplace:
            # \r to start of line, \x1b[K to clear from cursor to end so a
            # shorter message doesn't leave debris from the previous one.
            self._stream.write("\r\x1b[K" + msg)
            self._open = True
        else:
            self._stream.write(msg + "\n")
        self._stream.flush()

    def commit(self) -> None:
        """End the current in-place line so subsequent output starts fresh."""
        if self._open:
            self._stream.write("\n")
            self._stream.flush()
            self._open = False

    # Context-manager sugar so callers can `with Progress(...) as p:`
    def __enter__(self) -> "Progress":
        return self

    def __exit__(self, *_a: object) -> None:
        self.commit()


app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    # Plain Python tracebacks; Rich's pretty formatter is verbose and harder
    # to read for unfamiliar code paths.
    pretty_exceptions_enable=False,
    help="Concord — collect, index, and search the Congressional Record.",
)

scrape_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    help="Stage 0 — scrape an entity type into its canonical JSONL store.",
)
load_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    help="Stage 1 — mirror a JSONL store into SQLite tables.",
)
index_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    help="Stage 2 — populate derived indexes (chunks/FTS5/vectors).",
)
run_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    help="Run all stages back-to-back for one entity type.",
)

app.add_typer(scrape_app, name="scrape")
app.add_typer(load_app, name="load")
app.add_typer(index_app, name="index")
app.add_typer(run_app, name="run")


@app.callback()
def _root() -> None:
    """Concord — Congressional Record collection pipeline.

    The empty callback exists so Typer treats this as a multi-command app
    rather than collapsing the lone command into the root.
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


def _parse_congresses(raw: str) -> list[int]:
    """Parse ``--congresses 117,118,119`` into ``[117, 118, 119]``."""
    out: list[int] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            out.append(int(token))
        except ValueError as exc:
            raise typer.BadParameter(
                f"expected comma-separated integers, got {raw!r}"
            ) from exc
    if not out:
        raise typer.BadParameter(f"no congresses parsed from {raw!r}")
    return out


# ---------------------------------------------------------------------------
# Stage workers — invoked by both `concord <stage> <entity>` and
# `concord run <entity>`. The proceedings scrape orchestration lives in
# ``concord.scraper.proceedings.scrape`` (used as ``_run_pull`` here).
# ---------------------------------------------------------------------------


_run_pull = scrape_proceedings_runner


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
# Members stage workers
# ---------------------------------------------------------------------------


def _run_scrape_members(
    *,
    congresses: list[int],
    storage_path: Path,
    show_progress: bool,
) -> int:
    from .api import ApiError
    from .scraper import members as members_scraper

    try:
        api_client = Client()
    except ApiError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    progress = Progress(enabled=show_progress)

    def _on_progress(event: members_scraper.ScrapeProgressEvent) -> None:
        progress.update(
            f"  congress {event.congress:>3}  "
            f"+{event.written_in_congress:>4} written  "
            f"(total: {event.total_written})"
        )

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
        typer.echo(f"error: input file not found: {storage_path}", err=True)
        raise typer.Exit(code=2)

    from .pipeline.load_members import load as load_members

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

    from .pipeline.index_members import index as index_members

    result = index_members(db_path=db_path)
    typer.echo(f"Indexed {result.indexed_members} member(s) into members_fts.")
    return result.indexed_members


# ---------------------------------------------------------------------------
# Subcommands — Proceedings
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

    _run_pull(
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


# ---------------------------------------------------------------------------
# Subcommands — Members
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


# ---------------------------------------------------------------------------
# Subcommands — Web layer (not stage-scoped)
# ---------------------------------------------------------------------------


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
    "DEFAULT_MEMBERS_JSONL",
    "ENV_API_KEY",
    "ENV_OPENAI_API_KEY",
    "Progress",
    "app",
    "index_proceedings_command",
    "load_proceedings_command",
    "main",
    "run_proceedings_command",
    "scrape_proceedings_command",
    "serve_command",
]
