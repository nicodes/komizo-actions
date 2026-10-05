#!/usr/bin/env bash
set -euo pipefail
for suite in tests/parse-config.test.sh tests/check-secrets.test.sh tests/action-descriptions.test.sh; do
  if bash "$suite" | grep -q '^SKIP'; then
    echo "::error::$suite skipped itself -- PyYAML is missing"
    exit 1
  fi
done

