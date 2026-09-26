#!/usr/bin/env bash
# set-service-env/lib.sh - checks for the fields-postgres-v1 status contract.
#
# Sourced, never executed. v2 carries no profile values. The only remote
# status command is the literal
#   doas /usr/local/bin/scoped-env-status-fieldsofrevik
# with no arguments and an empty stdin. A line is accepted only when it is
# exactly one record of the frozen grammar. ready, ok, and a 32-hex generation
# travel together; any other pairing of those three is a protocol error and
# is not logged.

export LC_ALL=C

SCOPED_PROFILE=fields-postgres-v1
SCOPED_APP=fieldsofrevik

scoped_refuse() {
	echo "::error::$1"
	exit 1
}

scoped_check_profile() {
	case "${SERVICE_ENV_PROFILE:-}" in
		"$SCOPED_PROFILE") ;;
		"")
			scoped_refuse "service-env-profile is empty. The only approved profile is ${SCOPED_PROFILE}." ;;
		*)
			scoped_refuse "service-env-profile '${SERVICE_ENV_PROFILE}' is not approved. The only approved profile is ${SCOPED_PROFILE}." ;;
	esac
}

scoped_check_app() {
	if [ -z "${APP:-}" ]; then
		APP="${KOMIZO_APP_NAME:-}"
	fi
	if [ "$APP" != "$SCOPED_APP" ]; then
		scoped_refuse "profile ${SCOPED_PROFILE} is approved only for app ${SCOPED_APP}; got '${APP}'."
	fi
}

# Fields never takes the legacy secret path. Naming the variable is the whole
# message; the value is not.
scoped_check_no_profile_values() {
	local var
	if [ -n "${SECRET_NAMES:-}" ] && [ -n "${SECRET_NAMES//[[:space:]]/}" ]; then
		scoped_refuse "fields-postgres-v1 does not accept a secrets: list. Profile values are host-local and are not pushed."
	fi
	while IFS= read -r var; do
		[ -z "$var" ] && continue
		scoped_refuse "fields-postgres-v1 does not accept ${var}. Profile values are host-local and are not sent over GitHub or SSH."
	done < <(compgen -v | grep -E '^(KOMIZO_SCOPED_|KOMIZO_SECRET_)' | sort || true)
}

scoped_check_generation() {
	case "${EXPECTED_GENERATION:-}" in
		*[!0-9a-f]*)
			scoped_refuse "expected-generation must be exactly 32 lowercase hex; refusing before any remote call." ;;
	esac
	if [ "${#EXPECTED_GENERATION}" -ne 32 ]; then
		scoped_refuse "expected-generation must be exactly 32 lowercase hex; refusing before any remote call."
	fi
}

scoped_check_health_required() {
	if [ "${REQUIRE_HEALTH:-0}" != 1 ]; then
		return 0
	fi
	if [ -z "${HEALTH_URLS:-}" ] || [ -z "${HEALTH_URLS//[[:space:]]/}" ]; then
		scoped_refuse "service-env-profile is set but health-urls is empty. The opt-in fails closed: a green deploy requires a URL to poll."
	fi
}

# Print state, generation, reason on stdout and return 0 when the file is
# exactly one grammar-valid line and the ready/ok/hex invariant holds.
# Return 2 for anything else. Never prints the file.
scoped_parse_status_file() {
	local file="$1"
	python3 - "$file" <<'PY'
import re, sys
data = open(sys.argv[1], "rb").read()
if data.endswith(b"\n"):
    data = data[:-1]
if (not data) or b"\n" in data or b"\r" in data:
    sys.exit(2)
try:
    text = data.decode("ascii")
except UnicodeDecodeError:
    sys.exit(2)
pat = re.compile(
    r"wire=v2 profile=fields-postgres-v1 source=host-local "
    r"state=(ready|missing|invalid) "
    r"generation=([0-9a-f]{32}|none) "
    r"reason=(ok|no-current|bad-mode|symlink|partial|profile-mismatch)\Z"
)
m = pat.fullmatch(text)
if not m:
    sys.exit(2)
# ready iff reason=ok iff generation is 32 lowercase hex. Any other
# pairing, including a hex id on a missing or invalid line, is not a
# valid status line. A valid refusal has generation=none.
state, generation, reason = m.group(1), m.group(2), m.group(3)
hex_id = generation != "none"
ready = state == "ready"
ok = reason == "ok"
if ready != ok or ready != hex_id or ok != hex_id:
    sys.exit(2)
sys.stdout.write(state + "\n" + generation + "\n" + reason + "\n")
PY
}
