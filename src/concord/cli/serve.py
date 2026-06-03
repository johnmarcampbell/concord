"""CLI command for the web server (``concord serve``)."""

from pathlib import Path
from typing import Annotated

import typer

from concord.cli._common import DEFAULT_DB, _require_openai_key
from concord.observability import configure_logging


def serve_command(
    db_path: Annotated[
        Path,
        typer.Option("--db", help="SQLite database (from `concord load` + `concord index`)."),
    ] = DEFAULT_DB,
    storage_dir: Annotated[
        Path | None,
        typer.Option(
            "--storage-dir",
            help=(
                "JSONL canonical store directory. Only consulted by the web-initiated "
                "enrichment flow (ADR 0016). Defaults to the parent of --db."
            ),
        ),
    ] = None,
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

    A missing DB file is created with an empty schema on startup (ADR 0012);
    `serve` is the only top-level command that bootstraps rather than fails.
    """
    # Run_id-stamped logging for the long-running server, installed before
    # the app boots (ADR 0021). Idempotent with the CLI callback's install.
    configure_logging()
    _require_openai_key()

    # Lazy imports so `concord --help` doesn't pay the FastAPI/uvicorn cost.
    import uvicorn

    from concord.web.app import create_app

    app_instance = create_app(db_path, storage_dir=storage_dir)
    uvicorn.run(app_instance, host=host, port=port, reload=reload)
