#!/bin/sh
# Two manual stages; see https://github.com/nicodes/docs/blob/main/komizo-actions/history/import-2026-10-05/docs/releases.md; this workflow never moves release tags.
# GitHub release records remain editable; a commit SHA is the stronger identity.
set -eu
exec python3 "$(dirname "$0")/release.py" "$@"
