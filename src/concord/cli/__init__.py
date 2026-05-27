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

The same shape applies to Members, Bills, and Votes.

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

import typer

from ._apps import index_app, load_app, run_app, scrape_app

# Re-exports consumed by tests and external callers.
from ._common import DEFAULT_DB, ENV_OPENAI_API_KEY, Progress  # noqa: F401
from ..storage import MongoStorage  # noqa: F401 — patched by tests

# ---------------------------------------------------------------------------
# Root app
# ---------------------------------------------------------------------------

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    # Plain Python tracebacks; Rich's pretty formatter is verbose and harder
    # to read for unfamiliar code paths.
    pretty_exceptions_enable=False,
    help="Concord — collect, index, and search the Congressional Record.",
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
# Register entity subcommands (side-effectful imports)
# ---------------------------------------------------------------------------

# Each module decorates its commands onto the stage apps imported from _apps.
from . import bills, members, proceedings, votes  # noqa: E402, F401

# serve lives directly on the root app, not under a stage sub-app.
from .serve import serve_command  # noqa: E402

app.command("serve")(serve_command)

# Re-export command functions that appear in the legacy __all__ so any
# existing code doing `from concord.cli import scrape_proceedings_command`
# keeps working.
from .proceedings import (  # noqa: E402, F401
    DEFAULT_JSONL,
    index_proceedings_command,
    load_proceedings_command,
    run_proceedings_command,
    scrape_proceedings_command,
)
from .members import DEFAULT_MEMBERS_JSONL  # noqa: E402, F401

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:  # pragma: no cover - entry point shim
    app()


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "DEFAULT_DB",
    "DEFAULT_JSONL",
    "DEFAULT_MEMBERS_JSONL",
    "ENV_OPENAI_API_KEY",
    "MongoStorage",
    "Progress",
    "app",
    "index_proceedings_command",
    "load_proceedings_command",
    "main",
    "run_proceedings_command",
    "scrape_proceedings_command",
    "serve_command",
]
