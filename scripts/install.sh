#!/usr/bin/env bash
set -euo pipefail
python3 -m venv .artifacts/venv
.artifacts/venv/bin/python -m pip install --disable-pip-version-check --require-hashes -r check-secrets/requirements.txt
