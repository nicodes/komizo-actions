#!/usr/bin/env bash
set -euo pipefail
actionlint
mapfile -d '' -t scripts < <(git ls-files -z -- '*.sh')
shellcheck "${scripts[@]}"
python3 "${TOOLS_ROOT:?Select the reviewed Tools source}/helpers/composite-actions.py" .
python3 scripts/lint-actions.py
node --check connect/connect.cjs
node --check connect/cleanup.cjs
