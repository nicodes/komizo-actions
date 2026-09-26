#!/usr/bin/env bash
# set-service-env/lib.sh - shared checks for the fields-postgres-v1 profile.
#
# Sourced, never executed. The wire contract is frozen:
#   doas /usr/local/bin/set-scoped-env-fieldsofrevik <stage|confirm|abort|status>
#   no other arguments
#   stage stdin is exactly 11 LF-terminated lines: "v1", then the ten keys
#   below in ASCII order, each "KEY=<RFC4648 padded standard base64>"
#   confirm, abort and status send no stdin
#
# This file does not enforce a stricter value charset than "nonempty". The
# host's strict charset is unresolved; inventing one here would reject values
# the host might accept, or accept a charset the host later refuses, and
# either lie would be worse than encoding whatever bash can hold and letting
# a host rejection surface as a safe set-scoped-env: error.

# ASCII order for the key check, independent of the runner's locale.
export LC_ALL=C

# ASCII order. The payload sort is this order, checked again before the wire.
SCOPED_KEYS=(
	CLERK_AUTHORIZED_PARTIES
	CLERK_ISSUER
	CLERK_JWKS_URL
	DATABASE_MIGRATION_URL
	DATABASE_URL
	POSTGRES_PASSWORD
	REVIK_APP_PASSWORD
	REVIK_BACKUP_PASSWORD
	REVIK_MIGRATOR_PASSWORD
	WS_SECRET
)

SCOPED_PROFILE=fields-postgres-v1
SCOPED_APP=fieldsofrevik

scoped_refuse() {
	echo "::error::$1"
	exit 1
}

# Marker files carry no secret bytes. They exist so a later always() step can
# tell a completed stage ssh from a stage that never reached the host.
scoped_state_dir() {
	printf '%s' "${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
}

scoped_staged_marker() {
	printf '%s/komizo-scoped-env-fieldsofrevik.staged' "$(scoped_state_dir)"
}

scoped_confirmed_marker() {
	printf '%s/komizo-scoped-env-fieldsofrevik.confirmed' "$(scoped_state_dir)"
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

# The opt-in replaces set-secrets. A KOMIZO_SECRET_* variable or a names list
# alongside it would look, in a green run, like those values had been pushed.
# Refuse, and name the variable, never its value.
scoped_check_no_legacy_secrets() {
	local var name
	if [ -n "${SECRET_NAMES:-}" ] && [ -n "${SECRET_NAMES//[[:space:]]/}" ]; then
		scoped_refuse "service-env-profile is set, so the legacy secrets: list must be empty. Those names would otherwise be pushed to the aggregate secrets.env."
	fi
	while IFS= read -r var; do
		[ -z "$var" ] && continue
		name=${var#KOMIZO_SECRET_}
		scoped_refuse "service-env-profile is set, so ${var} must not be set. The opt-in does not push KOMIZO_SECRET_* (this one would have been '${name}') to the aggregate secrets.env. Pass the ten KOMIZO_SCOPED_* values instead."
	done < <(compgen -v | grep '^KOMIZO_SECRET_' | sort || true)
}

scoped_check_values() {
	local key var val extra
	local -A want=()
	for key in "${SCOPED_KEYS[@]}"; do
		want["$key"]=1
		var="KOMIZO_SCOPED_${key}"
		if [ -z "${!var+set}" ]; then
			scoped_refuse "KOMIZO_SCOPED_${key} is not set. All ten scoped values must be nonempty before any remote call."
		fi
		val=${!var}
		if [ -z "$val" ]; then
			scoped_refuse "KOMIZO_SCOPED_${key} is empty. GitHub substitutes an empty string for a secret that does not exist; refusing before any remote call."
		fi
	done
	while IFS= read -r var; do
		[ -z "$var" ] && continue
		extra=${var#KOMIZO_SCOPED_}
		if [ -z "${want[$extra]+set}" ]; then
			scoped_refuse "KOMIZO_SCOPED_${extra} is not one of the ten fields-postgres-v1 keys. Refusing so a typo is not silently dropped."
		fi
	done < <(compgen -v | grep '^KOMIZO_SCOPED_' | sort || true)
}

# Health is what makes confirm legal. An opt-in with no URL can stage and
# then has nothing that is allowed to confirm, which is an unconfirmed stage
# on a path that would otherwise go green. Fail closed.
scoped_check_health_required() {
	if [ "${REQUIRE_HEALTH:-0}" != 1 ]; then
		return 0
	fi
	if [ -z "${HEALTH_URLS:-}" ] || [ -z "${HEALTH_URLS//[[:space:]]/}" ]; then
		scoped_refuse "service-env-profile is set but health-urls is empty. The opt-in fails closed: confirm is only legal after a health success, so there is no successful deploy without a URL to poll."
	fi
}

scoped_encode() {
	python3 -c 'import base64,sys; sys.stdout.write(base64.b64encode(sys.stdin.buffer.read()).decode("ascii"))'
}

# Write the stage payload to $1. Caller has already checked the ten values.
# The file is 11 LF-terminated lines and nothing else. No secret is printed.
scoped_write_payload() {
	local dest="$1" key var val b64 raw dec prev
	prev=""
	for key in "${SCOPED_KEYS[@]}"; do
		if [ -n "$prev" ] && [[ "$prev" > "$key" ]]; then
			scoped_refuse "internal: scoped keys are not in ASCII order."
		fi
		prev=$key
	done
	: >"$dest"
	chmod 600 "$dest"
	printf 'v1\n' >>"$dest"
	for key in "${SCOPED_KEYS[@]}"; do
		var="KOMIZO_SCOPED_${key}"
		val=${!var}
		b64=$(printf '%s' "$val" | scoped_encode)
		if [[ ! "$b64" =~ ^[A-Za-z0-9+/]+={0,2}$ ]]; then
			scoped_refuse "internal: base64 for ${key} was not a single RFC4648 line."
		fi
		if [ $((${#b64} % 4)) -ne 0 ]; then
			scoped_refuse "internal: base64 for ${key} was not padded to a multiple of 4."
		fi
		raw=$(mktemp)
		dec=$(mktemp)
		chmod 600 "$raw" "$dec"
		printf '%s' "$val" >"$raw"
		printf '%s' "$b64" | python3 -c 'import base64,sys; sys.stdout.buffer.write(base64.b64decode(sys.stdin.buffer.read(), validate=True))' >"$dec"
		if ! cmp -s "$raw" "$dec"; then
			rm -f "$raw" "$dec"
			scoped_refuse "internal: base64 round-trip failed for ${key}."
		fi
		rm -f "$raw" "$dec"
		printf '%s=%s\n' "$key" "$b64" >>"$dest"
	done
	# Exactly 11 LF bytes, file ends with one LF, no blank line, no CR.
	local newlines last
	newlines=$(tr -cd '\n' <"$dest" | wc -c)
	newlines=${newlines//[[:space:]]/}
	if [ "$newlines" != 11 ]; then
		scoped_refuse "internal: stage payload was ${newlines} lines, not 11."
	fi
	# Command substitution would strip the LF being checked. Compare the byte.
	last=$(tail -c 1 "$dest" | od -An -tu1)
	last=${last//[[:space:]]/}
	if [ "$last" != 10 ]; then
		scoped_refuse "internal: stage payload did not end with LF."
	fi
	if grep -q $'\r' "$dest" || grep -q '^$' "$dest"; then
		scoped_refuse "internal: stage payload had a blank line or a CR."
	fi
	if [ "$(head -n 1 "$dest")" != "v1" ]; then
		scoped_refuse "internal: stage payload did not start with v1."
	fi
}

# Exit 3 if a scannable secret or its base64 appears in the file. Prints
# nothing. Short values are not scanned as raw substrings: a one-character
# secret would match every status line. Those values are still never written
# to the log by this action; the safe-line filter is what keeps them out.
scoped_stream_has_secret() {
	local file="$1"
	python3 - "$file" <<'PY'
import base64, os, sys
data = open(sys.argv[1], "rb").read()
keys = (
    "CLERK_AUTHORIZED_PARTIES",
    "CLERK_ISSUER",
    "CLERK_JWKS_URL",
    "DATABASE_MIGRATION_URL",
    "DATABASE_URL",
    "POSTGRES_PASSWORD",
    "REVIK_APP_PASSWORD",
    "REVIK_BACKUP_PASSWORD",
    "REVIK_MIGRATOR_PASSWORD",
    "WS_SECRET",
)
for key in keys:
    val = os.environ.get("KOMIZO_SCOPED_" + key, "")
    raw = val.encode()
    if len(raw) >= 8 and raw in data:
        sys.exit(3)
    enc = base64.b64encode(raw)
    if len(enc) >= 16 and enc in data:
        sys.exit(3)
sys.exit(0)
PY
}

# Print only lines the host contract calls a nonsecret status, and only after
# a secret scan. Anything else is counted and dropped. Returns 3 if a secret
# was seen (caller must fail), 0 otherwise.
scoped_emit_safe() {
	local stream="$1" file="$2" line kept=0 dropped=0 fence
	if [ ! -s "$file" ]; then
		return 0
	fi
	# python exits 3 when a secret is present and 0 when the stream is clean.
	# An `if cmd` is true only on 0, so the scan result has to be saved: treating
	# "found" as success would log the very bytes this exists to suppress.
	local scan=0
	scoped_stream_has_secret "$file" || scan=$?
	if [ "$scan" -eq 3 ]; then
		echo "::error::set-scoped-env: remote ${stream} contained secret material and was suppressed."
		return 3
	fi
	if [ "$scan" -ne 0 ]; then
		scoped_refuse "internal: secret scan failed."
	fi
	fence="komizo-$(date +%s%N)-$RANDOM"
	# Unquoted on the right of =~ so bash treats it as an ERE, not a literal.
	local safe_re='^set-scoped-env: [A-Za-z0-9._-]+( [A-Za-z0-9._-]+)*$'
	while IFS= read -r line || [ -n "$line" ]; do
		# A safe status is the prefix plus words of letters, digits, dot,
		# underscore or hyphen. Colons, slashes, plus and equals stay out, so
		# a base64 blob or a workflow command cannot be a "safe" line.
		if [[ "$line" =~ $safe_re ]]; then
			if [ "$kept" -eq 0 ]; then
				echo "::stop-commands::$fence"
			fi
			printf '%s\n' "$line"
			kept=$((kept + 1))
		else
			dropped=$((dropped + 1))
		fi
	done <"$file"
	if [ "$kept" -gt 0 ]; then
		echo "::$fence::"
	fi
	if [ "$dropped" -gt 0 ]; then
		echo "set-scoped-env: suppressed ${dropped} remote ${stream} line(s) that were not safe status lines."
	fi
	return 0
}
