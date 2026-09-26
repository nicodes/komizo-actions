#!/usr/bin/env bash
# tests/set-service-env.test.sh - pin the scoped-env action's safety contract
# shellcheck disable=SC2319
#
# against a fixture ssh.
#
# WHY THIS EXISTS. fields-postgres-v1 is a production secret path: it holds
# database and session credentials and asks a privileged host command to
# switch the live env link. The runner half is where the guarantees live, and
# it is a pure function of its inputs, so it is pinned here:
#
#   * every value checked before ssh; a missing or empty one never reaches it
#   * the stage stdin is exactly 11 LF lines, v1 then the ten keys in ASCII
#     order, each KEY=<RFC4648 padded standard base64>, and nothing else
#   * confirm, abort and status send an empty stdin and the same argv shape
#     with a different verb, and no other arguments
#   * secret bytes (the fixture values and their base64) do not appear in the
#     step log or the ssh argv
#   * a remote stream that contains a secret is suppressed and fails the step
#   * a safe set-scoped-env: error is surfaced; anything else is not
#   * the deploy opt-in bypasses set-secrets, stages before activate, confirms
#     only after health success, aborts on failure, and fails if unconfirmed
#   * the docs keep the honest limits: compose may not recreate, abort does
#     not roll back containers or database credentials or migrations, and the
#     host cancellation lease, retention and value charset are unresolved
#
# The fixtures are not real secrets.
#
# Run: bash tests/set-service-env.test.sh

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

pass=0
fail=0

ok() {
	if [ "$1" -eq 0 ]; then
		pass=$((pass + 1))
	else
		fail=$((fail + 1))
		printf 'FAIL  %s\n' "$2"
	fi
}

# Fixture values. All long enough that a log leak is detectable, and one
# carries the base64 alphabet's +, / and = so padding and the alphabet are
# not an accident of alphanumeric input. None of these are real secrets.
V_PARTIES='https://clerk.example.test'
V_ISSUER='https://clerk.example.test/iss'
V_JWKS='https://clerk.example.test/jwks.json'
V_MIGRATE='postgres://migrator:migrate-secret@db/app'
V_DB='postgres://app:db-secret@db/app'
V_PG='postgres-secret-value'
V_APP='app-secret-value'
V_BACKUP='backup-secret-value'
V_MIGRATOR='migrator-secret-value'
V_WS='ws-secret-value+/='

scoped_env() {
	printf '%s\n' \
		"KOMIZO_SCOPED_CLERK_AUTHORIZED_PARTIES=$V_PARTIES" \
		"KOMIZO_SCOPED_CLERK_ISSUER=$V_ISSUER" \
		"KOMIZO_SCOPED_CLERK_JWKS_URL=$V_JWKS" \
		"KOMIZO_SCOPED_DATABASE_MIGRATION_URL=$V_MIGRATE" \
		"KOMIZO_SCOPED_DATABASE_URL=$V_DB" \
		"KOMIZO_SCOPED_POSTGRES_PASSWORD=$V_PG" \
		"KOMIZO_SCOPED_REVIK_APP_PASSWORD=$V_APP" \
		"KOMIZO_SCOPED_REVIK_BACKUP_PASSWORD=$V_BACKUP" \
		"KOMIZO_SCOPED_REVIK_MIGRATOR_PASSWORD=$V_MIGRATOR" \
		"KOMIZO_SCOPED_WS_SECRET=$V_WS"
}

# The exact stage body, hardcoded so a shared python bug cannot bless itself.
# 11 lines, each ending in LF, no extra byte.
expected_payload() {
	printf '%s\n' \
		'v1' \
		'CLERK_AUTHORIZED_PARTIES=aHR0cHM6Ly9jbGVyay5leGFtcGxlLnRlc3Q=' \
		'CLERK_ISSUER=aHR0cHM6Ly9jbGVyay5leGFtcGxlLnRlc3QvaXNz' \
		'CLERK_JWKS_URL=aHR0cHM6Ly9jbGVyay5leGFtcGxlLnRlc3Qvandrcy5qc29u' \
		'DATABASE_MIGRATION_URL=cG9zdGdyZXM6Ly9taWdyYXRvcjptaWdyYXRlLXNlY3JldEBkYi9hcHA=' \
		'DATABASE_URL=cG9zdGdyZXM6Ly9hcHA6ZGItc2VjcmV0QGRiL2FwcA==' \
		'POSTGRES_PASSWORD=cG9zdGdyZXMtc2VjcmV0LXZhbHVl' \
		'REVIK_APP_PASSWORD=YXBwLXNlY3JldC12YWx1ZQ==' \
		'REVIK_BACKUP_PASSWORD=YmFja3VwLXNlY3JldC12YWx1ZQ==' \
		'REVIK_MIGRATOR_PASSWORD=bWlncmF0b3Itc2VjcmV0LXZhbHVl' \
		'WS_SECRET=d3Mtc2VjcmV0LXZhbHVlKy89'
}

install_ssh() {
	local tmp="$1"
	mkdir -p "$tmp/bin" "$tmp/home"
	printf 'Host deploy-target\n  HostName control.invalid\n' >"$tmp/ssh_config"
	cat >"$tmp/bin/ssh" <<'SSH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$SSH_CALLS"
cat > "$SSH_STDIN"
if [ -n "${STUB_STDERR:-}" ]; then
	printf '%s\n' "$STUB_STDERR" >&2
fi
if [ -n "${STUB_STDOUT:-}" ]; then
	printf '%s\n' "$STUB_STDOUT"
fi
if [ "${STUB_ECHO_STDIN:-}" = 1 ]; then
	cat "$SSH_STDIN"
fi
exit "${STUB_SSH_RC:-0}"
SSH
	chmod 755 "$tmp/bin/ssh"
	: >"$tmp/calls"
	: >"$tmp/stdin"
	: >"$tmp/output"
}

# run_script <script> <expected-rc> <label> [VAR=VALUE ...]
run_script() {
	local script="$1" want="$2" label="$3"
	shift 3
	local tmp out rc
	tmp="$(mktemp -d)"
	install_ssh "$tmp"
	# shellcheck disable=SC2046 # the fixture assignments are the environment
	out="$(
		env -i \
			PATH="$tmp/bin:/usr/bin:/bin" HOME="$tmp/home" \
			SSH_CONFIG="$tmp/ssh_config" \
			SSH_CALLS="$tmp/calls" SSH_STDIN="$tmp/stdin" \
			GITHUB_OUTPUT="$tmp/output" \
			RUNNER_TEMP="$tmp" \
			APP= SERVICE_ENV_PROFILE= OPERATION= \
			SECRET_NAMES= HEALTH_URLS= \
			REQUIRE_HEALTH= ALLOW_EMPTY_PROFILE= \
			SCOPED_STAGE_OUTCOME= \
			$(scoped_env) \
			"$@" \
			bash "$script" 2>&1
	)"
	rc=$?
	if [ "$rc" -eq "$want" ]; then
		pass=$((pass + 1))
	else
		fail=$((fail + 1))
		printf 'FAIL  %s\n      expected rc=%s, got rc=%s\n      %s\n' "$label" "$want" "$rc" "$out"
	fi
	LAST_TMP=$tmp
	LAST_OUT=$out
}

no_ssh() {
	if [ ! -s "$LAST_TMP/calls" ]; then
		pass=$((pass + 1))
	else
		fail=$((fail + 1))
		printf 'FAIL  %s\n      ssh was called: %s\n' "$1" "$(cat "$LAST_TMP/calls")"
	fi
}

log_has() {
	if printf '%s\n' "$LAST_OUT" | grep -qF "$1"; then
		pass=$((pass + 1))
	else
		fail=$((fail + 1))
		printf 'FAIL  %s\n      log was: %s\n' "$2" "$LAST_OUT"
	fi
}

log_lacks() {
	if printf '%s\n' "$LAST_OUT" | grep -qF "$1"; then
		fail=$((fail + 1))
		printf 'FAIL  %s\n      secret material reached the log\n' "$1"
	else
		pass=$((pass + 1))
	fi
}

argv_is() {
	if grep -qxF "$1" "$LAST_TMP/calls"; then
		pass=$((pass + 1))
	else
		fail=$((fail + 1))
		printf 'FAIL  %s\n      argv was: %s\n' "$2" "$(cat "$LAST_TMP/calls")"
	fi
}

assert_no_fixture_in_log() {
	local item
	for item in "$V_PARTIES" "$V_ISSUER" "$V_JWKS" "$V_MIGRATE" "$V_DB" "$V_PG" "$V_APP" "$V_BACKUP" "$V_MIGRATOR" "$V_WS" \
		'aHR0cHM6Ly9jbGVyay5leGFtcGxlLnRlc3Q=' \
		'cG9zdGdyZXM6Ly9hcHA6ZGItc2VjcmV0QGRiL2FwcA==' \
		'd3Mtc2VjcmV0LXZhbHVlKy89' \
		'postgres-secret-value'; do
		log_lacks "$item" "log must not contain fixture material"
	done
}

echo "== stage payload and argv =="

run_script set-service-env/run.sh 0 "stage accepts the ten values and completes" \
	OPERATION=stage SERVICE_ENV_PROFILE=fields-postgres-v1 APP=fieldsofrevik \
	STUB_STDOUT='set-scoped-env: staged'
argv_is "deploy-target doas /usr/local/bin/set-scoped-env-fieldsofrevik stage" \
	"stage argv is the frozen command and nothing else"
if [ "$(wc -l <"$LAST_TMP/calls" | tr -d ' ')" = 1 ]; then
	pass=$((pass + 1))
else
	fail=$((fail + 1))
	printf 'FAIL  stage made more than one ssh call\n'
fi
if ! grep -q ' -' "$LAST_TMP/calls"; then
	pass=$((pass + 1))
else
	fail=$((fail + 1))
	printf 'FAIL  stage argv has an extra flag (doas -n or otherwise)\n'
fi
expected_payload >"$LAST_TMP/expected"
if cmp -s "$LAST_TMP/expected" "$LAST_TMP/stdin"; then
	pass=$((pass + 1))
else
	fail=$((fail + 1))
	printf 'FAIL  stage stdin was not the exact 11-line payload\n'
fi
# 11 LF bytes, no extra, file ends with LF.
nl=$(tr -cd '\n' <"$LAST_TMP/stdin" | wc -c | tr -d ' ')
[ "$nl" = 11 ]
ok $? "stage stdin has exactly 11 LF bytes"
tail -c 1 "$LAST_TMP/stdin" | grep -q '^$'
ok $? "stage stdin ends with LF"
log_has "set-scoped-env: staged" "a safe status line is logged"
log_has "Scoped env stage completed." "stage completion is logged without values"
grep -qxF 'result=stage' "$LAST_TMP/output"
ok $? "stage writes result=stage and nothing secret to GITHUB_OUTPUT"
if grep -q 'postgres\|clerk\|secret' "$LAST_TMP/output"; then
	fail=$((fail + 1))
	printf 'FAIL  GITHUB_OUTPUT contained fixture material\n'
else
	pass=$((pass + 1))
fi
assert_no_fixture_in_log
[ -f "$LAST_TMP/komizo-scoped-env-fieldsofrevik.staged" ]
ok $? "a successful stage writes the marker and no secret in it"
if grep -q 'secret\|postgres\|clerk' "$LAST_TMP/komizo-scoped-env-fieldsofrevik.staged"; then
	fail=$((fail + 1))
	printf 'FAIL  stage marker contained fixture material\n'
else
	pass=$((pass + 1))
fi

echo "== validation before any remote call =="

run_script set-service-env/run.sh 1 "an empty scoped value is refused" \
	OPERATION=stage SERVICE_ENV_PROFILE=fields-postgres-v1 APP=fieldsofrevik \
	KOMIZO_SCOPED_WS_SECRET=
no_ssh "empty value did not reach ssh"
log_has "KOMIZO_SCOPED_WS_SECRET is empty" "the empty key is named"
log_lacks "$V_DB" "the other values are not logged with the error"

# The assignment form above sets the variable empty if the value is omitted
# by `env VAR`. Prove a truly unset variable with a direct env -u invocation.
tmp="$(mktemp -d)"
install_ssh "$tmp"
out="$(
	env -i \
		PATH="$tmp/bin:/usr/bin:/bin" HOME="$tmp/home" \
		SSH_CONFIG="$tmp/ssh_config" SSH_CALLS="$tmp/calls" SSH_STDIN="$tmp/stdin" \
		GITHUB_OUTPUT="$tmp/output" RUNNER_TEMP="$tmp" \
		OPERATION=stage SERVICE_ENV_PROFILE=fields-postgres-v1 APP=fieldsofrevik \
		KOMIZO_SCOPED_CLERK_AUTHORIZED_PARTIES="$V_PARTIES" \
		KOMIZO_SCOPED_CLERK_ISSUER="$V_ISSUER" \
		KOMIZO_SCOPED_CLERK_JWKS_URL="$V_JWKS" \
		KOMIZO_SCOPED_DATABASE_MIGRATION_URL="$V_MIGRATE" \
		KOMIZO_SCOPED_POSTGRES_PASSWORD="$V_PG" \
		KOMIZO_SCOPED_REVIK_APP_PASSWORD="$V_APP" \
		KOMIZO_SCOPED_REVIK_BACKUP_PASSWORD="$V_BACKUP" \
		KOMIZO_SCOPED_REVIK_MIGRATOR_PASSWORD="$V_MIGRATOR" \
		KOMIZO_SCOPED_WS_SECRET="$V_WS" \
		bash set-service-env/run.sh 2>&1
)"
rc=$?
[ "$rc" -eq 1 ]
ok $? "a truly unset scoped value is refused"
LAST_TMP=$tmp
LAST_OUT=$out
no_ssh "unset value did not reach ssh"
log_has "KOMIZO_SCOPED_DATABASE_URL is not set" "the missing key is named"

run_script set-service-env/run.sh 1 "an extra scoped key is refused" \
	OPERATION=stage SERVICE_ENV_PROFILE=fields-postgres-v1 APP=fieldsofrevik \
	KOMIZO_SCOPED_NOT_A_KEY=extra-secret-value
no_ssh "extra key did not reach ssh"
log_has "KOMIZO_SCOPED_NOT_A_KEY is not one of the ten" "the extra key is named"
log_lacks "extra-secret-value" "the extra value is not logged"

run_script set-service-env/run.sh 1 "a legacy secret beside the profile is refused" \
	OPERATION=stage SERVICE_ENV_PROFILE=fields-postgres-v1 APP=fieldsofrevik \
	KOMIZO_SECRET_DATABASE_URL='legacy-secret-must-not-leak'
no_ssh "legacy secret did not reach ssh"
log_has "KOMIZO_SECRET_DATABASE_URL must not be set" "the legacy name is refused"
log_lacks "legacy-secret-must-not-leak" "the legacy value is not logged"

run_script set-service-env/run.sh 1 "the wrong app is refused" \
	OPERATION=stage SERVICE_ENV_PROFILE=fields-postgres-v1 APP=blog
no_ssh "wrong app did not reach ssh"
log_has "approved only for app fieldsofrevik" "the app mismatch is named"

run_script set-service-env/run.sh 1 "an unapproved profile is refused" \
	OPERATION=stage SERVICE_ENV_PROFILE=other-v1 APP=fieldsofrevik
no_ssh "unapproved profile did not reach ssh"

run_script set-service-env/run.sh 1 "an injected operation is refused" \
	OPERATION='stage;id' SERVICE_ENV_PROFILE=fields-postgres-v1 APP=fieldsofrevik
no_ssh "injected operation did not reach ssh"

run_script set-service-env/run.sh 1 "no deploy-target means no ssh" \
	OPERATION=stage SERVICE_ENV_PROFILE=fields-postgres-v1 APP=fieldsofrevik \
	SSH_CONFIG=/nonexistent
no_ssh "missing deploy-target did not reach ssh"
[ ! -f "$LAST_TMP/komizo-scoped-env-fieldsofrevik.staged" ]
ok $? "a refused stage writes no marker"

echo "== confirm, abort, status send no payload =="

run_script set-service-env/run.sh 0 "confirm sends the verb and an empty stdin" \
	OPERATION=confirm SERVICE_ENV_PROFILE=fields-postgres-v1 APP=fieldsofrevik \
	STUB_STDOUT='set-scoped-env: confirmed'
argv_is "deploy-target doas /usr/local/bin/set-scoped-env-fieldsofrevik confirm" \
	"confirm argv is the frozen command"
if [ ! -s "$LAST_TMP/stdin" ]; then
	pass=$((pass + 1))
else
	fail=$((fail + 1))
	printf 'FAIL  confirm sent a stdin payload\n'
fi
assert_no_fixture_in_log
[ -f "$LAST_TMP/komizo-scoped-env-fieldsofrevik.confirmed" ]
ok $? "confirm writes the confirmed marker"

run_script set-service-env/run.sh 0 "abort sends the verb and an empty stdin" \
	OPERATION=abort SERVICE_ENV_PROFILE=fields-postgres-v1 APP=fieldsofrevik \
	STUB_STDOUT='set-scoped-env: aborted'
argv_is "deploy-target doas /usr/local/bin/set-scoped-env-fieldsofrevik abort" \
	"abort argv is the frozen command"
[ ! -s "$LAST_TMP/stdin" ]
ok $? "abort sends no stdin"

run_script set-service-env/run.sh 0 "status sends the verb and an empty stdin" \
	OPERATION=status SERVICE_ENV_PROFILE=fields-postgres-v1 APP=fieldsofrevik \
	STUB_STDOUT='set-scoped-env: idle'
argv_is "deploy-target doas /usr/local/bin/set-scoped-env-fieldsofrevik status" \
	"status argv is the frozen command"
[ ! -s "$LAST_TMP/stdin" ]
ok $? "status sends no stdin"
log_has "set-scoped-env: idle" "a nonsecret status is logged"

echo "== logs never carry secret bytes =="

run_script set-service-env/run.sh 1 "a remote stdout that echoes a secret fails and is suppressed" \
	OPERATION=stage SERVICE_ENV_PROFILE=fields-postgres-v1 APP=fieldsofrevik \
	STUB_STDOUT="set-scoped-env: $V_PG"
log_lacks "$V_PG" "echoed secret is not logged"
log_has "contained secret material and was suppressed" "the suppression is named"
# The host accepted the stage. The marker must still exist so deploy can abort.
[ -f "$LAST_TMP/komizo-scoped-env-fieldsofrevik.staged" ]
ok $? "a leaked-byte stage still leaves the marker for abort"

run_script set-service-env/run.sh 1 "a safe host error is surfaced and the value is not" \
	OPERATION=stage SERVICE_ENV_PROFILE=fields-postgres-v1 APP=fieldsofrevik \
	STUB_SSH_RC=1 STUB_STDERR='set-scoped-env: rejected'
log_has "set-scoped-env: rejected" "the safe error line is logged"
log_has "remote stage failed" "the failure is named"
assert_no_fixture_in_log
[ ! -f "$LAST_TMP/komizo-scoped-env-fieldsofrevik.staged" ]
ok $? "a failed stage ssh writes no marker"

run_script set-service-env/run.sh 1 "an unsafe host error is suppressed" \
	OPERATION=stage SERVICE_ENV_PROFILE=fields-postgres-v1 APP=fieldsofrevik \
	STUB_SSH_RC=1 STUB_STDERR="boom $V_DB"
log_lacks "$V_DB" "unsafe stderr did not reach the log"
log_has "contained secret material and was suppressed" "unsafe stderr is named as suppressed"

run_script set-service-env/run.sh 0 "a non-status host line is suppressed without failing" \
	OPERATION=status SERVICE_ENV_PROFILE=fields-postgres-v1 APP=fieldsofrevik \
	STUB_STDOUT='current -> generation'
log_lacks "current -> generation" "an unsafe status line is not logged"
log_has "suppressed 1 remote stdout line" "the drop is counted"

echo "== newline values stay a single payload line =="

tmp="$(mktemp -d)"
install_ssh "$tmp"
out="$(
	env -i \
		PATH="$tmp/bin:/usr/bin:/bin" HOME="$tmp/home" \
		SSH_CONFIG="$tmp/ssh_config" SSH_CALLS="$tmp/calls" SSH_STDIN="$tmp/stdin" \
		GITHUB_OUTPUT="$tmp/output" RUNNER_TEMP="$tmp" \
		OPERATION=stage SERVICE_ENV_PROFILE=fields-postgres-v1 APP=fieldsofrevik \
		KOMIZO_SCOPED_CLERK_AUTHORIZED_PARTIES="$V_PARTIES" \
		KOMIZO_SCOPED_CLERK_ISSUER="$V_ISSUER" \
		KOMIZO_SCOPED_CLERK_JWKS_URL="$V_JWKS" \
		KOMIZO_SCOPED_DATABASE_MIGRATION_URL="$V_MIGRATE" \
		KOMIZO_SCOPED_DATABASE_URL="$V_DB" \
		KOMIZO_SCOPED_POSTGRES_PASSWORD=$'line1\nline2' \
		KOMIZO_SCOPED_REVIK_APP_PASSWORD="$V_APP" \
		KOMIZO_SCOPED_REVIK_BACKUP_PASSWORD="$V_BACKUP" \
		KOMIZO_SCOPED_REVIK_MIGRATOR_PASSWORD="$V_MIGRATOR" \
		KOMIZO_SCOPED_WS_SECRET="$V_WS" \
		STUB_STDOUT='set-scoped-env: staged' \
		bash set-service-env/run.sh 2>&1
)"
rc=$?
[ "$rc" -eq 0 ]
ok $? "a value containing a newline is accepted"
nl=$(tr -cd '\n' <"$tmp/stdin" | wc -c | tr -d ' ')
[ "$nl" = 11 ]
ok $? "a newline inside a value does not add a payload line"
grep -qxF 'POSTGRES_PASSWORD=bGluZTEKbGluZTI=' "$tmp/stdin"
ok $? "the newline value is RFC4648 on one line"
if printf '%s\n' "$out" | grep -qF $'line1\nline2'; then
	fail=$((fail + 1))
	printf 'FAIL  newline value reached the log\n'
else
	pass=$((pass + 1))
fi

echo "== abort-if-staged =="

run_script set-service-env/abort-if-staged.sh 0 "no marker and a failed stage does not ssh" \
	SERVICE_ENV_PROFILE=fields-postgres-v1 APP=fieldsofrevik \
	SCOPED_STAGE_OUTCOME=failure
no_ssh "abort-if-staged did not call ssh without a marker"

run_script set-service-env/abort-if-staged.sh 1 "a cancelled stage with no marker fails loud and does not pretend to abort" \
	SERVICE_ENV_PROFILE=fields-postgres-v1 APP=fieldsofrevik \
	SCOPED_STAGE_OUTCOME=cancelled
no_ssh "cancelled stage without a marker did not ssh"
log_has "cancellation lease" "the unresolved lease is named"
log_has "previous-generation retention" "unresolved retention is named"

# A completed stage marker must abort, with an empty stdin.
tmp="$(mktemp -d)"
install_ssh "$tmp"
printf 'staged\n' >"$tmp/komizo-scoped-env-fieldsofrevik.staged"
out="$(
	env -i \
		PATH="$tmp/bin:/usr/bin:/bin" HOME="$tmp/home" \
		SSH_CONFIG="$tmp/ssh_config" SSH_CALLS="$tmp/calls" SSH_STDIN="$tmp/stdin" \
		GITHUB_OUTPUT="$tmp/output" RUNNER_TEMP="$tmp" \
		SERVICE_ENV_PROFILE=fields-postgres-v1 APP=fieldsofrevik \
		SCOPED_STAGE_OUTCOME=success \
		STUB_STDOUT='set-scoped-env: aborted' \
		bash set-service-env/abort-if-staged.sh 2>&1
)"
rc=$?
[ "$rc" -eq 0 ]
ok $? "a staged marker aborts"
LAST_TMP=$tmp
LAST_OUT=$out
argv_is "deploy-target doas /usr/local/bin/set-scoped-env-fieldsofrevik abort" \
	"cleanup abort uses the frozen command"
[ ! -s "$tmp/stdin" ]
ok $? "cleanup abort sends no payload"
[ ! -f "$tmp/komizo-scoped-env-fieldsofrevik.staged" ]
ok $? "a successful abort removes the staged marker"

tmp="$(mktemp -d)"
install_ssh "$tmp"
printf 'staged\n' >"$tmp/komizo-scoped-env-fieldsofrevik.staged"
printf 'confirmed\n' >"$tmp/komizo-scoped-env-fieldsofrevik.confirmed"
out="$(
	env -i \
		PATH="$tmp/bin:/usr/bin:/bin" HOME="$tmp/home" \
		SSH_CONFIG="$tmp/ssh_config" SSH_CALLS="$tmp/calls" SSH_STDIN="$tmp/stdin" \
		GITHUB_OUTPUT="$tmp/output" RUNNER_TEMP="$tmp" \
		SERVICE_ENV_PROFILE=fields-postgres-v1 APP=fieldsofrevik \
		SCOPED_STAGE_OUTCOME=success \
		bash set-service-env/abort-if-staged.sh 2>&1
)"
rc=$?
[ "$rc" -eq 0 ]
ok $? "a confirmed marker does not abort"
LAST_TMP=$tmp
if [ ! -s "$tmp/calls" ]; then
	pass=$((pass + 1))
else
	fail=$((fail + 1))
	printf 'FAIL  abort ran after confirm\n'
fi

echo "== deploy early gate =="

run_script set-service-env/validate.sh 0 "an empty profile is the legacy path" \
	ALLOW_EMPTY_PROFILE=1 SERVICE_ENV_PROFILE=
no_ssh "validate does not ssh"

run_script set-service-env/validate.sh 1 "the opt-in fails closed without health-urls" \
	ALLOW_EMPTY_PROFILE=1 REQUIRE_HEALTH=1 \
	SERVICE_ENV_PROFILE=fields-postgres-v1 APP=fieldsofrevik \
	HEALTH_URLS='   '
log_has "health-urls is empty" "empty health-urls is refused"
no_ssh "health gate does not ssh"

run_script set-service-env/validate.sh 0 "the opt-in accepts health-urls and the ten values" \
	ALLOW_EMPTY_PROFILE=1 REQUIRE_HEALTH=1 \
	SERVICE_ENV_PROFILE=fields-postgres-v1 APP=fieldsofrevik \
	HEALTH_URLS=$'https://fieldsofrevik.example/health\n'

run_script set-service-env/validate.sh 1 "the opt-in refuses a legacy names list" \
	ALLOW_EMPTY_PROFILE=1 REQUIRE_HEALTH=1 \
	SERVICE_ENV_PROFILE=fields-postgres-v1 APP=fieldsofrevik \
	HEALTH_URLS='https://fieldsofrevik.example/health' \
	SECRET_NAMES='DATABASE_URL'
log_has "legacy secrets: list must be empty" "a names list beside the opt-in is refused"

echo "== resolve.sh profile output =="

run_script deploy/resolve.sh 0 "an empty profile is written empty" \
	HOST=box.example.com APP=fieldsofrevik SERVICE_ENV_PROFILE=
grep -qxF 'service-env-profile=' "$LAST_TMP/output"
ok $? "empty profile output is empty"
grep -qxF 'has-secrets=false' "$LAST_TMP/output"
ok $? "legacy has-secrets detection is unchanged when no secret is set"

run_script deploy/resolve.sh 0 "whitespace-only profile is not an opt-in" \
	HOST=box.example.com SERVICE_ENV_PROFILE='   '
grep -qxF 'service-env-profile=' "$LAST_TMP/output"
ok $? "whitespace profile collapses to empty"

run_script deploy/resolve.sh 0 "the approved profile is passed through" \
	HOST=box.example.com SERVICE_ENV_PROFILE=fields-postgres-v1 \
	KOMIZO_SECRET_DATABASE_URL=postgres://x
grep -qxF 'service-env-profile=fields-postgres-v1' "$LAST_TMP/output"
ok $? "profile output is the approved value"
grep -qxF 'has-secrets=true' "$LAST_TMP/output"
ok $? "legacy detection still reports KOMIZO_SECRET_* so the step if can bypass it"

run_script deploy/resolve.sh 1 "a newline in the profile is refused" \
	HOST=box.example.com SERVICE_ENV_PROFILE=$'fields-postgres-v1\nhas-secrets=false'

echo "== deploy wiring =="

line_of() {
	grep -n "$1" deploy/action.yml | head -n 1 | cut -d: -f1
}
stage_line=$(line_of 'id: scoped-stage')
activate_line=$(line_of 'id: version')
health_line=$(line_of 'id: health')
confirm_line=$(line_of 'id: scoped-confirm')
abort_line=$(line_of 'id: scoped-abort')
unconfirmed_line=$(line_of 'id: scoped-unconfirmed')
prune_line=$(line_of 'prune.sh')
[ "$stage_line" -lt "$activate_line" ]
ok $? "stage is before activate"
[ "$activate_line" -lt "$health_line" ]
ok $? "activate is before health"
[ "$health_line" -lt "$confirm_line" ]
ok $? "confirm is after health"
[ "$confirm_line" -lt "$abort_line" ]
ok $? "abort is after confirm"
[ "$abort_line" -lt "$unconfirmed_line" ]
ok $? "the unconfirmed failure is after abort"
[ "$unconfirmed_line" -lt "$prune_line" ]
ok $? "prune is after the unconfirmed gate"

grep -q "has-secrets == 'true' && steps.cfg.outputs.service-env-profile == ''" deploy/action.yml
ok $? "set-secrets is bypassed when the profile is set"
grep -q "steps.health.outcome == 'success'" deploy/action.yml
ok $? "confirm requires health success, not a skipped check"
grep -q 'if: always()' deploy/action.yml
ok $? "abort runs under always()"
grep -q 'scoped env was staged but not confirmed' deploy/action.yml
ok $? "an unconfirmed stage fails the job"
if grep -q 'uses: nicodes/komizo-actions/set-service-env@' deploy/action.yml; then
	fail=$((fail + 1))
	printf 'FAIL  deploy pins set-service-env at a SHA that does not contain it\n'
else
	pass=$((pass + 1))
fi
grep -q 'set-service-env/run.sh' deploy/action.yml
ok $? "deploy drives the sibling script from the same checkout"
grep -q 'set-service-env/abort-if-staged.sh' deploy/action.yml
ok $? "deploy's cleanup is the abort-if-staged script"

# The release pin set must stay the five siblings. A new uses: would make
# the next release fail canonical_tree, or pin an action that is not at that SHA.
if grep -q 'nicodes/komizo-actions/set-service-env@' deploy/action.yml set-service-env/action.yml; then
	fail=$((fail + 1))
	printf 'FAIL  a composed pin to set-service-env was introduced\n'
else
	pass=$((pass + 1))
fi

echo "== the honest limits stay written down =="

for phrase in \
	'docker compose up may not recreate' \
	'does not roll back containers' \
	'database role credentials or migrations' \
	'cancellation lease' \
	'previous-generation retention' \
	'strict value charset' \
	'./secrets/current/{postgres,migrate,api,godot-api}.env'; do
	if grep -qF "$phrase" docs/fields-scoped-env-v1.md set-service-env/action.yml; then
		pass=$((pass + 1))
	else
		fail=$((fail + 1))
		printf 'FAIL  missing honest limit: %s\n' "$phrase"
	fi
done

grep -qF 'doas /usr/local/bin/set-scoped-env-fieldsofrevik' set-service-env/run.sh
ok $? "the frozen command is the one the script runs"
if grep -q 'doas -n' set-service-env/*.sh; then
	fail=$((fail + 1))
	printf 'FAIL  doas -n is an extra argument the frozen command does not have\n'
else
	pass=$((pass + 1))
fi
if grep -q 'set -x' set-service-env/*.sh; then
	fail=$((fail + 1))
	printf 'FAIL  xtrace would print secret values\n'
else
	pass=$((pass + 1))
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
