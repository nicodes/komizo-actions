#!/usr/bin/env bash
# set-service-env/run.sh - one verb of the scoped-env host command.
#
# A file rather than a run: block so a test can drive it against a fixture
# ssh. The safety contract:
#
#   * profile, app, operation and (for stage) every value are checked before
#     ssh. A run that is going to fail has not switched the host link.
#   * the remote argv is exactly
#     doas /usr/local/bin/set-scoped-env-fieldsofrevik <verb>
#     with no other arguments. The verb is a case arm, not an interpolation.
#   * stage sends the 11-line payload on stdin and nowhere else. confirm,
#     abort and status send an empty stdin. Values never travel as arguments.
#   * secret bytes are not written to the step log. Remote text is logged
#     only when every line is a set-scoped-env: safe status and a scan finds
#     no scannable secret. A stream that contains a secret fails the step.
#   * a stage ssh that returns 0 writes a marker before any output is judged,
#     so a later always() abort can still restore the link if this step then
#     fails closed on a leaked byte. confirm's marker stops that abort from
#     undoing a confirm the host already accepted.
#
# What this script does not do, and must not be read as doing: it does not
# restart containers, it does not roll back containers or database role
# credentials or migrations on abort, and it does not delete a retained
# previous generation. docker compose up may not recreate a service whose
# resolved config is unchanged. Those limits are the host's, and the
# cancellation lease / indefinite retention / strict value charset questions
# are unresolved there. See docs/fields-scoped-env-v1.md.
#
# Inputs (environment):
#   OPERATION              stage | confirm | abort | status
#   SERVICE_ENV_PROFILE    fields-postgres-v1
#   APP                    must be fieldsofrevik; KOMIZO_APP_NAME is the fallback
#   SSH_CONFIG             ssh config to look for deploy-target in
#   GITHUB_OUTPUT          where result=<operation> is written on success
#   RUNNER_TEMP            marker directory
#   KOMIZO_SCOPED_*        required for stage; ignored on the other verbs
#                          except as a log-redaction scan when they are set
set -euo pipefail

# shellcheck source=set-service-env/lib.sh
source "$(dirname "$0")/lib.sh"

: "${OPERATION:=}"
: "${SERVICE_ENV_PROFILE:=}"
: "${APP:=}"
: "${SSH_CONFIG:=$HOME/.ssh/config}"
: "${GITHUB_OUTPUT:=}"

umask 077

case "$OPERATION" in
	stage | confirm | abort | status) ;;
	*)
		scoped_refuse "operation must be stage, confirm, abort or status; got '${OPERATION}'." ;;
esac

scoped_check_profile
scoped_check_app

if [ "$OPERATION" = stage ]; then
	scoped_check_no_legacy_secrets
	scoped_check_values
fi

if ! grep -qE '^[[:space:]]*Host[[:space:]]+deploy-target[[:space:]]*$' "$SSH_CONFIG" 2>/dev/null; then
	scoped_refuse "No deploy-target entry in ~/.ssh/config. Run the connect action before set-service-env."
fi

payload=$(mktemp)
stdout_file=$(mktemp)
stderr_file=$(mktemp)
chmod 600 "$payload" "$stdout_file" "$stderr_file"
cleanup() {
	rm -f "$payload" "$stdout_file" "$stderr_file"
}
trap cleanup EXIT

if [ "$OPERATION" = stage ]; then
	scoped_write_payload "$payload"
fi

# The four argv strings are literals. OPERATION is not concatenated into the
# remote command: a value that passed the case above still must not be what
# the remote shell parses.
rc=0
case "$OPERATION" in
	stage)
		ssh deploy-target 'doas /usr/local/bin/set-scoped-env-fieldsofrevik stage' \
			<"$payload" >"$stdout_file" 2>"$stderr_file" || rc=$?
		;;
	confirm)
		ssh deploy-target 'doas /usr/local/bin/set-scoped-env-fieldsofrevik confirm' \
			</dev/null >"$stdout_file" 2>"$stderr_file" || rc=$?
		;;
	abort)
		ssh deploy-target 'doas /usr/local/bin/set-scoped-env-fieldsofrevik abort' \
			</dev/null >"$stdout_file" 2>"$stderr_file" || rc=$?
		;;
	status)
		ssh deploy-target 'doas /usr/local/bin/set-scoped-env-fieldsofrevik status' \
			</dev/null >"$stdout_file" 2>"$stderr_file" || rc=$?
		;;
esac

# Markers record what the host accepted, not what we are willing to print.
# Written before the secret scan so a leaked byte still leaves a trail the
# deploy abort step can see — except we only write them when ssh returned 0,
# which is the host's "this verb completed" signal.
if [ "$rc" -eq 0 ]; then
	case "$OPERATION" in
		stage)
			printf 'staged\n' >"$(scoped_staged_marker)"
			chmod 600 "$(scoped_staged_marker)"
			;;
		confirm)
			printf 'confirmed\n' >"$(scoped_confirmed_marker)"
			chmod 600 "$(scoped_confirmed_marker)"
			;;
		abort)
			rm -f "$(scoped_staged_marker)"
			;;
	esac
fi

secret_leak=0
scoped_emit_safe stdout "$stdout_file" || secret_leak=$?
scoped_emit_safe stderr "$stderr_file" || secret_leak=$?

if [ "$secret_leak" -ne 0 ]; then
	exit 1
fi
if [ "$rc" -ne 0 ]; then
	echo "::error::set-scoped-env: remote ${OPERATION} failed."
	exit "$rc"
fi

if [ -z "$GITHUB_OUTPUT" ]; then
	scoped_refuse "GITHUB_OUTPUT is empty; refusing to succeed without a place to record the operation."
fi
printf 'result=%s\n' "$OPERATION" >>"$GITHUB_OUTPUT"
echo "Scoped env ${OPERATION} completed."
