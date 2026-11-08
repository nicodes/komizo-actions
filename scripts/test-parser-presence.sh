#!/usr/bin/env bash
set -euo pipefail
if bash tests/action-descriptions.test.sh | grep -q '^SKIP'; then
  echo '::error::Action description controls skipped because the YAML reader is missing'
  exit 1
fi
