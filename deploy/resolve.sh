#!/usr/bin/env bash
# deploy/resolve.sh - settle the values later steps read, and write them as
# step outputs.
#
# A file rather than a `run:` block so it can be executed by a test. The inputs
# arrive as environment variables and the answers go to $GITHUB_OUTPUT, which is
# exactly the shape a table-driven test wants -- see tests/deploy-inputs.test.sh.
#
# Inputs (environment):
#   HOST           the host: input, empty to fall back to KOMIZO_SERVER_URL
#   APP            the app: input, empty to fall back to KOMIZO_APP_NAME
#   SECRET_NAMES   the secrets: input (newline-separated names)
#   GITHUB_OUTPUT  where the answers go
#
# Outputs: host, app, has-secrets
set -euo pipefail

: "${HOST:=${KOMIZO_SERVER_URL:-}}"
# A hostname, not a URL. See connect: a scheme is stripped rather than
# rejected, because it is the obvious thing to paste into a variable with that
# name and ssh cannot use it.
HOST="${HOST#*://}"
HOST="${HOST%%/*}"

# A newline here would forge a SECOND line in $GITHUB_OUTPUT -- the file is
# `name=value` per line -- so a host carrying "\nhas-secrets=false" would
# silently skip set-secrets and deploy against stale values. The full charset
# checks stay with connect and validate.sh; this only guards the write.
# Rejected before, not after, `echo host=`.
case "$HOST" in
	*[$'\n\r']*)
		echo "::error::host must not contain a newline."
		exit 1 ;;
esac
echo "host=$HOST" >> "$GITHUB_OUTPUT"

# Same treatment for the app name, and for the same reason: it decides the ssh
# user and the two privileged command names, and those are read by composed
# steps that cannot re-derive a shell default.
: "${APP:=${KOMIZO_APP_NAME:-}}"
case "$APP" in
	*[$'\n\r']*)
		echo "::error::app must not contain a newline."
		exit 1 ;;
esac
echo "app=$APP" >> "$GITHUB_OUTPUT"

# Whether there are secrets to push, for the same reason as the host: the
# set-secrets step is guarded by an `if:`, and an expression cannot see
# environment variables that only exist at run time. With the KOMIZO_SECRET_
# prefix the env block is the list, so there is no input for that `if:` to test.
if [ -n "${SECRET_NAMES//[[:space:]]/}" ] || compgen -v | grep -q '^KOMIZO_SECRET_'; then
	echo "has-secrets=true" >> "$GITHUB_OUTPUT"
else
	echo "has-secrets=false" >> "$GITHUB_OUTPUT"
fi
