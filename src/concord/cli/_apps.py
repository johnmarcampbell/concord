"""Stage-level Typer app instances.

Kept in their own module so entity sub-modules can import them to register
commands without creating a circular dependency with ``cli/__init__.py``.
"""

import typer

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
