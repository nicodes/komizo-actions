#!/bin/sh
# Two manual stages; see docs/releases.md; this workflow never moves release tags.
# GitHub release records remain editable; a commit SHA is the stronger identity.
set -eu
exec python3 "$(dirname "$0")/release.py" "$@"
