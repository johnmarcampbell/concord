# Contributing

## Setup

```sh
uv sync
uv run pre-commit install   # one-time per clone
```

The pre-commit hook runs `ruff format` and `ruff check --fix` on every `git commit`, so most style issues land fixed before they reach review.

## Style highlights

- **Line length: 100.** Enforced by `ruff format` (black-compatible).
- **Top-level imports.** No lazy imports inside functions unless there is a *real* circular-dependency or optional-dependency reason. If there is, add `# noqa: PLC0415 — <one-phrase reason>`.
- **No `from __future__ import annotations`** unless a module *genuinely* needs it (a real forward reference that can't be resolved, or a circular import that has no other fix). The project is Python 3.12+ — modern union syntax (`int | None`) and built-in generics work at runtime without the import. The import also turns every annotation into a string, which is a footgun for the Pydantic and FastAPI code that introspects annotations at runtime. Don't reach for it as a reflex.
- **Absolute, submodule-direct imports only** (ADR 0022). Import each name from the submodule that *defines* it — `from concord.models.votes import Vote`, not `from .votes import Vote` (relative) and not `from concord.models import Vote` (a package re-export façade). Package `__init__.py` files carry only a docstring, package metadata (`concord.__version__`), or a composition root (the `concord.cli` Typer app) — never a flat namespace re-export. Importing a submodule *itself* through its package (`from concord.web import app`) is fine. Ruff enforces the absolute half (`TID252`, autofixable); the bare `__init__` files enforce the rest by making façade imports fail.
- **Type-hint everything in `src/`.** `mypy --strict` runs in CI on `src/concord/` (tests are looser).
- **No magic numbers in `src/`.** Pull them into a module-level `_FOO = N` constant. HTTP status codes are in `concord.api.HTTP_*`. Tests are exempt.
- **`pytest.mark.parametrize` takes a tuple of names**, not a comma-separated string: `("url", "expected")`, not `"url,expected"`.
- **One assertion per `assert`.** Split `assert a and b` into two lines so failure messages identify the culprit.
- **Use `pytest.raises(...)`**, not try/except/else with assertions on the exception.
- **`# noqa` requires an inline reason.** Format: `# noqa: RULE — short reason`. A bare `# noqa` will be flagged in review.
- **Reach for per-file-ignores in `pyproject.toml` over scattering noqas** when a rule consistently misfires on a whole module (e.g., the storage layer's intentional SQL-string construction).

## Pydantic first

Pydantic models are the default tool for data crossing an application boundary — **reach for one before a bare `dict` or hand-rolled parsing.** A *boundary* is anywhere untyped or untrusted data enters or leaves the program: the API / XML / HTML wire, the JSONL load step, the SQLite read path, the LLM JSON exchange, CLI and web-form input. The validated model is the default; a `dict[str, Any]` threaded through application code is what needs a reason.

The project already has the vocabulary and the factories — use them rather than re-deriving ad-hoc parsing:

- **Wire-shape model** with a `from_<source>(payload)` classmethod for ingest ([ADR 0018](docs/adr/0018-pydantic-at-the-load-boundary.md) settles where validation runs, naming, and the factory shape).
- **Domain model** for what the app operates on after normalization.
- **Read-view model** with a `from_row` / `from_sql` factory for the read/display path (`BillHit`, `VoteHit`, … in `web/search.py`). Let one factory own all the reading for an aggregate instead of fetching its parts into loose dicts at each call site.

**Models own their crossing in both directions.** Read *in* with a `from_<source>(...)` classmethod — `from_congress_api`, `from_senate_xml`, `from_row` / `from_sql`. Write *out* with a `to_<destination>(...)` method — Pydantic's `model_dump_json` / `model_dump` for JSON/JSONL, or a named `to_<sink>()` for a bespoke shape. Parsing, validation, and serialization for a representation live **on the model that owns it**, so a caller crosses the boundary by calling the model's factory or serializer rather than hand-rolling the dict-shuffling. Keep each model aligned with its boundary (a wire-shape model mirrors the wire per ADR 0018; a read-view / persistence model owns the store) — don't push a representation onto a model that shouldn't know it.

Exceptions are fine — a single-row internal helper, or a genuinely hot loop where a model is measurable overhead — but they're *exceptions*: model first, and if you skip it, leave a one-line reason, the same discipline as a `# noqa`.

## Linter philosophy

The ruff config in [pyproject.toml](pyproject.toml) is intentionally broad — it pulls in most pylint checks (`PL`), security (`S`), complexity (`C90`), naming (`N`), pytest style (`PT`), and more. The goal isn't perfectionism: it's to catch *unintentional* drift early, so the codebase stays uniform without anyone having to remember the rules.

When a rule fires on code that's genuinely right as written — a FastAPI route handler that has eleven query params, a pipeline orchestrator that's long *because* it orchestrates — prefer an inline `# noqa: RULE — reason` over rewriting the code to dodge the linter. The reason text matters: it's the documentation a future reader needs to understand why the rule was waived. If the same rule keeps misfiring across many files in one module, prefer a `per-file-ignores` entry in `pyproject.toml` with a comment explaining the pattern.
