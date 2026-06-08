#!/usr/bin/env bash
#
# Smoke-test a published congress-concord wheel — the executable form of the
# "TestPyPI smoke-test recipe" in docs/releases.md (release step 3, and a final
# check for step 6). Deliberately scoped to the smoke test: it does NOT bump
# versions, tag, or cut releases — those steps are human-gated by design (see
# the "big footgun" and pre-release-checkbox sections of the doc).
#
# It downloads ONLY our wheel from the chosen index, then installs it from the
# local file so dependencies resolve from real PyPI — sidestepping the
# --extra-index-url footgun where a TestPyPI typosquat outranks a real dep. It
# runs in a throwaway venv that is removed on exit; it never touches the
# project's own environment.
#
# Usage:
#   scripts/smoke-test-release.sh                 # version from pyproject.toml, TestPyPI
#   scripts/smoke-test-release.sh 0.7.0rc1        # explicit version, TestPyPI
#   scripts/smoke-test-release.sh 0.7.0 pypi      # explicit version, real PyPI
#
# Env:
#   PYTHON   interpreter for the throwaway venv (default: python3.12)
#
set -euo pipefail

VERSION="${1:-}"
INDEX="${2:-testpypi}"
PYTHON="${PYTHON:-python3.12}"
PACKAGE="congress-concord"

# Default the version to [project].version in pyproject.toml (resolved relative
# to the repo root, so the script works from any CWD).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -z "$VERSION" ]; then
  VERSION="$("$PYTHON" - "$REPO_ROOT/pyproject.toml" <<'PY'
import sys, tomllib, pathlib
print(tomllib.loads(pathlib.Path(sys.argv[1]).read_text())["project"]["version"])
PY
)"
fi

case "$INDEX" in
  testpypi) INDEX_URL="https://test.pypi.org/simple/" ;;
  pypi)     INDEX_URL="https://pypi.org/simple/" ;;
  *) echo "error: unknown index '$INDEX' (use 'testpypi' or 'pypi')" >&2; exit 2 ;;
esac

echo ">> smoke-testing ${PACKAGE}==${VERSION} from ${INDEX} (${INDEX_URL})"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
VENV="$WORK/venv"
DL="$WORK/dl"

"$PYTHON" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip

# Download ONLY our wheel (no deps) from the chosen index.
"$VENV/bin/pip" download --index-url "$INDEX_URL" --no-deps --dest "$DL" \
  "${PACKAGE}==${VERSION}"

WHEEL="$(find "$DL" -name 'congress_concord-*.whl' | head -1)"
if [ -z "$WHEEL" ]; then
  echo "FAIL: no wheel downloaded for ${PACKAGE}==${VERSION}" >&2
  exit 1
fi
echo ">> downloaded $(basename "$WHEEL")"

# Install from the local file; deps come from the default index (real PyPI).
"$VENV/bin/pip" install --quiet "$WHEEL"

# 1. CLI entry point runs.
"$VENV/bin/concord" --help >/dev/null
echo ">> concord --help: ok"

# 2. Declared version matches what we asked for.
GOT="$("$VENV/bin/python" -c 'import concord; print(concord.__version__)')"
if [ "$GOT" != "$VERSION" ]; then
  echo "FAIL: installed __version__=${GOT}, expected ${VERSION}" >&2
  exit 1
fi
echo ">> __version__ == ${VERSION}: ok"

# 3. The packaged store actually bootstraps: schema applies and stamps to head.
"$VENV/bin/python" - <<'PY'
import sqlite3
import tempfile

from concord.storage.sqlite import _HEAD, ensure_schema

db = tempfile.mktemp(suffix=".db")
ensure_schema(db)
conn = sqlite3.connect(db)
version = conn.execute("PRAGMA user_version").fetchone()[0]
conn.close()
assert version == _HEAD, f"user_version {version} != _HEAD {_HEAD}"
print(f">> ensure_schema bootstraps to user_version={version}: ok")
PY

echo ">> PASS: ${PACKAGE}==${VERSION} installs cleanly, CLI runs, version matches, schema bootstraps."
echo ">> also eyeball the project page: ${INDEX_URL%simple/}project/${PACKAGE}/"
