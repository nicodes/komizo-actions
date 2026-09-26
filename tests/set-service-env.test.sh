#!/usr/bin/env bash
# tests/set-service-env.test.sh - pin the fields-postgres-v2 status contract.
# shellcheck disable=SC2319
#
# v2 sends no profile value. The runner half is a pure function of its inputs:
#
#   * fieldsofrevik with an empty profile is refused before ssh
#   * KOMIZO_SCOPED_* and KOMIZO_SECRET_* are refused, and their values are
#     not logged
#   * status argv is the literal command, no args, empty stdin
#   * only ready/ok/expected-id succeeds; a grammar-valid refusal fails
#   * a line that is not the grammar is not logged
#   * exit 75 with empty stdout is a lock timeout; anything else is not
#   * deploy argv is four quoted arguments, and a missing scoped-generation
#     line fails even when ssh exits 0
#   * there is no stage, confirm, abort, or shared marker
#
# The fixtures are not real secrets.
#
# Run: bash tests/set-service-env.test.sh

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

pass=0
fail=0
GEN=0123456789abcdef0123456789abcdef
OTHER=fedcba9876543210fedcba9876543210

ok() {
	if [ "$1" -eq 0 ]; then
		pass=$((pass + 1))
	else
		fail=$((fail + 1))
		printf 'FAIL  %s\n' "$2"
	fi
}

status_line() { # <state> <generation> <reason>
	printf 'wire=v2 profile=fields-postgres-v2 source=host-local state=%s generation=%s reason=%s\n' "$1" "$2" "$3"
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
	# shellcheck disable=SC2046
	out="$(
		env -i \
			PATH="$tmp/bin:/usr/bin:/bin" HOME="$tmp/home" \
			SSH_CONFIG="$tmp/ssh_config" \
			SSH_CALLS="$tmp/calls" SSH_STDIN="$tmp/stdin" \
			GITHUB_OUTPUT="$tmp/output" \
			RUNNER_TEMP="$tmp" \
			APP= SERVICE_ENV_PROFILE= EXPECTED_GENERATION= \
			SECRET_NAMES= HEALTH_URLS= \
			REQUIRE_HEALTH= ALLOW_EMPTY_PROFILE= \
			VERSION= REGISTRY= REGISTRY_USER= REGISTRY_TOKEN= \
			STUB_STDOUT= STUB_STDERR= STUB_SSH_RC= \
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
		printf 'FAIL  %s\n      leaked: %s\n' "$2" "$1"
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

stdin_empty() {
	if [ ! -s "$LAST_TMP/stdin" ]; then
		pass=$((pass + 1))
	else
		fail=$((fail + 1))
		printf 'FAIL  %s\n      stdin was: %s\n' "$1" "$(cat "$LAST_TMP/stdin")"
	fi
}

echo "== status =="

run_script set-service-env/status.sh 0 "ready/ok matching id succeeds" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" \
	STUB_STDOUT="$(status_line ready "$GEN" ok)"
argv_is "deploy-target doas /usr/local/bin/scoped-env-status-fieldsofrevik" \
	"status argv is the literal command"
stdin_empty "status stdin is empty"
grep -qxF "generation=$GEN" "$LAST_TMP/output"
ok $? "status records the matching generation"
if find "$LAST_TMP" -name '*staged*' -o -name '*confirmed*' -o -name '*marker*' | grep -q .; then
	fail=$((fail + 1))
	printf 'FAIL  status wrote a marker\n'
else
	pass=$((pass + 1))
fi

run_script set-service-env/status.sh 1 "missing/no-current is a refusal even at exit 0" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" \
	STUB_STDOUT="$(status_line missing none no-current)"
log_has "state=missing reason=no-current" "a closed refusal is named"
log_lacks "wire=v2" "the raw status line is not logged"

run_script set-service-env/status.sh 1 "ready/ok with a different id is a mismatch" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" \
	STUB_STDOUT="$(status_line ready "$OTHER" ok)"
log_has "generation=$OTHER" "the reported id is named"
log_lacks "Scoped env status ready" "a mismatch is not success"

run_script set-service-env/status.sh 1 "an injected status line is suppressed" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" \
	STUB_STDOUT="$(printf '%s\n::set-output name=x::postgres-secret-value\n' "$(status_line ready "$GEN" ok)")"
log_lacks "postgres-secret-value" "injected stdout is not logged"
log_lacks "::set-output" "a workflow command is not logged"
log_has "was suppressed" "protocol failure is a fixed message"

run_script set-service-env/status.sh 1 "wrong key order is suppressed" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" \
	STUB_STDOUT="profile=fields-postgres-v2 wire=v2 source=host-local state=ready generation=$GEN reason=ok"
log_lacks "profile=fields-postgres-v2 wire=v2" "a reordered line is not logged"

run_script set-service-env/status.sh 1 "ready without ok is a protocol error" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" \
	STUB_STDOUT="$(status_line ready "$GEN" no-current)"
log_lacks "reason=no-current" "an invariant break is not echoed"

run_script set-service-env/status.sh 1 "a hex id on a non-ready line is a protocol error" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" \
	STUB_STDOUT="$(status_line invalid "$GEN" bad-mode)"
log_lacks "reason=bad-mode" "a hex id outside ready/ok is not logged"
log_has "was suppressed" "the mixed line is a protocol failure"

run_script set-service-env/status.sh 1 "exit 1 with empty stdout is no line" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" \
	STUB_SSH_RC=1 STUB_STDOUT=
log_has "wrote no line" "an empty exit 1 is named"
log_lacks "Scoped env status ready" "an empty exit 1 is not success"

run_script set-service-env/status.sh 75 "exit 75 with empty stdout is lock timeout" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" \
	STUB_SSH_RC=75 STUB_STDOUT=
log_has "lock timeout" "lock timeout is named"

run_script set-service-env/status.sh 1 "exit 75 with stdout is not a timeout success" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" \
	STUB_SSH_RC=75 STUB_STDOUT="$(status_line ready "$GEN" ok)"
log_lacks "Scoped env status ready" "a timed-out line is not success"
log_lacks "wire=v2" "timeout stdout is not logged"

run_script set-service-env/status.sh 255 "ssh failure with empty stdout fails closed" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" \
	STUB_SSH_RC=255 STUB_STDERR='postgres-secret-value'
log_has "ssh failed" "ssh failure is named"
log_lacks "postgres-secret-value" "stderr is not parsed or logged"

run_script set-service-env/status.sh 1 "a bad generation never reaches ssh" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION='0123456789ABCDEF0123456789ABCDEF'
no_ssh "uppercase generation did not ssh"

run_script set-service-env/status.sh 1 "a short generation never reaches ssh" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION=abc
no_ssh "short generation did not ssh"

run_script set-service-env/status.sh 1 "KOMIZO_SCOPED_* is refused" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" \
	KOMIZO_SCOPED_WS_SECRET='ws-secret-value+/='
no_ssh "a scoped value did not ssh"
log_has "KOMIZO_SCOPED_WS_SECRET" "the variable name is the message"
log_lacks "ws-secret-value+/=" "the scoped value is not logged"

run_script set-service-env/status.sh 1 "KOMIZO_SECRET_* is refused" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" \
	KOMIZO_SECRET_DATABASE_URL='postgres://app:db-secret@db/app'
no_ssh "a legacy secret did not ssh"
log_lacks "db-secret" "the legacy secret value is not logged"

run_script set-service-env/status.sh 1 "CLERK_SECRET_KEY in the runner is refused" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" \
	CLERK_SECRET_KEY='sk_test_fake-clerk-secret'
no_ssh "a clerk secret did not ssh"
log_has "does not accept CLERK_SECRET_KEY" "the refusal names the variable"
log_lacks "fake-clerk-secret" "the clerk secret value is not logged"
log_lacks "sk_test_" "the clerk secret prefix is not logged"

run_script set-service-env/status.sh 1 "a set profile value is refused even when empty" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" \
	WS_SECRET=
no_ssh "an empty profile value did not ssh"
log_has "does not accept WS_SECRET" "an empty profile value is still a channel"

run_script set-service-env/status.sh 1 "fields-postgres-v1 is not a profile" \
	SERVICE_ENV_PROFILE=fields-postgres-v1 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN"
no_ssh "a v1 profile did not ssh"
log_has "fields-postgres-v1 is not approved" "v1 is named as rejected"

run_script set-service-env/status.sh 1 "a v1 status line is a protocol error" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" \
	STUB_STDOUT="wire=v2 profile=fields-postgres-v1 source=host-local state=ready generation=$GEN reason=ok"
log_lacks "profile=fields-postgres-v1" "a v1 status line is not logged"
log_has "was suppressed" "a v1 status line is a protocol failure"
log_lacks "Scoped env status ready" "a v1 status line is not success"

run_script set-service-env/status.sh 1 "the wrong app is refused" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=blog \
	EXPECTED_GENERATION="$GEN"
no_ssh "wrong app did not ssh"

run_script set-service-env/status.sh 1 "no deploy-target means no ssh" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" \
	SSH_CONFIG=/dev/null
no_ssh "missing deploy-target did not ssh"

# Two calls in one temp must not share a marker. The second id is independent.
tmp="$(mktemp -d)"
install_ssh "$tmp"
env -i PATH="$tmp/bin:/usr/bin:/bin" HOME="$tmp/home" \
	SSH_CONFIG="$tmp/ssh_config" SSH_CALLS="$tmp/calls" SSH_STDIN="$tmp/stdin" \
	GITHUB_OUTPUT="$tmp/output" RUNNER_TEMP="$tmp" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" \
	STUB_STDOUT="$(status_line ready "$GEN" ok)" \
	bash set-service-env/status.sh >/dev/null
: >"$tmp/calls"
env -i PATH="$tmp/bin:/usr/bin:/bin" HOME="$tmp/home" \
	SSH_CONFIG="$tmp/ssh_config" SSH_CALLS="$tmp/calls" SSH_STDIN="$tmp/stdin" \
	GITHUB_OUTPUT="$tmp/output2" RUNNER_TEMP="$tmp" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$OTHER" \
	STUB_STDOUT="$(status_line ready "$GEN" ok)" \
	bash set-service-env/status.sh >/dev/null
rc=$?
if [ "$rc" -ne 0 ] && ! find "$tmp" -name '*.staged' -o -name '*.confirmed' | grep -q .; then
	pass=$((pass + 1))
else
	fail=$((fail + 1))
	printf 'FAIL  a second status call shared state or succeeded on the wrong id\n'
fi
rm -rf "$tmp"

echo "== activate =="

run_script set-service-env/activate.sh 0 "four empty-auth args and the required line" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" VERSION=abc123 REGISTRY=ghcr.io \
	STUB_STDOUT="$(printf 'deploy: previous-version=old1\ndeploy: scoped-generation=%s' "$GEN")"
argv_is "deploy-target doas /usr/local/bin/deploy-fieldsofrevik 'abc123' '' '' '$GEN'" \
	"no token sends empty middle arguments, not the registry default"
stdin_empty "no token means empty stdin"
grep -qxF "scoped-generation=$GEN" "$LAST_TMP/output"
ok $? "activate records the generation"
grep -qxF "previous-version=old1" "$LAST_TMP/output"
ok $? "a plain previous-version is recorded"
log_has "deploy: scoped-generation=$GEN" "the accepted id is reconstructed"
log_lacks "previous-version=old1" "a recorded tag is not replayed from the host line"

run_script set-service-env/activate.sh 0 "registry auth is the middle pair and stdin" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" VERSION=abc123 \
	REGISTRY=ghcr.io REGISTRY_USER='github-actions[bot]' \
	REGISTRY_TOKEN='registry-token-value' \
	STUB_STDOUT="deploy: scoped-generation=$GEN"
argv_is "deploy-target doas /usr/local/bin/deploy-fieldsofrevik 'abc123' 'ghcr.io' 'github-actions[bot]' '$GEN'" \
	"auth sends four nonempty quoted arguments"
grep -qxF 'registry-token-value' "$LAST_TMP/stdin"
ok $? "the token stays on stdin"
log_lacks "registry-token-value" "the token is not logged"
if grep -q 'registry-token-value' "$LAST_TMP/calls"; then
	fail=$((fail + 1))
	printf 'FAIL  token reached ssh argv\n'
else
	pass=$((pass + 1))
fi

run_script set-service-env/activate.sh 1 "a mixed registry pair is refused" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" VERSION=abc123 \
	REGISTRY_USER=someone
no_ssh "mixed argv did not ssh"

run_script set-service-env/activate.sh 1 "missing scoped-generation line fails at exit 0" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" VERSION=abc123 \
	STUB_STDOUT='deploy: previous-version=old1'
log_has "exactly one" "absence is a failure"
log_lacks "Scoped deploy completed" "absence is not success"
if grep -q '^version=' "$LAST_TMP/output"; then
	fail=$((fail + 1))
	printf 'FAIL  a missing generation line still wrote outputs\n'
else
	pass=$((pass + 1))
fi

run_script set-service-env/activate.sh 1 "a mismatched deploy line fails" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" VERSION=abc123 \
	STUB_STDOUT="deploy: scoped-generation=$OTHER"
log_has "did not match" "mismatch is named"

run_script set-service-env/activate.sh 255 "ssh failure is not a successful deploy" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" VERSION=abc123 \
	STUB_SSH_RC=255 STUB_STDERR='postgres-secret-value'
log_lacks "Scoped deploy completed" "ssh failure is not success"
log_lacks "postgres-secret-value" "ssh stderr is not copied into the log"
if grep -q 'postgres-secret-value' "$LAST_TMP/output"; then
	fail=$((fail + 1))
	printf 'FAIL  deploy stderr reached GITHUB_OUTPUT\n'
else
	pass=$((pass + 1))
fi

run_script set-service-env/activate.sh 1 "a secret in remote output is suppressed and fails closed" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" VERSION=abc123 \
	STUB_STDOUT="$(printf 'deploy: scoped-generation=%s\nError response from daemon: DATABASE_URL=postgres://fields:fake-db-secret@db:5432/fields\n' "$GEN")" \
	STUB_STDERR='compose failed to interpolate WS_SECRET=fake-ws-secret'
log_lacks "fake-db-secret" "a database URL in stdout is not logged"
log_lacks "fake-ws-secret" "a secret in stderr is not logged"
log_lacks "DATABASE_URL" "the secret assignment is not logged"
log_lacks "WS_SECRET" "the stderr assignment is not logged"
log_lacks "postgres://fields" "the credential URL is not logged"
log_lacks "Scoped deploy completed" "secret-like output is not a green deploy"
log_has "suppressed" "the failure is a generic diagnostic"
if grep -q 'fake-db-secret\|fake-ws-secret' "$LAST_TMP/output"; then
	fail=$((fail + 1))
	printf 'FAIL  a remote secret reached GITHUB_OUTPUT\n'
else
	pass=$((pass + 1))
fi

run_script set-service-env/activate.sh 0 "benign extra deploy lines are not echoed" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" VERSION=abc123 \
	STUB_STDOUT="$(printf 'deploy: previous-version=old1\ndeploy: started=yes\ndeploy: scoped-generation=%s\ndeploy: reverse proxy reloaded\n' "$GEN")" \
	STUB_STDERR='deploy: WARNING -- the proxy would not reload; it is still serving its previous routes'
log_lacks "started=yes" "a known extra line is not replayed"
log_lacks "reverse proxy reloaded" "an unvalidated stdout line is not logged"
log_lacks "still serving" "stderr is not logged"
log_has "Scoped deploy completed" "benign extra lines do not fail the deploy"

run_script set-service-env/activate.sh 1 "an unsafe previous-version is not recorded" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" VERSION=abc123 \
	STUB_STDOUT="$(printf 'deploy: previous-version=old::set-output\ndeploy: scoped-generation=%s' "$GEN")"
log_has "not a plain image tag" "a forged previous-version is refused"
log_lacks "set-output" "a forged previous-version is not logged"
if grep -q 'set-output' "$LAST_TMP/output"; then
	fail=$((fail + 1))
	printf 'FAIL  forged previous-version reached GITHUB_OUTPUT\n'
else
	pass=$((pass + 1))
fi

echo "== validate =="

run_script set-service-env/validate.sh 0 "an empty profile is the legacy path" \
	ALLOW_EMPTY_PROFILE=1 APP=blog
no_ssh "legacy validate did not ssh"

run_script set-service-env/validate.sh 1 "fieldsofrevik refuses an empty profile" \
	ALLOW_EMPTY_PROFILE=1 APP=fieldsofrevik
no_ssh "fields empty profile did not ssh"
log_has "requires service-env-profile" "the refusal names the profile"

run_script set-service-env/validate.sh 1 "a generation on the legacy path is not dropped" \
	ALLOW_EMPTY_PROFILE=1 APP=blog EXPECTED_GENERATION="$GEN"
log_has "silently dropped" "legacy generation is refused"

run_script set-service-env/validate.sh 1 "the opt-in fails closed without health-urls" \
	ALLOW_EMPTY_PROFILE=1 REQUIRE_HEALTH=1 \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN"
log_has "health-urls is empty" "health is required"

run_script set-service-env/validate.sh 0 "the opt-in accepts health-urls and a generation" \
	ALLOW_EMPTY_PROFILE=1 REQUIRE_HEALTH=1 \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" HEALTH_URLS='https://example.test/health'

run_script set-service-env/validate.sh 1 "the opt-in refuses a secrets list" \
	ALLOW_EMPTY_PROFILE=1 REQUIRE_HEALTH=1 \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" HEALTH_URLS='https://example.test/health' \
	SECRET_NAMES=DATABASE_URL
log_has "does not accept a secrets: list" "the names list is refused"

run_script set-service-env/validate.sh 1 "the opt-in refuses CLERK_SECRET_KEY before ssh" \
	ALLOW_EMPTY_PROFILE=1 REQUIRE_HEALTH=1 \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" HEALTH_URLS='https://example.test/health' \
	CLERK_SECRET_KEY='sk_test_fake-clerk-secret'
no_ssh "validate did not ssh with a clerk secret"
log_lacks "fake-clerk-secret" "validate does not log the clerk secret"

run_script set-service-env/activate.sh 1 "a clerk secret in deploy output is suppressed" \
	SERVICE_ENV_PROFILE=fields-postgres-v2 APP=fieldsofrevik \
	EXPECTED_GENERATION="$GEN" VERSION=abc123 \
	STUB_STDOUT="deploy: scoped-generation=$GEN" \
	STUB_STDERR='CLERK_SECRET_KEY=sk_test_remote-clerk-secret'
log_lacks "remote-clerk-secret" "a remote clerk secret is not logged"
log_lacks "CLERK_SECRET_KEY" "the remote assignment is not logged"
log_lacks "Scoped deploy completed" "a remote clerk secret is not success"

echo "== wiring =="

line_of() {
	grep -n "$1" deploy/action.yml | head -n 1 | cut -d: -f1
}

preflight=$(line_of 'id: scoped-preflight')
activate=$(line_of 'id: scoped-activate')
health=$(line_of 'id: health')
postflight=$(line_of 'id: scoped-postflight')
unpinned=$(line_of 'id: scoped-unpinned')
prune=$(line_of 'name: Prune superseded images')
if [ -n "$preflight" ] && [ -n "$activate" ] && [ -n "$health" ] && [ -n "$postflight" ] && [ -n "$unpinned" ] && [ -n "$prune" ] \
	&& [ "$preflight" -lt "$activate" ] && [ "$activate" -lt "$health" ] && [ "$health" -lt "$postflight" ] && [ "$postflight" -lt "$unpinned" ] && [ "$unpinned" -lt "$prune" ]; then
	pass=$((pass + 1))
else
	fail=$((fail + 1))
	printf 'FAIL  scoped steps are out of order: pre=%s act=%s health=%s post=%s gate=%s prune=%s\n' \
		"$preflight" "$activate" "$health" "$postflight" "$unpinned" "$prune"
fi

grep -q "has-secrets == 'true' && steps.cfg.outputs.service-env-profile == '' && steps.cfg.outputs.app != 'fieldsofrevik'" deploy/action.yml
ok $? "set-secrets is skipped for the profile and for fieldsofrevik"

grep -q 'set-service-env/status.sh' deploy/action.yml
ok $? "deploy calls the status script"
grep -q 'set-service-env/activate.sh' deploy/action.yml
ok $? "deploy calls the four-arg activate script"

if grep -qE 'set-service-env/run\.sh|abort-if-staged|OPERATION: (stage|confirm|abort)|set-scoped-env-fieldsofrevik' deploy/action.yml set-service-env/*.sh; then
	fail=$((fail + 1))
	printf 'FAIL  stage/confirm/abort wire remains\n'
else
	pass=$((pass + 1))
fi

if grep -q 'uses: nicodes/komizo-actions/set-service-env@' deploy/action.yml set-service-env/action.yml; then
	fail=$((fail + 1))
	printf 'FAIL  a composed pin to set-service-env was introduced\n'
else
	pass=$((pass + 1))
fi

if grep -q 'id: scoped-unpinned' -A 8 deploy/action.yml | grep -qE 'ssh |doas '; then
	fail=$((fail + 1))
	printf 'FAIL  the cancel/failure gate calls ssh\n'
else
	pass=$((pass + 1))
fi

grep -qF 'doas /usr/local/bin/scoped-env-status-fieldsofrevik' set-service-env/status.sh
ok $? "status command is the frozen literal"
grep -qF 'doas /usr/local/bin/deploy-fieldsofrevik' set-service-env/activate.sh
ok $? "deploy command is the frozen literal"
if grep -q 'tee ' set-service-env/activate.sh; then
	fail=$((fail + 1))
	printf 'FAIL  activate still tees remote output into the log\n'
else
	pass=$((pass + 1))
fi
# shellcheck disable=SC2016 # the ${ is a literal we refuse to find
if grep -qF 'deploy-${' set-service-env/status.sh set-service-env/activate.sh; then
	fail=$((fail + 1))
	printf 'FAIL  a scoped command interpolates the app\n'
else
	pass=$((pass + 1))
fi

if grep -q 'doas -n' set-service-env/*.sh || grep -q 'set -x' set-service-env/*.sh; then
	fail=$((fail + 1))
	printf 'FAIL  doas -n or set -x is present\n'
else
	pass=$((pass + 1))
fi

if grep -qE 'base64|KOMIZO_SCOPED_[A-Z].*=' set-service-env/*.sh deploy/action.yml; then
	fail=$((fail + 1))
	printf 'FAIL  a profile value is still encoded or assigned\n'
else
	pass=$((pass + 1))
fi

for phrase in \
	'not prove that a fresh PostgreSQL cutover is safe' \
	'doas /usr/local/bin/scoped-env-status-fieldsofrevik' \
	'deploy: scoped-generation=' \
	'profile=fields-postgres-v1' \
	'mode 0600 files' \
	'There is no stage, confirm, or abort'; do
	if grep -qF "$phrase" docs/fields-scoped-env-v1.md; then
		pass=$((pass + 1))
	else
		fail=$((fail + 1))
		printf 'FAIL  docs missing: %s\n' "$phrase"
	fi
done

if grep -qE 'set-scoped-env-fieldsofrevik|eleven LF|RFC4648' docs/fields-scoped-env-v1.md README.md docs/actions.md; then
	fail=$((fail + 1))
	printf 'FAIL  docs still describe the ten-secret wire\n'
else
	pass=$((pass + 1))
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
