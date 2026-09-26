#!/usr/bin/env bash
# set-service-env/status.sh - read-only preflight or postflight.
#
# The remote command is a literal. It has no arguments and no stdin. Success
# is exit 0 plus exactly one grammar-valid line whose state is ready, reason
# is ok, and generation equals EXPECTED_GENERATION. A grammar-valid refusal
# (missing or invalid, exit 0) still fails this step. Exit 75 with empty
# stdout is the host lock timeout. Stderr is never treated as success and is
# not copied into the log. A line that is not the grammar is not logged.
#
# This does not replace the host deploy recheck. It does not stage, confirm,
# abort, or send a profile value.
#
# Inputs (environment):
#   SERVICE_ENV_PROFILE   fields-postgres-v1
#   APP                   fieldsofrevik
#   EXPECTED_GENERATION   32 lowercase hex
#   SSH_CONFIG            ssh config to look for deploy-target in
#   GITHUB_OUTPUT         receives generation=<id> on success
set -euo pipefail

# shellcheck source=set-service-env/lib.sh
source "$(dirname "$0")/lib.sh"

: "${SERVICE_ENV_PROFILE:=}"
: "${APP:=}"
: "${EXPECTED_GENERATION:=}"
: "${SSH_CONFIG:=$HOME/.ssh/config}"
: "${GITHUB_OUTPUT:=}"

umask 077

scoped_check_profile
scoped_check_app
scoped_check_no_profile_values
scoped_check_generation

if ! grep -qE '^[[:space:]]*Host[[:space:]]+deploy-target[[:space:]]*$' "$SSH_CONFIG" 2>/dev/null; then
	scoped_refuse "No deploy-target entry in ~/.ssh/config. Run the connect action before scoped-env status."
fi

stdout_file=$(mktemp)
stderr_file=$(mktemp)
chmod 600 "$stdout_file" "$stderr_file"
cleanup() {
	rm -f "$stdout_file" "$stderr_file"
}
trap cleanup EXIT

rc=0
# Literal argv. EXPECTED_GENERATION is compared locally and is not an argument.
# The command is not built from APP.
ssh deploy-target 'doas /usr/local/bin/scoped-env-status-fieldsofrevik' \
	</dev/null >"$stdout_file" 2>"$stderr_file" || rc=$?

if [ "$rc" -eq 75 ]; then
	if [ ! -s "$stdout_file" ]; then
		echo "::error::scoped-env status lock timeout (exit 75). Stdout was empty."
		exit 75
	fi
	echo "::error::scoped-env status exit 75 was not an empty lock timeout. Remote text was suppressed."
	exit 1
fi

if [ "$rc" -eq 1 ] && [ ! -s "$stdout_file" ]; then
	echo "::error::scoped-env status wrote no line (exit 1)."
	exit 1
fi

if [ "$rc" -ne 0 ] && [ ! -s "$stdout_file" ]; then
	echo "::error::scoped-env status ssh failed (exit ${rc}). Remote text was suppressed."
	exit "$rc"
fi

parsed=$(mktemp)
chmod 600 "$parsed"
parse_rc=0
scoped_parse_status_file "$stdout_file" >"$parsed" || parse_rc=$?
if [ "$parse_rc" -ne 0 ]; then
	rm -f "$parsed"
	echo "::error::scoped-env status was not the exact nonsecret grammar and was suppressed."
	exit 1
fi

state=$(sed -n '1p' "$parsed")
generation=$(sed -n '2p' "$parsed")
reason=$(sed -n '3p' "$parsed")
rm -f "$parsed"

case "$state" in
	ready | missing | invalid) ;;
	*) scoped_refuse "internal: status parser returned an unexpected state." ;;
esac
case "$reason" in
	ok | no-current | bad-mode | symlink | partial | profile-mismatch) ;;
	*) scoped_refuse "internal: status parser returned an unexpected reason." ;;
esac

if [ "$state" != ready ] || [ "$reason" != ok ] || [ "$generation" != "$EXPECTED_GENERATION" ]; then
	echo "::error::scoped-env status refused: state=${state} reason=${reason} generation=${generation}."
	exit 1
fi
if [ "$rc" -ne 0 ]; then
	echo "::error::scoped-env status ssh failed after a matching line."
	exit "$rc"
fi
if [ -z "$GITHUB_OUTPUT" ]; then
	scoped_refuse "GITHUB_OUTPUT is empty; refusing to succeed without a place to record the generation."
fi
printf 'generation=%s\n' "$generation" >>"$GITHUB_OUTPUT"
echo "Scoped env status ready generation=${generation}."
