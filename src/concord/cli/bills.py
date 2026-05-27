"""CLI commands for the Bills entity (scrape / scrape enrich / load / index / run)."""

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from concord.api import ENV_API_KEY, ApiError, Client
from concord.pipeline.index_bills import index as index_bills
from concord.pipeline.load_bills import load as load_bills
from concord.scraper import bills as bills_scraper
from concord.scraper.bills import BILL_ENRICHMENT_SECTIONS, BILLS_JSONL_NAME

from ._apps import index_app, load_app, run_app, scrape_app
from ._common import DEFAULT_DB, Progress, RateTracker, _parse_congresses, _parse_csv

DEFAULT_BILLS_STORAGE_DIR = Path("./data")

#: Congresses scraped by ``concord scrape bills`` when ``--congresses``
#: is not passed. Matches the Phase 2 roadmap scope.
DEFAULT_BILL_CONGRESSES = (117, 118, 119)

#: All eight legislative Bill type codes. Used both as the
#: ``concord scrape bills --bill-types`` default and as the validator
#: for the ``/bills/{...}/{bill_type}/...`` URL segment.
DEFAULT_BILL_TYPES = ("hr", "hres", "hjres", "hconres", "s", "sres", "sjres", "sconres")

#: Tier-2 sub-endpoint names that ``concord scrape bills enrich`` defaults to.
DEFAULT_BILL_SECTIONS = ("cosponsors", "actions", "subjects", "titles", "summaries")

#: Default cap on the auto-selected enrichment batch when --bill-ids is
#: not given. Operators tweak via ``--limit N``.
DEFAULT_ENRICH_AUTO_LIMIT = 25

#: Expected number of dash-separated parts in a ``--bill-ids`` token.
_BILL_ID_PART_COUNT = 3


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


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
    parsed = _parse_csv(raw, name="sections", coerce=str.lower)
    unknown = [s for s in parsed if s not in BILL_ENRICHMENT_SECTIONS]
    if unknown:
        raise typer.BadParameter(
            f"unknown section(s): {', '.join(unknown)}. "
            f"Valid: {', '.join(BILL_ENRICHMENT_SECTIONS)}"
        )
    return parsed


def _autoselect_unenriched_bills(
    db_path: Path,
    *,
    limit: int,
) -> list[tuple[int, str, int]]:
    """Return ``[(congress, bill_type, bill_number)]`` for un-enriched bills, newest first."""
    if not db_path.exists():
        typer.echo(f"error: database not found: {db_path}", err=True)
        raise typer.Exit(code=2)

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


# ---------------------------------------------------------------------------
# Stage workers
# ---------------------------------------------------------------------------


def _run_scrape_bills(
    *,
    congresses: list[int],
    bill_types: list[str],
    storage_dir: Path,
    limit: int | None,
    show_progress: bool,
) -> int:
    try:
        api_client = Client()
    except ApiError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    progress = Progress(enabled=show_progress)
    tracker = RateTracker()

    def _on_progress(event: bills_scraper.ScrapeProgressEvent) -> None:
        if not progress.interactive and not event.is_pair_done:
            return
        tracker.update_total(event.category_total)
        label = f"  congress {event.congress:>3}  {event.bill_type:<7}  "
        if event.is_pair_done:
            progress.update(label + tracker.finish(event.bills_written))
            progress.commit()
            tracker.reset()
        else:
            progress.update(label + tracker.tick(event.bills_written))

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
        f"{storage_dir / BILLS_JSONL_NAME} "
        f"across {len(congresses)} congress(es) x {len(bill_types)} bill type(s)."
    )
    return stats.bills_written


def _run_scrape_bills_enrich(
    *,
    bill_keys: list[tuple[int, str, int]],
    sections: list[str],
    storage_dir: Path,
    limit: int | None,
    show_progress: bool,
) -> int:
    try:
        api_client = Client()
    except ApiError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    progress = Progress(enabled=show_progress)
    tracker = RateTracker(total=len(bill_keys))
    section_failures = 0

    def _on_progress(event: bills_scraper.EnrichProgressEvent) -> None:
        nonlocal section_failures
        section_failures += len(event.partial_failures)
        suffix = f"   ({section_failures} section error(s))" if section_failures else ""
        progress.update("  " + tracker.tick(event.bills_done) + suffix)

    try:
        with api_client:
            stats = bills_scraper.scrape_enrichment(
                client=api_client,
                bill_keys=bill_keys,
                storage_dir=storage_dir,
                fetched_at=datetime.now(UTC),
                sections=sections,
                limit=limit,
                bills_total=len(bill_keys),
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

    stats = index_bills(db_path=db_path, limit=limit)
    typer.echo(f"Indexed {stats.indexed_bills} bill(s) into bills_fts.")
    return stats.indexed_bills


# ---------------------------------------------------------------------------
# Subcommands — scrape bills (with nested `enrich`)
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


# ---------------------------------------------------------------------------
# Subcommands — load / index / run bills
# ---------------------------------------------------------------------------


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
