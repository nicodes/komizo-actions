#!/bin/sh
# Releases bind exact accepted source; version tags are never moved.
set -eu
exec python3 "$(dirname "$0")/release.py" "$@"
