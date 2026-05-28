"""Enrich every Bill in the curated /bills landing list.

Mirrors the web-button flow from ADR 0016 (scrape_enrichment → load_one →
reindex_one) for the full :data:`CURATED_TOP_BILLS` list in one batch.
Bills missing from the local SQLite store are fetched first via the Bill
detail endpoint, appended to ``bills.jsonl``, and loaded so the parent
row exists before enrichment attaches tier-2 data (ADR 0009).

Requires ``CONGRESS_API_KEY``.

    uv run python scripts/enrich_top_bills.py
    uv run python scripts/enrich_top_bills.py --db data/proceedings.db
"""

import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from concord.api import Client
from concord.cli._common import DEFAULT_DB
from concord.pipeline import index_bills, load_bills
from concord.scraper.bills import BILLS_JSONL_NAME, scrape_enrichment
from concord.web.top_bills import CURATED_TOP_BILLS


def _bill_row_exists(db_path: Path, bill_id: str) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("SELECT 1 FROM bills WHERE bill_id = ? LIMIT 1", (bill_id,))
        return cur.fetchone() is not None
    finally:
        conn.close()


def _scrape_bill_identity(
    *,
    client: Client,
    storage_dir: Path,
    congress: int,
    bill_type: str,
    bill_number: int,
    iso: str,
) -> None:
    """Fetch one Bill's detail record and append the ADR 0006 envelope.

    Cheaper than :func:`scrape_basic`, which paginates whole
    ``(congress, bill_type)`` slots. Used when a curated entry isn't in
    the local store yet — we only need that one identity record so the
    parent ``bills`` row exists before enrichment.
    """
    storage_dir.mkdir(parents=True, exist_ok=True)
    detail = client.get_bill_detail(congress, bill_type, bill_number)
    envelope = {
        "fetched_at": iso,
        "key": {"congress": congress, "bill_type": bill_type, "bill_number": bill_number},
        "payload": detail,
    }
    with (storage_dir / BILLS_JSONL_NAME).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(envelope, ensure_ascii=False))
        fh.write("\n")


def main(
    db_path: Annotated[
        Path,
        typer.Option("--db", help="SQLite database path."),
    ] = DEFAULT_DB,
    storage_dir: Annotated[
        Path | None,
        typer.Option(
            "--storage-dir",
            help="JSONL canonical store directory. Defaults to the parent of --db.",
        ),
    ] = None,
) -> None:
    """Run enrichment for every curated top bill."""
    store = storage_dir if storage_dir is not None else db_path.parent
    iso = datetime.now(UTC).isoformat()

    keys: list[tuple[int, str, int]] = []
    with Client() as client:
        for entry in CURATED_TOP_BILLS:
            key = (entry.congress, entry.bill_type, entry.bill_number)
            bill_id = f"{entry.congress}-{entry.bill_type}-{entry.bill_number}"
            if not _bill_row_exists(db_path, bill_id):
                typer.echo(f"fetching identity for {bill_id} ({entry.label})…")
                try:
                    _scrape_bill_identity(
                        client=client,
                        storage_dir=store,
                        congress=entry.congress,
                        bill_type=entry.bill_type,
                        bill_number=entry.bill_number,
                        iso=iso,
                    )
                except Exception as exc:
                    typer.echo(f"  skip {bill_id}: identity fetch failed: {exc}", err=True)
                    continue
                load_bills.load_one(storage_dir=store, db_path=db_path, bill_id=bill_id)
            keys.append(key)

        if not keys:
            typer.echo("no curated bills available to enrich", err=True)
            raise typer.Exit(code=1)

        typer.echo(f"enriching {len(keys)} bill(s)…")
        stats = scrape_enrichment(
            client=client,
            bill_keys=keys,
            storage_dir=store,
            fetched_at=datetime.now(UTC),
        )

    for congress, bill_type, bill_number in keys:
        bill_id = f"{congress}-{bill_type}-{bill_number}"
        load_bills.load_one(storage_dir=store, db_path=db_path, bill_id=bill_id)
        index_bills.reindex_one(db_path=db_path, bill_id=bill_id)
        typer.echo(f"  done: {bill_id}")

    typer.echo(
        f"enriched {stats.bills_enriched} bill(s); "
        f"{stats.snapshots_written} snapshot(s) written; "
        f"{stats.section_failures} section failure(s)"
    )
    if stats.section_failures > 0:
        sys.exit(2)


if __name__ == "__main__":
    typer.run(main)
