# 0014 — Publish to PyPI as `congress-concord`, CLI-first

**Status**: Accepted, 2026-05-27.

## Context

Concord has been a private repo with `uv sync` as its install story. Making it `pip install`-able from PyPI turns it into a thing other people can actually depend on, which forces several decisions the repo has been getting away with leaving implicit. None of these decisions is hard on its own, but each one has alternatives that would point the project in a different direction long-term, so they're worth recording together.

The four decisions this ADR covers:

1. **What name to publish under.** `concord` was already taken on PyPI (Concord Systems' CLI tools, ~6 years old, last release 0.2.9). We had to pick something else for the distribution name.
2. **What we promise on the public API.** Once the package is on PyPI, semver becomes meaningful: every version-number bump implies a contract about what does and doesn't break. We had to decide what's covered.
3. **How releases get cut.** PyPI accepts uploads from anywhere with a token, but the modern best practice (since 2023) is trusted publishing via OIDC — PyPI authenticates GitHub Actions directly, no long-lived token to manage.
4. **Where the version string lives.** Either hand-bumped in `pyproject.toml`, or derived from git tags via something like hatch-vcs. These produce very different day-to-day "cut a release" experiences.

## Decision

### Distribution name: `congress-concord`; import name: `concord`

`[project].name = "congress-concord"` on PyPI; `from concord import ...` in Python. Following the same dist-name-≠-import-name pattern as `scikit-learn`/`sklearn`. No code churn from the rename — only `pyproject.toml` changes.

The first-choice name was `concord-congress` (the natural reading: "Concord, for Congress"). PyPI's "Add a new pending publisher" form rejected it with `Invalid project name`. The most likely explanation is PyPI's anti-typosquatting check that blocks new names which structurally look like extensions of an existing project: `<existing>-<suffix>` is treated as confusable with `<existing>`. Since `concord` (Concord Systems' CLI tools) exists on PyPI, `concord-*` is closed off. `congress-concord` doesn't start with `concord-`, so it doesn't trip the rule.

### CLI is the stable contract

What semver tracks: `concord <subcommand>` shape, flag names, exit codes, the format of success-summary lines on stdout, and the on-disk JSONL + SQLite formats that the CLI produces (other tools may read these files).

What semver does *not* track: Python imports. `from concord.storage.sqlite import SqliteStorage` may break between minor versions as the codebase refactors. The README says so explicitly. If a downstream Python user shows up wanting library-stable imports, that's a future ADR and a future contract — not a thing we promise at first publish.

### Trusted publishing via OIDC on GitHub Release published

The existing `release.yml` fires on Release published (it already builds and pushes a Docker image to GHCR). The PyPI publish jobs piggyback on the same event, in parallel. No `PYPI_API_TOKEN` in GitHub Secrets — PyPI authenticates the workflow via short-lived OIDC tokens issued by GitHub.

Two publish jobs split by prerelease status:

- `publish-testpypi` fires when `github.event.release.prerelease == true`. Publishes to test.pypi.org.
- `publish-pypi` fires when `prerelease == false`. Publishes to pypi.org.

The existing Docker job also gets the `prerelease == false` gate — prerelease tags are dry-runs and shouldn't leak Docker images to GHCR's `latest`.

This requires one-time manual setup in the PyPI and TestPyPI web UIs: register `congress-concord` on each, configure the GitHub repo + workflow file + environment name as a trusted publisher. Not automatable; documented in the PyPI-readiness PR's description.

### Version source: manual bump in `pyproject.toml`, exposed via `importlib.metadata`

`[project].version` is the single source of truth. To cut a release, you edit the version, commit, push, then publish a GitHub Release matching the new tag. The release event fires the workflow.

`concord.__version__` is exposed at the package root via `importlib.metadata.version("congress-concord")` rather than a hard-coded string. That way the version is never duplicated, and editable installs (where `importlib.metadata` may raise) fall back to `"0.0.0+unknown"`.

### First published version: `0.2.1`

The codebase has grown substantially since `0.1.0` was set (Members, Bills, Votes, web layer, semantic search, ~100× the LOC), so starting fresh PyPI history below the `0.2.x` line wouldn't honestly reflect where the project is. `1.0.0` is reserved for the day the CLI contract is firm enough to be worth promising — not today.

The actual first-published version is `0.2.1`, not `0.2.0`. `v0.2.0` already existed as a GitHub release (created before this ADR landed and before `release.yml` had a PyPI publish job), so the version was effectively "burned" in the project's release history even though no wheel ever reached PyPI. Rather than retroactively recycle it, the first PyPI publish skips ahead one patch: `v0.2.1rc1` for the TestPyPI dry-run, `v0.2.1` for real PyPI.

The pyproject.toml version is the **source of truth for what gets published**, not the git tag. `uv build` reads `[project].version` from pyproject and bakes it into the wheel filename and metadata; the git tag is only a workflow trigger. So every release requires two coordinated changes: bump `pyproject.toml` (committed and merged to master) *then* publish a GitHub release whose tag matches that version. Forgetting either step is the manual-bump tradeoff this ADR accepts; the alternative (`hatch-vcs` deriving version from the tag) was rejected for the reasons above.

## Consequences

**Trade-offs accepted:**

- **Two names for one thing.** Users see `pip install congress-concord` but `import concord` in their code. Surprises some people the first time. Mitigated by leading the README with both names side-by-side. The alternative — renaming the import path — would have rippled through every file in the codebase, every ADR, every doc; not warranted.
- **No protection for downstream Python importers.** Anyone who writes `from concord.storage.sqlite import SqliteStorage` does so at their own risk. As the project refactors, they may have to keep up. Honest given the project's age and one-author shape; revisitable if a real library-API user shows up.
- **Trusted publishing requires manual one-time setup on PyPI's side.** Can't be fully automated. Has to be done once per project per index (PyPI + TestPyPI = two setups).
- **PyPI versions are permanent.** Anything we publish, even a typo, lives forever (yanking is possible but messy and visible). Mitigated by the TestPyPI dry-run before the real first publish.
- **Manual version bumps can be forgotten.** PyPI rejects re-uploads of the same version, so forgetting to bump is recoverable but annoying. Alternative (hatch-vcs from git tags) would prevent this entirely but adds tooling and produces messy dev-build version strings.

**Things this buys:**

- **A real install story.** `pip install congress-concord` and the CLI works. No `git clone` + `uv sync` required for users who just want to run a scrape.
- **Forever-no-token-rotation.** Trusted publishing means we never have to manage a PyPI API token, never have to rotate one, never have to worry about one leaking. Modern default.
- **A free dry-run channel.** TestPyPI is the same software as PyPI; publishing to it validates the entire pipeline (wheel building, classifier rendering, README rendering, install + import smoke test) before we touch the real index.
- **Refactor freedom.** Internal modules can be reorganized without breaking semver as long as the CLI shape doesn't change. Cleanup work doesn't cost a major version bump.
- **Version single-sourcing.** `pyproject.toml` carries the version; `concord.__version__` reads it. Can't drift out of sync.

## Rejected: hatch-vcs (version derived from git tags)

Cleaner in principle (the git tag is the version, full stop), but produces dev-build version strings like `0.2.0.dev3+gabc1234` that confuse users running editable installs, and adds the hatch-vcs dependency. Manual bumping is fine for a slow release cadence.

## Rejected: full library API surface as part of semver

Would give downstream Python users a real contract to depend on. Also makes every refactor potentially a breaking change, which is unsustainable for a one-author project. Revisitable if Concord ever has a meaningful library-API user base.

## Rejected: long-lived `PYPI_API_TOKEN` secret

Works, used to be standard, requires manual token rotation and creates a secret-management burden. Trusted publishing is strictly better for any project that uses GitHub Actions.

## Rejected: `CHANGELOG.md` in the repo

GitHub Release notes serve the same purpose with less duplication. If a real CHANGELOG need shows up (e.g. someone running `concord` air-gapped wanting a local copy of release history), revisit.
