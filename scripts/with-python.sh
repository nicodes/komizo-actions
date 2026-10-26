#!/usr/bin/env bash
set -euo pipefail
test -x .artifacts/venv/bin/python
export PATH="$PWD/.artifacts/venv/bin:$PATH"
exec "$@"
