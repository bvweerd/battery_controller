#!/bin/bash
# SessionStart hook: make the Python suite runnable in Claude Code on the web.
#
# Without this, `pip install -r requirements.txt` runs under the container's
# default python3 (3.11). pytest-homeassistant-custom-component follows Home
# Assistant's supported Python floor, so on 3.11 pip resolves back to 0.13.109
# (2023), whose dependency tree — PyRIC, mock-open, an old paho-mqtt — no
# longer builds against modern setuptools. The install fails, and it looks
# like the environment cannot run the tests at all.
#
# On 3.13 pip resolves 0.13.316 and installs cleanly. So the fix is not to
# work around the build errors but to stop using an interpreter that Home
# Assistant left behind.
set -euo pipefail

# Local checkouts have their own environments; only set one up in the remote
# container.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}"

VENV=".venv"

# Newest first: Home Assistant's floor keeps rising, and a newer interpreter
# resolves a newer Home Assistant. 3.13 is the oldest that still works.
PYTHON=""
for candidate in python3.16 python3.15 python3.14 python3.13; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYTHON="$candidate"
    break
  fi
done

if [ -z "$PYTHON" ]; then
  echo "No Python >= 3.13 found; skipping the virtualenv." >&2
  echo "The test suite will not be runnable in this session." >&2
  exit 0
fi

# Idempotent: reuse the venv if it already targets the interpreter we want,
# rebuild it if the container has since gained a newer one.
if [ -x "$VENV/bin/python" ]; then
  have=$("$VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
  want=$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
  if [ "$have" != "$want" ]; then
    echo "Rebuilding .venv: was Python $have, now using $want"
    rm -rf "$VENV"
  fi
fi

if [ ! -x "$VENV/bin/python" ]; then
  echo "Creating .venv with $PYTHON"
  "$PYTHON" -m venv "$VENV"
fi

"$VENV/bin/python" -m pip install --quiet --upgrade pip

# pre-commit and mypy are not in requirements.txt but the CI lint job installs
# them, so a session that can run the tests should be able to run the linters
# too.
echo "Installing test and lint dependencies"
"$VENV/bin/python" -m pip install --quiet -r requirements.txt pre-commit mypy

# The analyzer's Jest suite and its DP runtime guard.
if command -v npm >/dev/null 2>&1 && [ -f package.json ]; then
  echo "Installing npm dependencies"
  npm install --no-audit --no-fund --silent
fi

# Put the venv first on PATH for the rest of the session, so `pytest`,
# `mypy` and `pre-commit` resolve to it without an explicit path.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  {
    echo "export VIRTUAL_ENV=\"$PWD/$VENV\""
    echo "export PATH=\"$PWD/$VENV/bin:\$PATH\""
  } >> "$CLAUDE_ENV_FILE"
fi

HA_VERSION=$("$VENV/bin/python" -m pip show homeassistant 2>/dev/null \
  | awk '/^Version:/ {print $2}')
echo "Ready: $("$VENV/bin/python" -V), Home Assistant ${HA_VERSION:-unknown}"

# Known gap: the pre-commit mypy hook pins language_version to python3.14
# (see .pre-commit-config.yaml). If this container has no 3.14, that one hook
# cannot build its environment. Every other hook works; use
# `SKIP=mypy pre-commit run --all-files`, and run mypy directly instead.
if ! command -v python3.14 >/dev/null 2>&1; then
  echo "Note: no python3.14 here, so the pre-commit mypy hook cannot run."
  echo "      Use: SKIP=mypy pre-commit run --all-files"
fi
