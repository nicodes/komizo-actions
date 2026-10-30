#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
scratch=$(mktemp -d)
trap 'rm -rf -- "$scratch"' EXIT
mkdir "$scratch/poison" "$scratch/bin"
printf 'raise RuntimeError("caller YAML module must not load")\n' > "$scratch/poison/yaml.py"
printf '#!/bin/sh\nexit 99\n' > "$scratch/bin/komizo"
chmod 700 "$scratch/bin/komizo"
printf '{"recipe":"invalid","tools":"invalid"}\n' > "$scratch/komizo.json"
printf 'services:\n  app:\n    image: example/app:fixture\n' > "$scratch/compose.yml"
printf 'name: Fixture\non: push\njobs: {}\n' > "$scratch/workflow.yml"
cd "$scratch"
PYTHONPATH="$scratch/poison" PATH="$scratch/bin:$PATH" RUNNER_TEMP="$scratch" \
  GITHUB_ACTION_PATH="$root/check-secrets" COMPOSE="$scratch/compose.yml" \
  WORKFLOWS="$scratch/workflow.yml" SCOPED_ENV_DIR=secrets/current HOST_ONLY= \
  SECRETS_ENV_SERVICES= bash "$root/check-secrets/run.sh"
test -z "$(find "$scratch" -maxdepth 1 -type d -name 'komizo-secret-parser.*' -print)"
