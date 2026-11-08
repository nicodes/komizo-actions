#!/usr/bin/env bash
set -euo pipefail
actionlint
mapfile -t scripts < <(rg --files -g '*.sh' -g '!.artifacts/**' -g '!.ci-tools/**')
shellcheck "${scripts[@]}"
python3 "${TOOLS_ROOT:?Select the reviewed Tools source}/helpers/composite-actions.py" .
python3 scripts/lint-actions.py
node --check connect/connect.cjs
node --check connect/cleanup.cjs
