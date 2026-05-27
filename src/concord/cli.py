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
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import IO, Annotated, Any

import typer
from pydantic import ValidationError

from .api import ENV_API_KEY, Client
from .chunking import Chunker
from .embedding import Embedder
from .models import Proceeding
from .pipeline.index_proceedings import IndexResult, index
from .pipeline.index_proceedings import ProgressEvent as IndexProgressEvent
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

#: Default storage directory for Bills (and the other Phase 2b
#: sub-endpoint JSONLs once they exist). One directory because ADR 0009
#: splits a multi-endpoint entity into multiple sibling files.
DEFAULT_BILLS_STORAGE_DIR = Path("./data")

#: Congresses scraped by ``concord scrape members`` when ``--congresses``
#: is not passed. Matches the Phase 1 roadmap scope.
DEFAULT_MEMBER_CONGRESSES = (117, 118, 119)

#: Congresses scraped by ``concord scrape bills`` when ``--congresses``
#: is not passed. Matches the Phase 2 roadmap scope.
DEFAULT_BILL_CONGRESSES = (117, 118, 119)

#: All eight legislative Bill type codes. Used both as the
#: ``concord scrape bills --bill-types`` default and as the validator
#: for the ``/bills/{...}/{bill_type}/...`` URL segment.
DEFAULT_BILL_TYPES = ("hr", "hres", "hjres", "hconres", "s", "sres", "sjres", "sconres")

#: Tier-2 sub-endpoint names that ``concord scrape bills enrich`` defaults to.
DEFAULT_BILL_SECTIONS = ("cosponsors", "actions", "subjects", "titles", "summaries")

#: Congresses scraped by ``concord scrape votes`` when ``--congresses``
#: is not passed. Matches the Phase 3a roadmap scope.
DEFAULT_VOTE_CONGRESSES = (117, 118, 119)

#: Sessions scraped per Congress when ``--sessions`` is not passed.
DEFAULT_VOTE_SESSIONS = (1, 2)

#: Chambers scraped by ``concord scrape votes`` when ``--chambers`` is
#: not passed. Phase 3a is House-only; Phase 3b will extend to senate.
DEFAULT_VOTE_CHAMBERS = ("house",)

#: All chamber codes the votes CLI knows about. ``senate`` is a no-op
#: in 3a (logs a "lands in 3b" message and returns).
VALID_VOTE_CHAMBERS = ("house", "senate")

#: Default storage dir for the votes JSONLs.
DEFAULT_VOTES_STORAGE_DIR = Path("./data")

#: Default cap on the auto-selected enrichment batch when --bill-ids is
#: not given. Operators tweak via ``--limit N``.
DEFAULT_ENRICH_AUTO_LIMIT = 25

#: Expected number of dash-separated parts in a ``--bill-ids`` token.
_BILL_ID_PART_COUNT = 3


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


def _parse_csv(raw: str, *, name: str, coerce: "Callable[[str], Any]") -> "list[Any]":
    """Parse a comma-separated CLI option into a list of values.

    ``coerce`` is applied to each non-empty token (e.g. ``int`` for
    ``--congresses``, ``str.lower`` for ``--bill-types``). ``name`` is
    used in error messages.
    """
    out: list[Any] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            out.append(coerce(token))
        except (TypeError, ValueError) as exc:
            raise typer.BadParameter(f"bad value in --{name}: {token!r}") from exc
    if not out:
        raise typer.BadParameter(f"no values parsed from --{name} {raw!r}")
    return out


def _parse_congresses(raw: str) -> list[int]:
    """Parse ``--congresses 117,118,119`` into ``[117, 118, 119]``."""
    return _parse_csv(raw, name="congresses", coerce=int)


def _parse_bill_types(raw: str) -> list[str]:
    """Parse ``--bill-types hr,s,...`` into lowercased codes.

    Rejects unknown codes against :data:`DEFAULT_BILL_TYPES` so a typo
    fails fast instead of producing zero scraper output.
    """
    parsed = _parse_csv(raw, name="bill-types", coerce=str.lower)
    unknown = [t for t in parsed if t not in DEFAULT_BILL_TYPES]
    if unknown:
        raise typer.BadParameter(
            f"unknown bill type(s): {', '.join(unknown)}. Valid: {', '.join(DEFAULT_BILL_TYPES)}"
        )
    return parsed


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
        # No input file is a no-op, not an error: load is idempotent and
        # callable in any order. The user probably hasn't run scrape yet —
        # tell them what they need.
        typer.echo(
            f"No input file at {storage_path} — "
            f"run `concord scrape members` first. Nothing to load."
        )
        return 0, 0

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
# Bills stage workers
# ---------------------------------------------------------------------------


def _run_scrape_bills(
    *,
    congresses: list[int],
    bill_types: list[str],
    storage_dir: Path,
    limit: int | None,
    show_progress: bool,
) -> int:
    from .api import ApiError
    from .scraper import bills as bills_scraper

    try:
        api_client = Client()
    except ApiError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    progress = Progress(enabled=show_progress)

    def _on_progress(event: bills_scraper.ScrapeProgressEvent) -> None:
        progress.update(
            f"  congress {event.congress:>3}  {event.bill_type:<7}  "
            f"+{event.bills_written:>5} written  "
            f"({event.bills_seen} seen)"
        )

    try:
        with api_client:
            stats = bills_scraper.scrape_basic(
                client=api_client,
                congresses=congresses,
                storage_dir=storage_dir,
                fetched_at=datetime.now(UTC),
                bill_types=bill_types,
                limit=limit,
                progress=_on_progress if show_progress else None,
            )
    finally:
        progress.commit()

    typer.echo(
        f"Wrote {stats.bills_written} bill snapshot(s) to "
        f"{storage_dir / bills_scraper.BILLS_JSONL_NAME} "
        f"across {len(congresses)} congress(es) x {len(bill_types)} bill type(s)."
    )
    return stats.bills_written


def _autoselect_unenriched_bills(
    db_path: Path,
    *,
    limit: int,
) -> list[tuple[int, str, int]]:
    """Return ``[(congress, bill_type, bill_number)]`` for un-enriched bills, newest first."""
    if not db_path.exists():
        typer.echo(f"error: database not found: {db_path}", err=True)
        raise typer.Exit(code=2)
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            "SELECT congress, bill_type, bill_number FROM bills "
            "WHERE cosponsors_fetched_at IS NULL "
            "ORDER BY (introduced_date IS NULL), introduced_date DESC, "
            "         congress DESC, bill_number ASC "
            "LIMIT ?",
            (limit,),
        )
        return [(int(c), str(t), int(n)) for c, t, n in cursor.fetchall()]
    finally:
        conn.close()


def _run_scrape_bills_enrich(
    *,
    bill_keys: list[tuple[int, str, int]],
    sections: list[str],
    storage_dir: Path,
    limit: int | None,
    show_progress: bool,
) -> int:
    from .api import ApiError
    from .scraper import bills as bills_scraper

    try:
        api_client = Client()
    except ApiError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    progress = Progress(enabled=show_progress)

    def _on_progress(event: bills_scraper.EnrichProgressEvent) -> None:
        c, t, n = event.bill_key
        suffix = ""
        if event.partial_failures:
            suffix = f" (failed: {', '.join(event.partial_failures)})"
        progress.update(
            f"  {c}-{t}-{n}  +{event.sections_written}/{len(sections)} sections{suffix}"
        )

    try:
        with api_client:
            stats = bills_scraper.scrape_enrichment(
                client=api_client,
                bill_keys=bill_keys,
                storage_dir=storage_dir,
                fetched_at=datetime.now(UTC),
                sections=sections,
                limit=limit,
                progress=_on_progress if show_progress else None,
            )
    finally:
        progress.commit()

    typer.echo(
        f"Enriched {stats.bills_enriched} bill(s); "
        f"wrote {stats.snapshots_written} snapshot(s) to {storage_dir}"
        + (f" ({stats.section_failures} section failure(s))" if stats.section_failures else "")
        + "."
    )
    return stats.snapshots_written


def _run_load_bills(
    *,
    storage_dir: Path,
    db_path: Path,
    limit: int | None,
) -> int:
    from .pipeline.load_bills import load as load_bills
    from .scraper.bills import BILLS_JSONL_NAME

    jsonl_path = storage_dir / BILLS_JSONL_NAME
    if not jsonl_path.exists():
        typer.echo(
            f"No input file at {jsonl_path} — run `concord scrape bills` first. Nothing to load."
        )
        return 0

    stats = load_bills(storage_dir=storage_dir, db_path=db_path, limit=limit)
    typer.echo(
        f"Loaded {stats.bills_written} bill(s) into {db_path} "
        f"(read {stats.snapshots_read} snapshot(s)"
        + (f", {stats.malformed} malformed" if stats.malformed else "")
        + ")."
    )
    return stats.bills_written


def _run_index_bills(
    *,
    db_path: Path,
    limit: int | None,
) -> int:
    if not db_path.exists():
        typer.echo(f"error: database not found: {db_path}", err=True)
        raise typer.Exit(code=2)

    from .pipeline.index_bills import index as index_bills

    stats = index_bills(db_path=db_path, limit=limit)
    typer.echo(f"Indexed {stats.indexed_bills} bill(s) into bills_fts.")
    return stats.indexed_bills


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
# Subcommands — Bills
# ---------------------------------------------------------------------------


scrape_bills_app = typer.Typer(
    no_args_is_help=False,
    add_completion=False,
    pretty_exceptions_enable=False,
    help=(
        "Stage 0 — scrape Bills. Without a sub-command, snapshots Bill identity "
        "records into bills.jsonl. Use `enrich` for tier-2 sub-endpoints."
    ),
    invoke_without_command=True,
)
scrape_app.add_typer(scrape_bills_app, name="bills")


def _parse_bill_ids(raw: str) -> list[tuple[int, str, int]]:
    """Parse ``--bill-ids 119-hr-1,119-hr-22`` into ``[(119,"hr",1), ...]``."""
    out: list[tuple[int, str, int]] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        bits = token.split("-")
        if len(bits) != _BILL_ID_PART_COUNT:
            raise typer.BadParameter(
                f"bad --bill-ids token {token!r}; expected '<congress>-<type>-<number>'"
            )
        try:
            congress = int(bits[0])
            bill_type = bits[1].lower()
            bill_number = int(bits[2])
        except ValueError as exc:
            raise typer.BadParameter(f"bad --bill-ids token {token!r}: {exc}") from exc
        if bill_type not in DEFAULT_BILL_TYPES:
            raise typer.BadParameter(
                f"unknown bill type in {token!r}: {bill_type}. Valid: "
                + ", ".join(DEFAULT_BILL_TYPES)
            )
        out.append((congress, bill_type, bill_number))
    if not out:
        raise typer.BadParameter(f"no values parsed from --bill-ids {raw!r}")
    return out


def _parse_sections(raw: str) -> list[str]:
    """Parse ``--sections cosponsors,actions`` into a validated list."""
    from .scraper.bills import BILL_ENRICHMENT_SECTIONS

    parsed = _parse_csv(raw, name="sections", coerce=str.lower)
    unknown = [s for s in parsed if s not in BILL_ENRICHMENT_SECTIONS]
    if unknown:
        raise typer.BadParameter(
            f"unknown section(s): {', '.join(unknown)}. "
            f"Valid: {', '.join(BILL_ENRICHMENT_SECTIONS)}"
        )
    return parsed


@scrape_bills_app.callback(invoke_without_command=True)
def scrape_bills_command(
    ctx: typer.Context,
    congresses: Annotated[
        str,
        typer.Option(
            "--congresses",
            help="Comma-separated list of congress numbers to scrape.",
        ),
    ] = ",".join(str(c) for c in DEFAULT_BILL_CONGRESSES),
    bill_types: Annotated[
        str,
        typer.Option(
            "--bill-types",
            help="Comma-separated list of Bill type codes (hr, s, hjres, …).",
        ),
    ] = ",".join(DEFAULT_BILL_TYPES),
    storage_dir: Annotated[
        Path,
        typer.Option(
            "--storage-dir",
            help="Directory holding bills.jsonl (and Phase 2b's sibling files).",
        ),
    ] = DEFAULT_BILLS_STORAGE_DIR,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            help="Maximum number of detail snapshots to write across all pairs.",
        ),
    ] = None,
    show_progress: Annotated[
        bool,
        typer.Option(
            "--progress/--no-progress",
            help="Print a stderr line per (congress, bill_type) pair.",
        ),
    ] = True,
) -> None:
    """Snapshot Bill identity records into ``<storage-dir>/bills.jsonl``.

    For each ``(congress, bill_type)`` pair the list endpoint is walked
    to discover Bill numbers, then the detail endpoint is fetched per
    bill and one ADR 0006 envelope is appended per detail response.
    Re-running appends new snapshots; the Stage 1 loader projects the
    latest per key into SQLite.
    """
    # When the user invoked a sub-command (e.g. `scrape bills enrich`),
    # let the sub-command run instead of the basic scrape.
    if ctx.invoked_subcommand is not None:
        return
    parsed_congresses = _parse_congresses(congresses)
    parsed_types = _parse_bill_types(bill_types)
    _run_scrape_bills(
        congresses=parsed_congresses,
        bill_types=parsed_types,
        storage_dir=storage_dir,
        limit=limit,
        show_progress=show_progress,
    )


@scrape_bills_app.command("enrich")
def scrape_bills_enrich_command(
    bill_ids: Annotated[
        str | None,
        typer.Option(
            "--bill-ids",
            help=(
                "Comma-separated bill IDs like '119-hr-1,119-hr-22'. "
                "Required when --db is not provided."
            ),
        ),
    ] = None,
    sections: Annotated[
        str,
        typer.Option(
            "--sections",
            help="Comma-separated sub-endpoint sections to fetch.",
        ),
    ] = ",".join(DEFAULT_BILL_SECTIONS),
    storage_dir: Annotated[
        Path,
        typer.Option(
            "--storage-dir",
            help="Directory holding the bill_<section>.jsonl files.",
        ),
    ] = DEFAULT_BILLS_STORAGE_DIR,
    db_path: Annotated[
        Path | None,
        typer.Option(
            "--db",
            help=(
                "SQLite DB used to auto-select un-enriched bills when --bill-ids is not provided."
            ),
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            help=(
                "Cap on bills enriched. Defaults to 25 in --db auto-select mode; "
                "applied to --bill-ids count when given."
            ),
        ),
    ] = DEFAULT_ENRICH_AUTO_LIMIT,
    show_progress: Annotated[
        bool,
        typer.Option(
            "--progress/--no-progress",
            help="Print a stderr line per bill enriched.",
        ),
    ] = True,
) -> None:
    """Fetch tier-2 sub-endpoints for selected Bills.

    Either pass ``--bill-ids`` (explicit selection) or ``--db`` (auto-
    selects bills with ``cosponsors_fetched_at IS NULL`` ordered by
    ``introduced_date DESC``, capped by ``--limit``).
    """
    parsed_sections = _parse_sections(sections)
    if bill_ids is None and db_path is None:
        typer.echo(
            "error: provide --bill-ids or --db (or both); neither was set",
            err=True,
        )
        raise typer.Exit(code=2)

    if bill_ids is not None:
        keys = _parse_bill_ids(bill_ids)
    elif db_path is not None:
        keys = _autoselect_unenriched_bills(db_path, limit=limit)
        if not keys:
            typer.echo(f"No un-enriched bills found in {db_path}; nothing to do.")
            return
    else:
        # Unreachable: the earlier guard already exited with code 2.
        return

    _run_scrape_bills_enrich(
        bill_keys=keys,
        sections=parsed_sections,
        storage_dir=storage_dir,
        limit=limit,
        show_progress=show_progress,
    )


@load_app.command("bills")
def load_bills_command(
    storage_dir: Annotated[
        Path,
        typer.Option(
            "--storage-dir",
            help="Directory holding bills.jsonl (from `concord scrape bills`).",
        ),
    ] = DEFAULT_BILLS_STORAGE_DIR,
    db_path: Annotated[
        Path,
        typer.Option("--db", help="SQLite database. Created if missing."),
    ] = DEFAULT_DB,
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="Maximum number of bills to UPSERT."),
    ] = None,
) -> None:
    """Project the latest Bill snapshot per key into SQLite.

    Populates the ``bills`` table. Re-running is safe: an UPSERT on
    ``bill_id`` keeps the row consistent with the JSONL's latest
    snapshot per ``(congress, bill_type, bill_number)``.
    """
    _run_load_bills(storage_dir=storage_dir, db_path=db_path, limit=limit)


@index_app.command("bills")
def index_bills_command(
    db_path: Annotated[
        Path,
        typer.Option("--db", help="SQLite database written by `concord load bills`."),
    ] = DEFAULT_DB,
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="Cap on rows written to bills_fts."),
    ] = None,
) -> None:
    """Populate the ``bills_fts`` FTS5 virtual table.

    Truncates and repopulates ``bills_fts`` from the current ``bills``
    table. Idempotent.
    """
    _run_index_bills(db_path=db_path, limit=limit)


@run_app.command("bills")
def run_bills_command(
    congresses: Annotated[
        str,
        typer.Option(
            "--congresses",
            help="Comma-separated list of congress numbers to scrape.",
        ),
    ] = ",".join(str(c) for c in DEFAULT_BILL_CONGRESSES),
    bill_types: Annotated[
        str,
        typer.Option(
            "--bill-types",
            help="Comma-separated list of Bill type codes.",
        ),
    ] = ",".join(DEFAULT_BILL_TYPES),
    storage_dir: Annotated[
        Path,
        typer.Option("--storage-dir", help="JSONL canonical store directory."),
    ] = DEFAULT_BILLS_STORAGE_DIR,
    db_path: Annotated[
        Path,
        typer.Option("--db", help="SQLite derived store. Created if missing."),
    ] = DEFAULT_DB,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            help="Cap on new bills scraped. Load and index run unbounded.",
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
    """Run all three stages for Bills: scrape → load → index."""
    if not os.environ.get(ENV_API_KEY):
        typer.echo(
            f"error: {ENV_API_KEY} is not set; required for `concord scrape bills`",
            err=True,
        )
        raise typer.Exit(code=2)

    parsed_congresses = _parse_congresses(congresses)
    parsed_types = _parse_bill_types(bill_types)

    typer.echo("→ Stage 0: scrape", err=True)
    _run_scrape_bills(
        congresses=parsed_congresses,
        bill_types=parsed_types,
        storage_dir=storage_dir,
        limit=limit,
        show_progress=show_progress,
    )

    typer.echo("→ Stage 1: load", err=True)
    _run_load_bills(storage_dir=storage_dir, db_path=db_path, limit=None)

    typer.echo("→ Stage 2: index", err=True)
    _run_index_bills(db_path=db_path, limit=None)

    typer.echo("✓ Done.", err=True)


# ---------------------------------------------------------------------------
# Subcommands — Votes (Phase 3a)
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


def _run_scrape_votes(
    *,
    congresses: list[int],
    sessions: list[int],
    chambers: list[str],
    storage_dir: Path,
    limit: int | None,
    show_progress: bool,
) -> int:
    from .api import ApiError
    from .scraper import votes as votes_scraper

    if "senate" in chambers:
        typer.echo("Senate ingest lands in Phase 3b — skipping.")
    if "house" not in chambers:
        return 0

    try:
        api_client = Client()
    except ApiError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    progress = Progress(enabled=show_progress)

    def _on_progress(event: votes_scraper.ScrapeProgressEvent) -> None:
        progress.update(
            f"  {event.chamber} {event.congress}/{event.session}  "
            f"+{event.votes_written:>4} written  "
            f"({event.votes_seen} seen)"
        )

    try:
        with api_client:
            stats = votes_scraper.scrape_house(
                client=api_client,
                congresses=congresses,
                storage_dir=storage_dir,
                fetched_at=datetime.now(UTC),
                sessions=tuple(sessions),
                limit=limit,
                progress=_on_progress if show_progress else None,
            )
    finally:
        progress.commit()

    typer.echo(
        f"Wrote {stats.votes_written} vote detail snapshot(s) and "
        f"{stats.positions_written} member-positions snapshot(s) to {storage_dir}."
    )
    return stats.votes_written


def _run_load_votes(
    *,
    storage_dir: Path,
    db_path: Path,
    limit: int | None,
) -> int:
    from .pipeline.load_votes import load as load_votes
    from .scraper.votes import HOUSE_VOTES_JSONL_NAME

    jsonl_path = storage_dir / HOUSE_VOTES_JSONL_NAME
    if not jsonl_path.exists():
        typer.echo(
            f"No input file at {jsonl_path} — run `concord scrape votes` first. Nothing to load."
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

    from .pipeline.index_votes import index as index_votes

    stats = index_votes(db_path=db_path, limit=limit)
    typer.echo(
        f"Flagged {stats.votes_flagged_party_unity} party-unity vote(s); "
        f"scored {stats.members_scored} member-congress row(s) into member_party_unity."
    )
    return stats.members_scored


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
            help="Comma-separated chambers. Phase 3a only handles 'house'.",
        ),
    ] = ",".join(DEFAULT_VOTE_CHAMBERS),
    storage_dir: Annotated[
        Path,
        typer.Option(
            "--storage-dir",
            help="Directory holding house_votes.jsonl + house_vote_positions.jsonl.",
        ),
    ] = DEFAULT_VOTES_STORAGE_DIR,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            help="Maximum number of detail snapshots to write (members fetches mirror).",
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
    """Snapshot House roll-call votes into ``<storage-dir>/house_votes*.jsonl``."""
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
    if not os.environ.get(ENV_API_KEY):
        typer.echo(
            f"error: {ENV_API_KEY} is not set; required for `concord scrape votes`",
            err=True,
        )
        raise typer.Exit(code=2)

    parsed_congresses = _parse_congresses(congresses)
    parsed_sessions = _parse_sessions(sessions)
    parsed_chambers = _parse_chambers(chambers)

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
