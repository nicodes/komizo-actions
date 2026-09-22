#!/usr/bin/env bash
# tests/preview.test.sh - pin the preview action's safety contract against a
# fixture ssh.
#
# WHY THIS EXISTS. The preview action is a deploy path: it holds the deploy
# key (through connect) and runs a privileged primitive on a production host.
# Its runner half is where the guarantees live, and it is a pure function of
# its inputs, so it is pinned here clause by clause:
#
#   * every input charset-validated before the wire (app slug, pr-number,
#     image refs, action); anything malformed never reaches ssh
#   * the exact ssh argv, with every argument single-quoted
#   * the remote output fenced while it streams
#   * the outputs PARSED out of the fenced capture and validated, never
#     trusted unparsed; contract violations fail the step
#   * the deploy key never forwarded into the invocation, the log, or the
#     outputs
#   * ssh's exit code propagates -- a failed preview is a failed step
#
# Run: bash tests/preview.test.sh

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

pass=0
fail=0

ok() { # <condition-rc> <label>
	if [ "$1" -eq 0 ]; then
		pass=$((pass + 1))
	else
		fail=$((fail + 1))
		printf 'FAIL  %s\n' "$2"
	fi
}

# run_case <expected-rc> <label> [VAR=VALUE ...]
#
# A fake ssh records its argv and prints stubbed remote output. Each case gets
# a fresh environment for the values the script reads, plus an ssh config
# carrying the deploy-target alias (SSH_CONFIG=/nonexistent removes it) and a
# temp GITHUB_OUTPUT.
run_case() {
	local want="$1" label="$2"
	shift 2
	local tmp out rc
	tmp="$(mktemp -d)"
	mkdir -p "$tmp/bin" "$tmp/home"
	printf 'Host deploy-target\n  HostName control.invalid\n' > "$tmp/ssh_config"
	cat > "$tmp/bin/ssh" <<'SSH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$SSH_CALLS"
printf '%s\n' "${STUB_REMOTE_OUTPUT:-safe remote output}"
exit "${STUB_SSH_RC:-0}"
SSH
	chmod 755 "$tmp/bin/ssh"
	: > "$tmp/calls"
	: > "$tmp/output"
	out="$(
		env -i \
			PATH="$tmp/bin:$PATH" HOME="$tmp/home" \
			SSH_CONFIG="$tmp/ssh_config" \
			SSH_CALLS="$tmp/calls" GITHUB_OUTPUT="$tmp/output" \
			APP= PR_NUMBER= IMAGES= ACTION= \
			"$@" \
			bash preview/run.sh 2>&1
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

no_ssh() { # <label> -- nothing reached the host
	if [ ! -s "$LAST_TMP/calls" ]; then
		pass=$((pass + 1))
	else
		fail=$((fail + 1))
		printf 'FAIL  %s\n      ssh was called: %s\n' "$1" "$(cat "$LAST_TMP/calls")"
	fi
}

out_has() { # <fixed-string> <label>
	if printf '%s' "$LAST_OUT" | grep -qF "$1"; then
		pass=$((pass + 1))
	else
		fail=$((fail + 1))
		printf 'FAIL  %s\n      output was: %s\n' "$2" "$LAST_OUT"
	fi
}

outputs_contain() { # <line> -- assert the last run wrote this step output
	if grep -qxF "$1" "$LAST_TMP/output"; then
		pass=$((pass + 1))
	else
		fail=$((fail + 1))
		printf 'FAIL  expected step output %s, got:\n' "$1"
		sed 's/^/      /' "$LAST_TMP/output"
	fi
}

IMG_API=ghcr.io/you/gdam-api:0123456789abcdef0123456789abcdef01234567
IMG_WEB=ghcr.io/you/gdam-web:0123456789abcdef0123456789abcdef01234567
GOOD_UP="preview-url=https://pr-42.preview.example.com
api-url=https://pr-42-api.preview.example.com
gate-status=open"

echo "== the exact ssh argv =="

run_case 0 "up passes app, pr-number and every image, single-quoted" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API $IMG_WEB" ACTION=up \
	STUB_REMOTE_OUTPUT="$GOOD_UP"
expected="deploy-target komizo-box preview up 'gdam' '42' '$IMG_API' '$IMG_WEB'"
if grep -qxF "$expected" "$LAST_TMP/calls"; then
	pass=$((pass + 1))
else
	fail=$((fail + 1))
	printf 'FAIL  exact ssh argv for up\n      want: %s\n      got:  %s\n' "$expected" "$(cat "$LAST_TMP/calls")"
fi

run_case 0 "down names the preview and passes no images" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=down
expected="deploy-target komizo-box preview down 'gdam' '42'"
if grep -qxF "$expected" "$LAST_TMP/calls"; then
	pass=$((pass + 1))
else
	fail=$((fail + 1))
	printf 'FAIL  exact ssh argv for down\n      want: %s\n      got:  %s\n' "$expected" "$(cat "$LAST_TMP/calls")"
fi

echo "== malformed inputs never reach ssh =="

# <label>|<APP>|<PR_NUMBER>|<IMAGES>|<ACTION> -- every row must refuse before
# the wire.
while IFS='|' read -r label app pr images action; do
	run_case 1 "$label" APP="$app" PR_NUMBER="$pr" IMAGES="$images" ACTION="$action"
	no_ssh "$label reached ssh"
	out_has "::error::" "$label fails with an error annotation"
done <<'CASES'
empty action|gdam|42|ghcr.io/you/gdam-api:abc|
unknown action|gdam|42|ghcr.io/you/gdam-api:abc|restart
action with shell|gdam|42|ghcr.io/you/gdam-api:abc|up;id
empty app||42|ghcr.io/you/gdam-api:abc|up
app with a slash|g/dam|42|ghcr.io/you/gdam-api:abc|up
app with uppercase|GDAM|42|ghcr.io/you/gdam-api:abc|up
app with a space|g dam|42|ghcr.io/you/gdam-api:abc|up
app with a quote|g'dam|42|ghcr.io/you/gdam-api:abc|up
empty pr-number|gdam||ghcr.io/you/gdam-api:abc|up
pr-number zero|gdam|0|ghcr.io/you/gdam-api:abc|up
pr-number with a leading zero|gdam|007|ghcr.io/you/gdam-api:abc|up
pr-number negative|gdam|-1|ghcr.io/you/gdam-api:abc|up
pr-number with shell|gdam|42;id|ghcr.io/you/gdam-api:abc|up
empty images|gdam|42||up
whitespace-only images|gdam|42|   |up
image without a tag|gdam|42|ghcr.io/you/gdam-api|up
image without a registry path|gdam|42|gdam-api:abc|up
image without the project-component split|gdam|42|ghcr.io/you/gdam:abc|up
image with a quote|gdam|42|ghcr.io/you/gdam-api:ab'c|up
image with shell substitution|gdam|42|ghcr.io/you/gdam-api:$(id)|up
one bad image of two|gdam|42|ghcr.io/you/gdam-api:abc not-a-ref!|up
CASES

run_case 1 "a newline in the pr-number is refused" \
	APP=gdam PR_NUMBER=$'42\n1' IMAGES="$IMG_API" ACTION=up
no_ssh "newline pr-number reached ssh"

# A registry with a port is a plain reference and passes.
run_case 0 "a registry with a port is accepted" \
	APP=gdam PR_NUMBER=42 IMAGES="registry.internal:5000/you/gdam-api:abc" ACTION=up \
	STUB_REMOTE_OUTPUT="$GOOD_UP"
grep -qxF "deploy-target komizo-box preview up 'gdam' '42' 'registry.internal:5000/you/gdam-api:abc'" "$LAST_TMP/calls"
ok $? "the ported registry ref rides along verbatim"

echo "== the connection seam =="

run_case 1 "no deploy-target alias refuses before ssh" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up SSH_CONFIG=/nonexistent
no_ssh "a missing alias still invoked ssh"
out_has "No deploy-target SSH alias" "a missing alias says to run connect"

echo "== the fence and the exit code =="

run_case 23 "ssh's exit code propagates" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up STUB_SSH_RC=23
case "$LAST_OUT" in
	*"::stop-commands::komizo-preview-"*"::komizo-preview-"*)
		pass=$((pass + 1)) ;;
	*)
		fail=$((fail + 1))
		printf 'FAIL  remote-output fence was not opened and closed\n      %s\n' "$LAST_OUT" ;;
esac

# The host's output is untrusted; it must arrive fenced so workflow commands
# in it cannot execute.
run_case 0 "host output is fenced" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT="$GOOD_UP
::error::forged by the host"
case "$LAST_OUT" in
	*"::stop-commands::komizo-preview-"*"::error::forged by the host"*"::komizo-preview-"*)
		pass=$((pass + 1)) ;;
	*)
		fail=$((fail + 1))
		printf 'FAIL  forged workflow command was not inside the fence\n      %s\n' "$LAST_OUT" ;;
esac

echo "== the outputs are parsed, not trusted =="

run_case 0 "up parses all three outputs" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT="$GOOD_UP"
outputs_contain "preview-url=https://pr-42.preview.example.com"
outputs_contain "api-url=https://pr-42-api.preview.example.com"
outputs_contain "gate-status=open"

# The primitive's own log lines ride along; only the contract keys are parsed.
run_case 0 "unrelated output lines are ignored" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT="preview: creating database pr-42
preview: route written
$GOOD_UP"
outputs_contain "gate-status=open"

run_case 1 "an up without preview-url fails closed" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT="api-url=https://pr-42-api.preview.example.com
gate-status=open"
out_has "refusing to guess" "the missing preview-url is explained"
if [ ! -s "$LAST_TMP/output" ]; then
	pass=$((pass + 1))
else
	fail=$((fail + 1))
	printf 'FAIL  partial outputs were written before the failure\n'
fi

run_case 1 "an up without gate-status fails closed" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT="preview-url=https://pr-42.preview.example.com
api-url=https://pr-42-api.preview.example.com"

run_case 1 "a preview-url that is not https is refused" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT="preview-url=http://pr-42.preview.example.com
api-url=https://pr-42-api.preview.example.com
gate-status=open"

run_case 1 "a preview-url with a space is refused" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT="preview-url=https://pr-42.preview.example.com x
api-url=https://pr-42-api.preview.example.com
gate-status=open"

run_case 1 "a duplicate key is refused" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT="$GOOD_UP
gate-status=closed"

run_case 1 "a gate-status that is not a plain token is refused" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT="preview-url=https://pr-42.preview.example.com
api-url=https://pr-42-api.preview.example.com
gate-status=open;id"

# Down reports the outputs when the primitive prints them, and succeeds when
# it does not -- a teardown has no URL to report.
run_case 0 "down without contract output still succeeds" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=down \
	STUB_REMOTE_OUTPUT="preview: pr-42 torn down, zero orphans"
if [ ! -s "$LAST_TMP/output" ]; then
	pass=$((pass + 1))
else
	fail=$((fail + 1))
	printf 'FAIL  down wrote outputs the host never reported\n'
fi

run_case 0 "down parses gate-status when reported" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=down \
	STUB_REMOTE_OUTPUT="gate-status=removed"
outputs_contain "gate-status=removed"

echo "== the deploy key goes nowhere =="

secret=credential-must-not-appear
run_case 0 "the deploy key is not forwarded" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT="$GOOD_UP" KOMIZO_DEPLOY_KEY="$secret"
if ! grep -qF "$secret" "$LAST_TMP/calls" \
	&& ! printf '%s' "$LAST_OUT" | grep -qF "$secret" \
	&& ! grep -qF "$secret" "$LAST_TMP/output"; then
	pass=$((pass + 1))
else
	fail=$((fail + 1))
	printf 'FAIL  the deploy key reached the invocation, the log, or the outputs\n'
fi
if grep -q 'KOMIZO_DEPLOY_KEY' preview/run.sh; then
	fail=$((fail + 1))
	printf 'FAIL  preview/run.sh reads the deploy key at all\n'
else
	pass=$((pass + 1))
fi

echo "== resolve.sh =="

# The host resolution is the same seam deploy has: the KOMIZO_SERVER_URL
# fallback, scheme-stripping, and the newline guard on the output write.
run_resolve() {
	local want="$1" label="$2"
	shift 2
	local tmp out rc
	tmp="$(mktemp -d)"
	: > "$tmp/output"
	out="$(
		env -i \
			PATH="$PATH" HOME="$tmp" \
			GITHUB_OUTPUT="$tmp/output" HOST= \
			"$@" \
			bash preview/resolve.sh 2>&1
	)"
	rc=$?
	if [ "$rc" -eq "$want" ]; then
		pass=$((pass + 1))
	else
		fail=$((fail + 1))
		printf 'FAIL  %s\n      expected rc=%s, got rc=%s\n      %s\n' "$label" "$want" "$rc" "$out"
	fi
	LAST_TMP=$tmp
}

run_resolve 0 "a plain hostname passes through" HOST=box.example.com
outputs_contain "host=box.example.com"

run_resolve 0 "a scheme and path are stripped" HOST=https://box.example.com/preview
outputs_contain "host=box.example.com"

run_resolve 0 "the KOMIZO_SERVER_URL fallback is used when host is empty" \
	KOMIZO_SERVER_URL=fallback.example.com
outputs_contain "host=fallback.example.com"

run_resolve 0 "an explicit host wins over the fallback" \
	HOST=explicit.example.com KOMIZO_SERVER_URL=fallback.example.com
outputs_contain "host=explicit.example.com"

run_resolve 1 "a newline in the host is refused" HOST=$'box.example.com\nx=y'

echo "== the composite wiring =="

# The composite composes connect (pinned like deploy's siblings), threads the
# inputs through env rather than interpolating them, and maps the three
# outputs from the run step.
grep -q 'uses: nicodes/komizo-actions/connect@' preview/action.yml
ok $? "preview composes connect"

# shellcheck disable=SC2016 # single quotes are deliberate: literal workflow expressions
grep -q 'run: bash "$GITHUB_ACTION_PATH/run.sh"' preview/action.yml
ok $? "the run step drives preview/run.sh"

# shellcheck disable=SC2016 # single quotes are deliberate: a literal workflow expression
grep -q 'APP: ${{ inputs.app }}' preview/action.yml
ok $? "app arrives through env, not interpolation"

# shellcheck disable=SC2016 # single quotes are deliberate: literal workflow expressions
grep -q 'value: ${{ steps.preview.outputs.preview-url }}' preview/action.yml
ok $? "the preview-url output maps from the run step"

# shellcheck disable=SC2016 # single quotes are deliberate: literal workflow expressions
grep -q 'value: ${{ steps.preview.outputs.api-url }}' preview/action.yml
ok $? "the api-url output maps from the run step"

# shellcheck disable=SC2016 # single quotes are deliberate: literal workflow expressions
grep -q 'value: ${{ steps.preview.outputs.gate-status }}' preview/action.yml
ok $? "the gate-status output maps from the run step"

# The key reaches connect's `key:` input and nowhere else: the description
# prose may name the KOMIZO_DEPLOY_KEY fallback, but inputs.key is wired
# exactly once.
# shellcheck disable=SC2016 # single quotes are deliberate: a literal workflow expression
grep -qF 'key: ${{ inputs.key }}' preview/action.yml
ok $? "the key is passed to connect"

if [ "$(grep -c 'inputs.key' preview/action.yml)" -eq 1 ]; then
	pass=$((pass + 1))
else
	fail=$((fail + 1))
	printf 'FAIL  inputs.key is wired more than once\n'
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
