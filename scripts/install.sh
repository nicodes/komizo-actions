#!/usr/bin/env bash
set -euo pipefail
python3 -m venv .artifacts/venv
.artifacts/venv/bin/python -m pip install --disable-pip-version-check --require-hashes -r requirements-yaml.txt

mise exec python -- python3 -I "${TOOLS_ROOT:?Select the reviewed Tools source}/cookbook/source_policy.py" setup
