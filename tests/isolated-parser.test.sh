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
  WORKFLOWS="$scratch/workflow.yml" SCOPED_ENV_DIR=secrets/current HOST_ONLY="" \
  SECRETS_ENV_SERVICES="" bash "$root/check-secrets/run.sh"
test -z "$(find "$scratch" -maxdepth 1 -type d -name 'komizo-yaml-parser.*' -print)"

printf 'compose: compose.yml\nhostnames:\n  - fixture.example.com\n' > "$scratch/komizo.yml"
selected=$(PYTHONPATH="$scratch/poison" PATH="$scratch/bin:$PATH" RUNNER_TEMP="$scratch" \
  bash "$root/scripts/with-yaml.sh" "$root/publish-config/parse-config.py" "$scratch/komizo.yml" "$scratch/hostnames")
test "$selected" = "$scratch/compose.yml"
grep -qx fixture.example.com "$scratch/hostnames"
test -z "$(find "$scratch" -maxdepth 1 -type d -name 'komizo-yaml-parser.*' -print)"
