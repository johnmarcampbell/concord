# Internal import convention: submodule-direct, no façades, absolute-only

> Establish and ruff-enforce one codebase-wide rule for internal imports — import names from the submodule that defines them (no parent-package re-export façades), keep `from pkg import submodule`, and use absolute paths everywhere — then sweep the codebase to match and record it in a new ADR.

## Source

- GitHub issue: [#123 — Untangle concord.models from the senate_xml client (layering + façade follow-up)](https://github.com/johnmarcampbell/concord/issues/123), smell #2 ("eager-loading façade") generalised to a whole-codebase convention.
- This is **plan 2 of 2** for issue #123. It is **blocked by / sequenced after** plan 1, [untangle-models-senate-xml.md](untangle-models-senate-xml.md).

## Context

`concord/models/__init__.py` is a flat aggregator that re-exports every submodule's symbols, so importing the package eagerly loads all six submodules. That eager load is what amplified issue #123's layering inversion into a hard import cycle (see plan 1). The convention is also applied inconsistently today: **44** sites import the flat `concord.models` façade while **7** files already reach into submodules directly.

The decision (reached by grilling against ADR 0014 and ADR 0018) is to make **submodule-direct imports the single house standard** and delete the re-export façades, rather than preserve them. Rationale specific to this repo:

- **ADR 0014** makes the CLI the stable contract and treats Python imports as best-effort — there is no external-API obligation a flat namespace protects. The façades earn nothing internally.
- **AI-navigability** is a project value; absolute, submodule-direct imports are self-locating (a reader of one file knows exactly what `concord.storage.sqlite.SqliteStorage` is without resolving the file's position).
- **Module decomposition is the active churn mode** (recent commits split `sqlite.py` per-domain, unified the route seam). Relative imports are fragile under exactly those moves; absolute imports break loudly and get tool-updated.

Two empirical findings reinforce the direction: the `web` and `cli` package re-export surfaces are **already dead** — `from concord.web import create_app` has 0 users (every caller uses `concord.web.app`), and `from concord.cli import …` has 0 users at all. Flat façades here rot into unused indirection.

A definitional distinction settled during grilling, used throughout this plan:

- **Pattern X — re-exported symbol:** `from concord.models import Vote`, where `Vote` is *defined in a submodule and re-exported by the package `__init__`*. **Banned.** Deleting the façade makes this self-enforcing (the symbol simply isn't there).
- **Pattern Y — submodule object:** `from concord.web import search` / `from concord.storage import votes as votes_storage`, where `search`/`votes` are *real submodules*. **Allowed** — it doesn't depend on any `__init__` re-export and works against an empty `__init__`. Not rewritten.

## Goals

1. No parent-package `__init__.py` re-exports submodule symbols (Pattern X eliminated) — except the two defensible exceptions below.
2. Every consumer imports names from the submodule that defines them; Pattern Y (`from pkg import submodule`) is left as-is.
3. All internal imports are absolute. Ruff enforces this via `flake8-tidy-imports.ban-relative-imports = "all"`, so the rule can't silently re-drift.
4. A new ADR records the convention so future modules follow it without re-litigation.
5. `ruff` / `mypy src` / `pytest` green; `tests/test_import_cycles.py` still passes.

## Non-goals

1. **Touching the two defensible exceptions:** `concord/__init__.py`'s `__version__` (package metadata, blessed by ADR 0014) and `concord/cli/__init__.py`'s **composition root** (the `typer.Typer` app wiring + the side-effectful `from . import bills, members, proceedings, votes` command registration). Those are not namespace façades. The composition root's *relative* imports do still get converted to absolute (goal 3); its *structure* stays.
2. **Normalising Pattern Y to `import pkg.sub as sub`.** Allow-Y is the decided rule; `from concord.storage import votes as votes_storage` stays.
3. **Adding a `# noqa: TID252` carve-out** for the cli composition root's sibling import. Decided against (Option A / "pure"): let `from . import bills` autofix to `from concord.cli import bills`. A style exception would erode the "one standard" this plan exists to create.
4. **The `SenateXmlError` layering fix** — that's plan 1.
5. **Any behaviour change.** Imports only.

## Relevant prior decisions

- [ADR 0014 — Publish to PyPI as `congress-concord`, CLI-first](../adr/0014-publish-to-pypi-cli-first.md). The CLI is the stable contract; Python imports are explicitly *not* covered by semver. This is the licence to delete the façades and churn import paths freely.
- [ADR 0018 — Pydantic validation at the load boundary](../adr/0018-pydantic-at-the-load-boundary.md). Defines the model symbols being re-pointed; the new ADR cites it for the model/error ownership context.
- **ADR 0022 — Internal import convention** *(new, created with this plan — see step 5)*. Next free number after [0021 — Scrape-run observability](../adr/0021-scrape-run-observability.md).
- [CONTRIBUTING.md:17](../../CONTRIBUTING.md) already states "Absolute imports only" — this plan makes the linter actually enforce it.

## Relevant files and code

**Façade `__init__.py` files to strip (keep the module docstring, delete re-exports + `__all__`):**
- `src/concord/models/__init__.py` — re-exports ~30 symbols across 6 submodules; **44 consumer sites**.
- `src/concord/storage/__init__.py:17-20` — re-exports `Storage`/`JsonlStorage`/`SqliteStorage`/`ensure_schema`; **8 consumer sites**.
- `src/concord/web/__init__.py:8-10` — re-exports `create_app`; **0 consumers** (free).
- `src/concord/cli/__init__.py:37-45,76-108` — the dead `__all__` block + the imports that only feed it; **0 consumers** (free). The composition root in the same file stays.

**Files to keep untouched (exceptions):**
- `src/concord/__init__.py` — `__version__` via `importlib.metadata`. Keep.
- `src/concord/cli/__init__.py` composition root — `typer.Typer(...)`, `app.add_typer(...)`, `_root()`, `app.command("serve")(...)`, `main()`, and `from . import bills, members, proceedings, votes  # noqa: F401`. Keep (relative imports here still convert to absolute).

**Config:**
- `pyproject.toml` — ruff `[tool.ruff.lint.flake8-tidy-imports]` section (add `ban-relative-imports = "all"`; `TID` is already in `select`).

**Symbol → defining-submodule map for the `concord.models` rewrite** (derived from the current `models/__init__.py`):

| Submodule | Symbols |
|---|---|
| `concord.models._common` | `Chamber`, `SessionNumber`, `Snapshot` |
| `concord.models.bills` | `BillAction`, `BillCosponsor`, `BillDetail`, `BillSubject`, `BillSummary`, `BillTitle`, `bill_id_from_components` |
| `concord.models.members` | `Member`, `Term`, `normalize_state` |
| `concord.models.proceedings` | `Article`, `Issue`, `Proceeding`, `parse_granule_id` |
| `concord.models.runs` | `Attempt`, `RunEvent`, `RunRecord` |
| `concord.models.votes` | `HouseVoteMembers`, `SenateVoteDetail`, `SenateVotePosition`, `Vote`, `VoteKind`, `VotePosition`, `VoteThreshold`, `amendment_id_from_components`, `parse_vote_threshold`, `vote_id_from_components` |

**Symbol → defining-submodule map for the `concord.storage` rewrite:**

| Submodule | Symbols |
|---|---|
| `concord.storage.base` | `Storage` |
| `concord.storage.jsonl` | `JsonlStorage` |
| `concord.storage.sqlite` | `SqliteStorage`, `ensure_schema` |

**Storage Pattern-X consumer sites (8) to rewrite:** `src/concord/cli/proceedings.py:22`; `tests/test_web_routes.py:17`, `tests/test_indexing.py:10`, `tests/test_web_search.py:12`, `tests/test_storage_jsonl.py:7`, `tests/test_storage_sqlite.py:10`, `tests/test_cli.py:31`, `tests/test_pipeline.py:21`. (The 4 `from concord.storage import <submodule> as …` lines in `src/concord/storage/sqlite.py:49-52` are **Pattern Y — leave them**.)

**Relative-import sites (autofix targets, step 4).** 21 total; 4 vanish when the storage/web façades are stripped (`storage/__init__.py:17-19`, `web/__init__.py:8`). The 2 `from .models import …` lines below are *both* relative *and* Pattern X, so they are hand-rewritten to the defining submodule in step 1, **not** left to autofix (ruff would only strip the dot to `from concord.models import …`, which breaks once the façade is gone):
- `src/concord/api.py:34` — `from .models import Article, Attempt, Issue` → `from concord.models.proceedings import Article, Issue` + `from concord.models.runs import Attempt`.
- `src/concord/senate_xml.py:34` — `from .models import Attempt` → `from concord.models.runs import Attempt`.

Remaining relatives that autofix cleanly to absolute: `api.py:33,35`, `senate_xml.py:33,35`, `cli/__init__.py:35-36,46`, `cli/members.py:16-17`, `cli/proceedings.py:24-25`, `cli/votes.py:18-19`, `cli/bills.py:19-20`.

## Approach

The work is one large but mechanical sweep plus a config flip and an ADR. The only real subtlety is **ordering**: façade removal and the relative→absolute autofix interact on the two `from .models import …` lines. If you run `ruff check --fix` first, `from .models import Attempt` becomes `from concord.models import Attempt` — which then breaks the moment the façade is stripped. So:

1. **First**, strip the façades and rewrite every Pattern-X consumer to its defining submodule — including the two `from .models import …` lines, written directly to their submodules. After this step the codebase is internally consistent (no symbol depends on a façade) even though some non-façade relatives still exist.
2. **Then** flip ruff to `ban-relative-imports = "all"` and `ruff check --fix` the remaining relatives. Because every Pattern-X import already names a submodule, the autofix only has to strip dots on Pattern-Y / sibling-module imports, which is safe.

Correctness is enforced by the toolchain, not by eyeballing: after the rewrite, ruff's F401 (unused import) / F811 and mypy's name resolution will flag any symbol pointed at the wrong submodule, and pytest exercises the import paths. A `from concord.models import X` that survives the sweep will fail at import (the façade is gone), so nothing can silently keep using it.

For the `concord.models` rewrite specifically: a single `from concord.models import (A, B, C)` may fan out into up to six submodule imports. Use the symbol→submodule table above; group by submodule, alphabetise within ruff's import ordering (ruff format / isort rules will normalise). A short throwaway script keyed on the table is reasonable, but hand-editing 44 sites guided by the table and then leaning on ruff+mypy+pytest to catch slips is equally fine.

## Step-by-step plan

1. **Strip the four façades and rewrite Pattern-X consumers.**
   1. `src/concord/models/__init__.py`: delete the submodule imports (lines 31–55) and `__all__` (lines 57–88); keep the module docstring. Rewrite all **44** `concord.models` Pattern-X consumers to submodule imports using the model symbol→submodule table. This includes the two relative Pattern-X lines (`api.py:34`, `senate_xml.py:34`) written directly to `concord.models.proceedings` / `concord.models.runs`.
   2. `src/concord/storage/__init__.py`: delete the `from .base/.jsonl/.sqlite import …` lines (17–19) and `__all__` (20); keep the docstring. Rewrite the **8** storage Pattern-X consumers using the storage table. Leave the 4 Pattern-Y lines in `storage/sqlite.py:49-52`.
   3. `src/concord/web/__init__.py`: delete `from .app import create_app` (8) and `__all__` (10); keep the docstring. No consumer rewrites (all callers already use `concord.web.app`).
   4. `src/concord/cli/__init__.py`: delete the dead-`__all__`-only imports (`from ._common import …` line 37, `from .members import …` line 38, `from .proceedings import (…)` lines 39–45) and the `__all__` block (lines 76–108). **Keep** the composition root: `from . import bills, members, proceedings, votes  # noqa: F401` (35), `from ._apps import …` (36), `from .serve import serve_command` (46), `from concord.observability import configure_logging`, the `typer.Typer(...)` app, `add_typer` calls, `_root`, `app.command("serve")(...)`, and `main`.
   - Verify: `uv run python -c "import concord; import concord.models; import concord.storage; import concord.web; import concord.cli"` and `grep -rEn "from concord\.(models|storage) import [A-Z]" src tests` returns only Pattern-Y submodule names, none of the mapped symbols.

2. **Run mypy + ruff to catch mis-pointed symbols.** `uv run mypy src` and `uv run ruff check` — fix any `name-defined` / F401 fallout from step 1 (a symbol routed to the wrong submodule, or a now-unused import). Iterate until both are clean of step-1 fallout.

3. **Confirm the test suite imports resolve.** `uv run pytest -q` — failures at this point are almost certainly a consumer pointed at the wrong submodule; fix against the tables.

4. **Flip ruff to absolute-only and autofix.** In `pyproject.toml`, add:
   ```toml
   [tool.ruff.lint.flake8-tidy-imports]
   ban-relative-imports = "all"
   ```
   Then `uv run ruff check --fix`. Confirm it converts the remaining relatives (`api.py`, `senate_xml.py`, `cli/*`) to absolute, including `from . import bills, members, proceedings, votes` → `from concord.cli import bills, members, proceedings, votes` (keep its `# noqa: F401`; do **not** add a `TID252` noqa). Verify zero relative imports remain: `grep -rEn "^\s*from \.[A-Za-z_.]|^\s*from \. import" src/concord` returns nothing.

5. **Write ADR 0022.** Add `docs/adr/0022-internal-import-convention.md` (status Accepted, dated). Record: (a) import names from the defining submodule — no parent-package re-export façades (Pattern X banned); (b) `from pkg import submodule` (Pattern Y) is allowed; (c) package `__init__.py` carries only package metadata (`concord.__version__`) or a composition root (the `cli` Typer assembly), never a flat namespace; (d) absolute imports only, enforced by `ban-relative-imports = "all"`. Cite ADR 0014 (imports are not the public contract) and ADR 0018 (model symbol ownership), and note this resolves issue #123 smell #2. Add it to the ADR index if one exists.

6. **Update CONTRIBUTING.md.** [CONTRIBUTING.md:17](../../CONTRIBUTING.md) already says "absolute imports only" — extend that bullet (or add one) to state the submodule-direct / no-façade rule and point to ADR 0022, so the human-facing style guide and the ADR agree.

7. **Final gate.** `uv run ruff check && uv run ruff format --check && uv run mypy src && uv run pytest` all green; `tests/test_import_cycles.py` passes.

## Demo seed data

Not applicable — pure import refactor, no new tables, columns, entities, or API capabilities.

## Testing strategy

- **Toolchain as the test.** Correctness of the rewrite is enforced by mypy (name resolution), ruff (F401/F811 + TID252), and the existing pytest suite — a symbol pointed at the wrong submodule fails to resolve. No new behavioural tests are needed.
- **Regression — import cycles:** `tests/test_import_cycles.py` must stay green (the façade removal should, if anything, make cycles *less* likely).
- **Optional guard test:** consider a small test asserting that each package `__init__` other than the two exceptions has an empty/absent `__all__`, to prevent a new façade sprouting later. Low priority — façade removal is largely self-enforcing (a re-export nobody can import is dead on arrival), so this is belt-and-suspenders, not required.
- **Regression risk:** the broadest in the repo by file count (~52 import sites + 5 `__init__`/config files), but each change is mechanical and tool-verified. Highest-risk spots: the model symbol fan-out (wrong submodule) and the cli composition root (don't delete a wiring import that only *looks* like it only feeds `__all__` — cross-check against the "keep" list in step 1.4).

## Acceptance criteria

- [ ] `models/__init__.py`, `storage/__init__.py`, `web/__init__.py` contain only their docstrings (no re-exports, no `__all__`).
- [ ] `cli/__init__.py` retains the Typer composition root and side-effect registration but no dead `__all__` / namespace re-exports.
- [ ] `concord/__init__.py`'s `__version__` is untouched.
- [ ] `grep -rEn "from concord\.(models|storage|web|cli) import [A-Z]" src tests` shows no re-exported symbol (only Pattern-Y submodule imports remain).
- [ ] `pyproject.toml` sets `ban-relative-imports = "all"`; `grep` finds zero `from .` relative imports under `src/concord`.
- [ ] `docs/adr/0022-internal-import-convention.md` exists and is referenced from CONTRIBUTING.md.
- [ ] `uv run ruff check`, `uv run ruff format --check`, `uv run mypy src`, `uv run pytest` all green; `tests/test_import_cycles.py` passes.

## Open questions

None — ban-X/allow-Y, the two exceptions, Option A (no TID252 carve-out), and absolute-only enforcement were all resolved during grilling.

## Out-of-band work

- **Blocked by plan 1** ([untangle-models-senate-xml.md](untangle-models-senate-xml.md)). Land that PR first: it also edits `senate_xml.py` (the `SenateXmlError` import) and adds `concord/errors.py`, and its fix lets this plan's ADR 0022 describe a resolved layering rather than an open cycle. Rebase this sweep on top.
- After both land, issue #123 can be closed (smell #1 by plan 1, smell #2 by this plan).
