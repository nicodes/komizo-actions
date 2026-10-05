#!/usr/bin/env bash
# tests/action-descriptions.test.sh - no expression syntax in action prose.
#
# Actions evaluates ${{ }} in an action.yml's description fields, which are
# prose nobody expects to be executed. An expression there is not a
# documentation bug that renders oddly; it is a load failure:
#
#   check-secrets/action.yml (Line: 2, Col: 14): Unrecognized named-value:
#   'secrets'. Located at position 1 within expression: secrets.NAME
#
# Every workflow using the action failed at "Set up job", before one step ran,
# because a description sentence said a value must be written as a
# `${{ secrets.NAME }}` expression. Five repositories went red at once and the
# reason named a line in prose.
#
# Only the prose fields are checked. `outputs.*.value`, a step's `with:`,
# `if:` and `env:` are where expressions belong -- that is how a composite
# action receives its inputs and publishes its results -- and a checker that
# flagged those would be reporting every action in this repository as broken.
#
# Documentation that quotes the syntax writes it without its braces.
#
# Run: bash tests/action-descriptions.test.sh

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

if ! python3 -c 'import yaml' 2>/dev/null; then
	echo "SKIP: PyYAML is not installed"
	exit 0
fi

python3 - <<'PY'
import glob
import sys

import yaml


def prose_fields(doc):
    """Every string in an action.yml that is documentation rather than input."""
    if isinstance(doc.get("description"), str):
        yield "description", doc["description"]
    for section in ("inputs", "outputs"):
        for name, spec in (doc.get(section) or {}).items():
            if isinstance(spec, dict) and isinstance(spec.get("description"), str):
                yield f"{section}.{name}.description", spec["description"]
    for i, step in enumerate((doc.get("runs") or {}).get("steps") or []):
        if isinstance(step, dict) and isinstance(step.get("name"), str):
            yield f"runs.steps[{i}].name", step["name"]


rc = 0
checked = 0
paths = sorted(glob.glob("*/action.yml") + glob.glob(".github/actions/*/action.yml"))
for path in paths:
    doc = yaml.safe_load(open(path)) or {}
    for where, text in prose_fields(doc):
        if "${{" in text:
            print(
                f"::error::{path}: {where} contains expression syntax. Actions "
                "evaluates it and the action fails to load before any step "
                "runs. Quote it without the braces."
            )
            rc = 1
    checked += 1

if not checked:
    print("::error::no action.yml files found -- this test checked nothing")
    sys.exit(1)
if rc:
    sys.exit(rc)
print(f"action prose: {checked} file(s) clean")
PY
