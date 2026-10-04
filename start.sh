#!/usr/bin/env bash
# One-command start: create a Python 3.12/3.11 venv, install, preflight, launch.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.12 .venv
  else
    PY="$(command -v python3.12 || command -v python3.11 || true)"
    if [ -z "$PY" ]; then
      echo "Python 3.11/3.12 not found. Install one of:"
      echo "  brew install uv            (recommended; fetches Python 3.12 itself)"
      echo "  brew install python@3.12"
      exit 1
    fi
    "$PY" -m venv .venv
  fi
fi

if command -v uv >/dev/null 2>&1; then
  uv pip install --quiet --python .venv/bin/python -r requirements.txt
else
  .venv/bin/python -m pip install --quiet -r requirements.txt
fi

[ -f .env ] || cp .env.example .env
.venv/bin/python verify_setup.py
exec .venv/bin/python run_local.py
