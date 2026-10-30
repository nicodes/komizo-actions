#!/usr/bin/env bash
set -euo pipefail
args=(--compose "$COMPOSE" --scoped-env-dir "$SCOPED_ENV_DIR")
# Read the newline-separated inputs with `read -r` rather than word
# splitting: a host-only entry contains a space after the colon, and
# splitting on it would turn one rule into two malformed ones.
while IFS= read -r line; do
  [ -n "$line" ] || continue
  args+=(--workflow "$line")
done <<< "$WORKFLOWS"
while IFS= read -r line; do
  [ -n "$line" ] || continue
  args+=(--host-only "$line")
done <<< "$HOST_ONLY"
if [ -n "$SECRETS_ENV_SERVICES" ]; then
  args+=(--secrets-env-services "$SECRETS_ENV_SERVICES")
fi
bash "$GITHUB_ACTION_PATH/../scripts/with-yaml.sh" "$GITHUB_ACTION_PATH/check.py" "${args[@]}"
