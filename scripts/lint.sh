#!/usr/bin/env bash
set -euo pipefail
actionlint
shellcheck scripts/*.sh check-secrets/*.sh activate/*.sh deploy/*.sh preview/*.sh set-service-env/*.sh tests/*.sh
python3 scripts/lint-actions.py
