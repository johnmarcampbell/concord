---
status: accepted
---

# Internal import convention

## Context

Concord's packages had drifted into an inconsistent import style. `concord.models/__init__.py` was a flat aggregator that re-exported every submodule's symbols, so importing the package eagerly loaded all six submodules. ~44 call sites imported the flat `concord.models` façade while ~7 already reached into submodules directly (`from concord.models.votes import Vote`). `concord.storage`, `concord.web`, and `concord.cli` carried smaller versions of the same re-export façade — two of which (`web`, `cli`) had **zero** consumers and existed purely as dead indirection.

That eager-loading façade is what turned a latent layering smell into a hard import cycle: when [#122](https://github.com/johnmarcampbell/concord/pull/122) wired `senate_xml` into the observability ledger, `models → votes → senate_xml → observability → models` closed, because importing `concord.models` pulled in `votes`. [ADR 0021](./0021-scrape-run-observability.md) introduced those ledger edges; the façade amplified them. The layering half of that smell was fixed separately ([#124](https://github.com/johnmarcampbell/concord/pull/124), `SenateXmlError` moved to a dependency-free `concord/errors.py`); this ADR settles the import convention so the class of problem can't recur and the codebase stops being inconsistent about it.

Two prior decisions frame the choice:

- [ADR 0014](./0014-publish-to-pypi-cli-first.md) makes the **CLI** the stable, semver-tracked contract and states explicitly that **Python imports are not covered** — `from concord.storage.sqlite import SqliteStorage` may break between minor versions. So there is no external-API obligation that a flat package namespace protects; the façades earn nothing.
- [ADR 0018](./0018-pydantic-at-the-load-boundary.md) names the model symbols (`Vote`, `BillDetail`, `SenateVoteDetail`, …) and where each lives.

Relative imports were a second, related inconsistency: `CONTRIBUTING.md` already said "absolute imports only", but ruff's `flake8-tidy-imports.ban-relative-imports` was at its default `"parents"`, which permits single-dot `from .foo import bar`. ~55 single-dot relatives had accumulated (mostly across `concord.cli`).

## Decision

One internal-import standard, enforced by ruff so it can't silently re-drift.

1. **Import names from the submodule that defines them — no parent-package re-export façades.** Write `from concord.models.votes import Vote`, not `from concord.models import Vote`. A package `__init__.py` does not re-export its submodules' symbols. This is self-enforcing: with the façade gone, `from concord.models import Vote` raises `ImportError`.

2. **Importing a submodule *object* through its package is fine.** `from concord.web import app` / `from concord.storage import votes as votes_storage` import the *submodule*, not a re-exported symbol; they work against a bare `__init__` and are allowed. (In the prior discussion: "ban Pattern X, allow Pattern Y".)

3. **A package `__init__.py` carries only:** its docstring; package metadata; or a composition root. It never assembles a flat namespace from its children. Two standing exceptions under this rule:
   - `concord/__init__.py` exposes `__version__` via `importlib.metadata` (package metadata, per ADR 0014).
   - `concord/cli/__init__.py` is the Typer **composition root** — it builds the `app`, registers stage sub-apps, and imports its command submodules for their decorator side effects (`from concord.cli import bills, members, proceedings, votes  # noqa: F401`). This is application assembly, not a namespace façade.

4. **Absolute imports only**, enforced by `ban-relative-imports = "all"` in `[tool.ruff.lint.flake8-tidy-imports]`. The composition root's sibling imports become absolute like everything else (`from concord.cli._apps import …`) — no `# noqa: TID252` carve-out.

## Consequences

- `concord/models`, `concord/storage`, `concord/web` `__init__.py` files are now docstring-only; `concord/cli/__init__.py` keeps the composition root and drops its (unused) `__all__` re-export block. ~52 consumer sites were rewritten to submodule-direct imports and ~55 relative imports converted to absolute.
- Imports are **self-locating**: a reader (human or agent) of any file sees the full provenance of every symbol without resolving the file's position — and the codebase's active churn mode (decomposing modules into per-domain files) no longer risks silent breakage from relative-path re-resolution.
- This class of `models`-package import cycle is structurally impossible: importing `concord.models` no longer imports any submodule, so it cannot participate in a cycle.
- One genuine sharp edge: a symbol reached through a package namespace via attribute access (`import concord.cli as m; m.Progress`) stops resolving once the façade is gone, even though a `from concord.cli import …` grep shows no consumers. Such accesses must point at the defining submodule (`concord.cli._common.Progress`). This bit exactly one test during the sweep.
- Trade-off accepted: importing a name now requires knowing its submodule. That cost is paid once at write time and bought back every time the code is read. Given ADR 0014 already disclaims import stability, there is no external surface to preserve.
