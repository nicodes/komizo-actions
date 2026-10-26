#!/usr/bin/env bash
set -euo pipefail
for suite in tests/*.test.sh; do bash "$suite"; done
bash scripts/test-parser-presence.sh
