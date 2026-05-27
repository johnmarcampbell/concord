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
- **Absolute imports only.** Use `from concord.foo import bar`, not `from .foo import bar`. Ruff will autofix this for you.
- **Type-hint everything in `src/`.** `mypy --strict` runs in CI on `src/concord/` (tests are looser).
- **No magic numbers in `src/`.** Pull them into a module-level `_FOO = N` constant. HTTP status codes are in `concord.api.HTTP_*`. Tests are exempt.
- **`pytest.mark.parametrize` takes a tuple of names**, not a comma-separated string: `("url", "expected")`, not `"url,expected"`.
- **One assertion per `assert`.** Split `assert a and b` into two lines so failure messages identify the culprit.
- **Use `pytest.raises(...)`**, not try/except/else with assertions on the exception.
- **`# noqa` requires an inline reason.** Format: `# noqa: RULE — short reason`. A bare `# noqa` will be flagged in review.
- **Reach for per-file-ignores in `pyproject.toml` over scattering noqas** when a rule consistently misfires on a whole module (e.g., the storage layer's intentional SQL-string construction).

## Linter philosophy

The ruff config in [pyproject.toml](pyproject.toml) is intentionally broad — it pulls in most pylint checks (`PL`), security (`S`), complexity (`C90`), naming (`N`), pytest style (`PT`), and more. The goal isn't perfectionism: it's to catch *unintentional* drift early, so the codebase stays uniform without anyone having to remember the rules.

When a rule fires on code that's genuinely right as written — a FastAPI route handler that has eleven query params, a pipeline orchestrator that's long *because* it orchestrates — prefer an inline `# noqa: RULE — reason` over rewriting the code to dodge the linter. The reason text matters: it's the documentation a future reader needs to understand why the rule was waived. If the same rule keeps misfiring across many files in one module, prefer a `per-file-ignores` entry in `pyproject.toml` with a comment explaining the pattern.
