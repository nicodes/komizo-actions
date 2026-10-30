#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
parser_directory=$(mktemp -d "$RUNNER_TEMP/komizo-yaml-parser.XXXXXXXX")
trap 'rm -rf -- "$parser_directory"' EXIT
python3 -I -m venv "$parser_directory"
"$parser_directory/bin/python" -I -m pip --isolated install --disable-pip-version-check \
  --no-cache-dir --no-deps --require-hashes --index-url https://pypi.org/simple \
  -r "$root/requirements-yaml.txt" >&2
"$parser_directory/bin/python" -I "$@"
