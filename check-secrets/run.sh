#!/usr/bin/env bash
set -euo pipefail
parser_directory=$(mktemp -d "$RUNNER_TEMP/komizo-secret-parser.XXXXXXXX")
trap 'rm -rf -- "$parser_directory"' EXIT
python3 -I -m venv "$parser_directory"
"$parser_directory/bin/python" -I -m pip --isolated install --disable-pip-version-check \
  --no-cache-dir --no-deps --require-hashes --index-url https://pypi.org/simple \
  -r "$GITHUB_ACTION_PATH/requirements.txt" >&2
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
"$parser_directory/bin/python" -I "$GITHUB_ACTION_PATH/check.py" "${args[@]}"
