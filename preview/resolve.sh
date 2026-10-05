#!/usr/bin/env bash
# preview/resolve.sh - settle the host value the connect step's `if:` reads,
# and write it as a step output.
#
# A file rather than a `run:` block so it can be executed by a test. Mirrors
# the host half of deploy/resolve.sh; preview needs nothing else resolved --
# app is a required input here, not an environment fallback.
#
# Inputs (environment):
#   HOST           the host: input, empty to fall back to KOMIZO_SERVER_URL
#   GITHUB_OUTPUT  where the answer goes
#
# Outputs: host
set -euo pipefail

: "${HOST:=${KOMIZO_SERVER_URL:-}}"
# A hostname, not a URL. See connect: a scheme is stripped rather than
# rejected, because it is the obvious thing to paste into a variable with that
# name and ssh cannot use it.
HOST="${HOST#*://}"
HOST="${HOST%%/*}"

# A newline here would forge a SECOND line in $GITHUB_OUTPUT -- the file is
# `name=value` per line. Rejected before, not after, `echo host=`.
case "$HOST" in
	*[$'\n\r']*)
		echo "::error::host must not contain a newline."
		exit 1 ;;
esac
echo "host=$HOST" >> "$GITHUB_OUTPUT"
