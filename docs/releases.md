# Releasing `congress-concord`

This document covers the **recurring** release workflow — what to do every time you want to push a new version to PyPI. The one-time setup (registering the PyPI/TestPyPI pending publishers, creating the `pypi` and `testpypi` GitHub environments) and the rationale behind these choices live in [ADR 0014](adr/0014-publish-to-pypi-cli-first.md).

## At a glance

| Step | Where | Effect |
| ---- | ----- | ------ |
| 1. Bump `[project].version` in `pyproject.toml` to e.g. `0.3.0rc1` | PR + merge | Pyproject now reflects the next dry-run version |
| 2. Create a GitHub Release tagged `v0.3.0rc1`, **"Set as a pre-release" ✅** | GitHub UI | Fires `publish-testpypi` only — TestPyPI gets the wheel |
| 3. Smoke-test the TestPyPI wheel — run [`scripts/smoke-test-release.sh`](../scripts/smoke-test-release.sh) (or the [manual recipe](#testpypi-smoke-test-recipe)) | Your machine | Confirms install + import + CLI work |
| 4. Bump `[project].version` to the real version, e.g. `0.3.0` | PR + merge | Drops the `rc1` suffix |
| 5. Create a GitHub Release tagged `v0.3.0`, pre-release **❌ unchecked** | GitHub UI | Fires `publish-pypi` + Docker→GHCR; real PyPI gets the wheel |
| 6. Verify on https://pypi.org/project/congress-concord/ | PyPI page | Eyeball the release page, README rendering, classifiers |

That's the whole loop. Each version-bump PR is a single one-line `pyproject.toml` change — fast to write, fast to review.

## The big footgun: pyproject version vs git tag

The release workflow runs `uv build`, which reads `[project].version` from `pyproject.toml`. **The git tag is only a workflow trigger** — it doesn't influence the version baked into the wheel filename or metadata. So:

- `pyproject.toml` is the source of truth for what gets published.
- The git tag is convention; it should match `pyproject.toml`'s version, but nothing in the workflow checks that.
- The wheel's declared version is what PyPI files it under. If the tag says `v0.3.0` and pyproject still says `0.2.1rc1`, you'll ship a wheel called `congress_concord-0.2.1rc1-...` regardless of the tag.

If you forget to bump `pyproject.toml`, the symptoms depend on the situation:

- **Best case**: PyPI rejects the upload because the version already exists. You bump pyproject, merge, and re-cut the release.
- **Worst case**: the version *doesn't* already exist on PyPI (e.g. you bumped to `0.2.1rc1`, then forgot to bump to `0.2.1` for the real release, but never published `0.2.1rc1` to real PyPI). You'd ship a release-candidate version to the real PyPI under the wrong tag. Recoverable via yank-then-republish-as-next-patch, but messy.

Easiest discipline: the bump-PR's title should match the GitHub release's tag (e.g. PR titled "Bump to 0.3.0" pairs with release `v0.3.0`).

## TestPyPI smoke-test recipe

> **Shortcut:** [`scripts/smoke-test-release.sh`](../scripts/smoke-test-release.sh) runs this whole recipe for you in a throwaway venv — `scripts/smoke-test-release.sh [version] [testpypi|pypi]`, where `version` defaults to `pyproject.toml`'s and the index defaults to TestPyPI. Use it for step 3 (`scripts/smoke-test-release.sh`) and as a final check after step 5 (`scripts/smoke-test-release.sh X.Y.Z pypi`). It is scoped to the smoke test only — it does not bump, tag, or release, since those steps are human-gated by design (see the footgun and checkbox sections). The manual recipe below explains *why* it works the way it does.

The pattern most tutorials suggest — `pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ <package>` — **is unsafe** for our use case. Here's why, and what to do instead.

### Why `--extra-index-url` is broken for this

`--extra-index-url` doesn't prefer one index over the other. pip searches both and picks the highest-version match. TestPyPI is full of typosquats, abandoned experiments, and broken placeholders that real PyPI long since cleaned up — and some of those placeholders happen to have higher version numbers than the legitimate package on real PyPI.

The failure we hit in our first dry-run: pip's resolver picked `FASTAPI-1.0.tar.gz` from TestPyPI (an abandoned 2.5 KB stub with `summary: "A small api that uses fastapi-users"`) over the real `fastapi-0.136+` on PyPI, because `1.0 > 0.136`. Install then crashed building the stub (`FileNotFoundError: 'DESCRIPTION.txt'`).

### The recipe that works

Download only our wheel from TestPyPI, then install it as a local file. pip resolves the wheel's declared dependencies from the default index (real PyPI) without touching TestPyPI for anything else.

```sh
# Clean slate
deactivate 2>/dev/null
rm -rf /tmp/concord-test /tmp/concord-dl

# Fresh venv (use 3.12 or 3.13 — newer Pythons may lag on dep wheels)
python3.12 -m venv /tmp/concord-test
source /tmp/concord-test/bin/activate

# Download ONLY our wheel from TestPyPI, no deps
pip download --index-url https://test.pypi.org/simple/ \
             --no-deps \
             --dest /tmp/concord-dl \
             congress-concord==X.Y.ZrcN

# Install from the local file; deps come from real PyPI
pip install /tmp/concord-dl/congress_concord-X.Y.ZrcN-py3-none-any.whl

# Smoke test
concord --help
python -c "import concord; print(concord.__version__)"  # → X.Y.ZrcN
```

Also eyeball https://test.pypi.org/project/congress-concord/ — the README should render, classifiers should appear in the sidebar, and the "Project links" panel should resolve.

## Pre-release vs release: what the checkbox does

The "Set as a pre-release" checkbox on the GitHub Release form is the single switch that routes the workflow:

- **Checked** → `github.event.release.prerelease == true` → only `publish-testpypi` fires. The Docker job and `publish-pypi` are both gated on `prerelease == false` and get skipped.
- **Unchecked** → `publish-pypi` + the Docker→GHCR build both fire. `publish-testpypi` gets skipped.

You can repeat the dry-run as many times as you need — `rc1`, `rc2`, ... — without ever leaking a Docker image to GHCR or a wheel to real PyPI.

## Recovery: real PyPI rejected the upload

PyPI rejects re-uploads of a version that's already been published, even after a yank. If `publish-pypi` fails with `File already exists` or similar:

1. Check https://pypi.org/project/congress-concord/ for the latest published version.
2. Bump `pyproject.toml` to the next patch number.
3. Re-do steps 4–5 from the table.

Yanking hides broken releases from the dependency resolver but does not free up the version number. There is no way to "redo" a version on PyPI.

## Recovery: `publish-pypi` failed during attestation (Sigstore 5xx)

`gh-action-pypi-publish` signs the artifact and records a PEP 740 attestation in Sigstore's Rekor transparency log **before** it uploads to PyPI. Rekor occasionally returns a transient `5xx`, which fails the job partway through with something like:

```
requests.exceptions.HTTPError: 502 Server Error: Bad Gateway for url: https://rekor.sigstore.dev/api/v1/log/entries
sigstore._internal.rekor.RekorClientError: Rekor returned an unknown error with HTTP 502
```

This is **not** a problem with your package, version, or trusted-publisher config — it's a Sigstore outage. Because attestation runs *before* the upload, nothing reached PyPI, so unlike the rejected-upload case above the version is still free. To recover:

1. **Confirm the version didn't land.** `curl -s https://pypi.org/pypi/congress-concord/json | python3 -c "import sys, json; print('X.Y.Z' in json.load(sys.stdin)['releases'])"`. If it prints `True`, the upload actually succeeded and the failure was a post-upload hiccup — **stop**, treat it as already-published (see the rejected-upload recovery above), and do *not* re-run.
2. **Check Sigstore is healthy again** before retrying: `curl -s -o /dev/null -w "%{http_code}\n" https://rekor.sigstore.dev/api/v1/log` should be `200` (status page: https://status.sigstore.dev/).
3. **Re-run only the failed job** so the Docker build doesn't repeat: `gh run rerun <run-id> --failed`. The skipped `publish-testpypi` and the already-green `build-and-push` are preserved; only `publish-pypi` runs again.

If Rekor is flapping badly and you must ship, you can disable attestations for one run by setting `attestations: false` on the `pypa/gh-action-pypi-publish` step in `release.yml` — but prefer waiting out the outage, since the attestation is what lets installers verify the wheel's provenance.

## Recovery: the workflow didn't fire

If you cut a GitHub Release and nothing happened, common causes:

- The release was saved as a **draft**, not published. Drafts don't fire `release.published`.
- The `pypi` or `testpypi` GitHub environment doesn't exist in repo settings, or its name doesn't match the workflow's `environment:` field.
- The PyPI/TestPyPI pending-publisher config doesn't match the actual workflow filename (`release.yml`), repo (`johnmarcampbell/concord`), or environment name. Verify on https://pypi.org/manage/account/publishing/ and the equivalent on TestPyPI.

Check the **Actions** tab:

- If the workflow appears but a publish job shows **Skipped**, the `if:` gate didn't match — usually the pre-release checkbox state was wrong.
- If the workflow doesn't appear at all, the trigger didn't fire — the release is probably still a draft.
- If the workflow appears but fails with `OIDC token mismatch` or similar, the trusted-publisher config on PyPI doesn't match what the workflow is sending. Check the publisher's claimed `workflow_filename`, `environment`, and `repository` against the workflow file.
