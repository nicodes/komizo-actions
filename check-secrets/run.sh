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
sdk_root="${SOURCE_POLICY_SDK_ROOT:-$PWD}"
"$sdk_root/.artifacts/sdk/source-policy/bin/python" -I "${TOOLS_ROOT:?Select the reviewed Tools source}/cookbook/source_policy.py" check --sdk-root "$sdk_root" "${args[@]}"
