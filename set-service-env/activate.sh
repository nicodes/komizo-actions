#!/usr/bin/env bash
# set-service-env/activate.sh - Fields deploy argv, four positional arguments.
#
# Legacy activate is not this script. This runs only for fieldsofrevik with
# fields-postgres-v1. The remote command is exactly
#   doas /usr/local/bin/deploy-fieldsofrevik
#     '<version>' '<registry-or-empty>' '<registry-user-or-empty>' '<32hex>'
# The registry token, if any, stays on stdin. The generation is not an
# environment variable on the far side and is not a fifth channel.
#
# No registry token means the middle two arguments are empty quoted strings,
# even when the registry input still has its ghcr.io default. A mixed
# empty/nonempty pair is refused locally.
#
# Success requires ssh exit 0 and exactly one stdout line
#   deploy: scoped-generation=<the same 32 hex>
# Absence of that line is failure, unlike the optional previous-version line.
# This does not roll back containers or the host-local provision. A failed
# activate stays failed.
#
# Inputs (environment):
#   VERSION, REGISTRY, REGISTRY_USER, REGISTRY_TOKEN, EXPECTED_GENERATION
#   SERVICE_ENV_PROFILE, APP, SSH_CONFIG, GITHUB_OUTPUT
set -euo pipefail

# shellcheck source=set-service-env/lib.sh
source "$(dirname "$0")/lib.sh"

: "${VERSION:=}"
: "${REGISTRY:=}"
: "${REGISTRY_USER:=}"
: "${REGISTRY_TOKEN:=}"
: "${EXPECTED_GENERATION:=}"
: "${SERVICE_ENV_PROFILE:=}"
: "${APP:=}"
: "${SSH_CONFIG:=$HOME/.ssh/config}"
: "${GITHUB_OUTPUT:=}"

umask 077

scoped_check_profile
scoped_check_app
scoped_check_no_profile_values
scoped_check_generation

case "$VERSION" in
	"" | *[!A-Za-z0-9._-]*)
		scoped_refuse "version must be a plain image tag (letters, digits, dot, underscore, hyphen)." ;;
esac

if ! grep -qE '^[[:space:]]*Host[[:space:]]+deploy-target[[:space:]]*$' "$SSH_CONFIG" 2>/dev/null; then
	scoped_refuse "No deploy-target entry in ~/.ssh/config. Run the connect action before scoped activate."
fi

# Auth is the token, not the registry input default. Both middle args empty,
# or both nonempty. Never one of each, and never the generation in their place.
reg_arg=""
user_arg=""
if [ -n "$REGISTRY_TOKEN" ]; then
	if [ -z "$REGISTRY" ] || [ -z "$REGISTRY_USER" ]; then
		scoped_refuse "registry-token is set but registry or registry-user is empty."
	fi
	case "$REGISTRY" in
		*[!A-Za-z0-9._:/-]*)
			scoped_refuse "registry contains characters that are not valid in a registry host." ;;
	esac
	# Same charset as activate/action.yml, including the bracket form the
	# deploy-inputs test lifts out of that file.
	case "$REGISTRY_USER" in
		*[!]A-Za-z0-9._@[-]*)
			scoped_refuse "registry-user must be letters, digits, dot, underscore, at-sign, brackets or hyphen." ;;
	esac
	reg_arg=$REGISTRY
	user_arg=$REGISTRY_USER
elif [ -n "$REGISTRY_USER" ]; then
	scoped_refuse "registry-user is set but registry-token is empty; refusing a mixed deploy argv."
fi

log=$(mktemp)
chmod 600 "$log"
cleanup() {
	rm -f "$log"
}
trap cleanup EXIT

echo "Deploying ${VERSION} generation=${EXPECTED_GENERATION}"

fence="komizo-$(date +%s%N)-$RANDOM"
echo "::stop-commands::$fence"
rc=0
# shellcheck disable=SC2029 # the remote command is built from charset-checked fields
printf '%s' "$REGISTRY_TOKEN" \
	| ssh deploy-target "doas /usr/local/bin/deploy-fieldsofrevik '${VERSION}' '${reg_arg}' '${user_arg}' '${EXPECTED_GENERATION}'" \
		2>&1 | tee "$log" || rc=$?
echo "::$fence::"

gen_lines=$(grep -cE '^deploy: scoped-generation=[0-9a-f]{32}$' "$log" || true)
if [ "$gen_lines" -ne 1 ]; then
	echo "::error::deploy did not print exactly one deploy: scoped-generation=<32hex> line."
	if [ "$rc" -ne 0 ]; then
		exit "$rc"
	fi
	exit 1
fi
reported=$(grep -E '^deploy: scoped-generation=[0-9a-f]{32}$' "$log")
reported=${reported#deploy: scoped-generation=}
if [ "$reported" != "$EXPECTED_GENERATION" ]; then
	echo "::error::deploy scoped-generation=${reported} did not match expected-generation."
	exit 1
fi
if [ "$rc" -ne 0 ]; then
	echo "::error::scoped deploy failed."
	exit "$rc"
fi

prev_count=$(grep -c '^deploy: previous-version=' "$log" || true)
if [ "$prev_count" -gt 1 ]; then
	scoped_refuse "deploy printed previous-version more than once."
fi
previous=$(sed -n 's/^deploy: previous-version=//p' "$log" | head -n 1)
case "$previous" in
	*[!A-Za-z0-9._-]*)
		scoped_refuse "deploy previous-version was not a plain image tag and was not recorded." ;;
esac
if [ -z "$GITHUB_OUTPUT" ]; then
	scoped_refuse "GITHUB_OUTPUT is empty; refusing to succeed without a place to record the version."
fi
{
	echo "version=${VERSION}"
	echo "previous-version=${previous}"
	echo "scoped-generation=${reported}"
} >>"$GITHUB_OUTPUT"
echo "Scoped deploy completed generation=${reported}."
