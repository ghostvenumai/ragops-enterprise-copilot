#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
PYTHON="${PYTHON:-.venv/bin/python}"
exec "$PYTHON" -m automation.run_loop "$@"
