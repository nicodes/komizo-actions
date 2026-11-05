#!/usr/bin/env bash
set -euo pipefail
actionlint
shellcheck scripts/*.sh check-secrets/*.sh activate/*.sh deploy/*.sh preview/*.sh set-service-env/*.sh tests/*.sh
python3 "${TOOLS_ROOT:?Select the reviewed Tools source}/helpers/composite-actions.py" .
python3 scripts/lint-actions.py
