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
#   * the exact ssh argv, with every argument single-quoted -- the box
#     binary's flags shape, --app/--pr, not positional
#   * the remote output fenced while it streams
#   * the outputs PARSED out of the fenced capture and validated, never
#     trusted unparsed; contract violations fail the step
#   * the deploy key never forwarded into the invocation, the log, or the
#     outputs
#   * ssh's exit code propagates -- a failed preview is a failed step
#
# The fixtures are the box binary's REAL output shapes, at komizo main
# (cmd/komizo-box/preview.go over box/preview.go):
#
#   up   prints the PreviewRecord as ONE JSON object -- fields v, app, pr,
#        project, db_name, gate_port, images, created_at, last_used,
#        route_file (db_password is json:"-", never marshalled)
#   down prints an informational sentence -- logged, never parsed
#   ls   prints the surviving records as a JSON array ([] when empty)
#
# and the preview domain comes from the host's knob, /etc/komizo/preview
# (key=value, DOMAIN the key, compiled default preview.gdam.dev), read over
# the same fenced path.
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
# A fake ssh records its argv and prints stubbed remote output, per command:
# the preview up/down invocation, the preview ls state read, and the preview
# knob read. Each case gets a fresh environment for the values the script
# reads, plus an ssh config carrying the deploy-target alias
# (SSH_CONFIG=/nonexistent removes it) and a temp GITHUB_OUTPUT.
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
case "$*" in
	*"preview ls"*)
		printf '%s\n' "${STUB_LS_OUTPUT:-[]}"
		exit "${STUB_LS_RC:-${STUB_SSH_RC:-0}}" ;;
	*"etc/komizo/preview"*)
		printf '%s\n' "${STUB_KNOB_OUTPUT:-}"
		exit "${STUB_KNOB_RC:-${STUB_SSH_RC:-0}}" ;;
	*)
		printf '%s\n' "${STUB_REMOTE_OUTPUT:-safe remote output}"
		exit "${STUB_SSH_RC:-0}" ;;
esac
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

no_outputs() { # <label> -- the last run wrote no step outputs at all
	if [ ! -s "$LAST_TMP/output" ]; then
		pass=$((pass + 1))
	else
		fail=$((fail + 1))
		printf 'FAIL  %s\n      outputs were written:\n' "$1"
		sed 's/^/      /' "$LAST_TMP/output"
	fi
}

IMG_API=ghcr.io/you/gdam-api:0123456789abcdef0123456789abcdef01234567
IMG_WEB=ghcr.io/you/gdam-web:0123456789abcdef0123456789abcdef01234567

# The real shapes, as the box binary prints them at komizo main.
RECORD_UP='{"v":1,"app":"gdam","pr":42,"project":"gdam-pr-42","db_name":"gdam_pr_42","gate_port":20000,"images":["'"$IMG_API"'","'"$IMG_WEB"'"],"created_at":"2026-09-22T03:04:05Z","last_used":"2026-09-22T03:04:05Z","route_file":"_preview-gdam-pr-42.caddy"}'
DOWN_SENTENCE='preview gdam-pr-42 is down: project, database gdam_pr_42 and route removed.'
KNOB_EXAMPLE='DOMAIN=preview.example.com'

echo "== the exact ssh argv =="

run_case 0 "up passes app, pr-number and every image as flags, single-quoted" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API $IMG_WEB" ACTION=up \
	STUB_REMOTE_OUTPUT="$RECORD_UP" STUB_KNOB_OUTPUT="$KNOB_EXAMPLE"
expected="deploy-target doas -n /usr/local/bin/komizo-preview up --app 'gdam' --pr '42' '$IMG_API' '$IMG_WEB'"
if grep -qxF "$expected" "$LAST_TMP/calls"; then
	pass=$((pass + 1))
else
	fail=$((fail + 1))
	printf 'FAIL  exact ssh argv for up\n      want: %s\n      got:  %s\n' "$expected" "$(cat "$LAST_TMP/calls")"
fi
expected="deploy-target if [ -r /etc/komizo/preview ]; then cat /etc/komizo/preview; fi"
if grep -qxF "$expected" "$LAST_TMP/calls"; then
	pass=$((pass + 1))
else
	fail=$((fail + 1))
	printf 'FAIL  exact ssh argv for the knob read\n      want: %s\n      got:  %s\n' "$expected" "$(cat "$LAST_TMP/calls")"
fi

run_case 0 "down names the preview as flags and passes no images" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=down \
	STUB_REMOTE_OUTPUT="$DOWN_SENTENCE"
expected="deploy-target doas -n /usr/local/bin/komizo-preview down --app 'gdam' --pr '42'"
if grep -qxF "$expected" "$LAST_TMP/calls"; then
	pass=$((pass + 1))
else
	fail=$((fail + 1))
	printf 'FAIL  exact ssh argv for down\n      want: %s\n      got:  %s\n' "$expected" "$(cat "$LAST_TMP/calls")"
fi
grep -qxF "deploy-target doas -n /usr/local/bin/komizo-preview ls" "$LAST_TMP/calls"
ok $? "down verifies the teardown against preview ls"

# The privileged primitive goes through doas to the root-owned wrapper --
# the doas rule's args match exactly, so the box permits the preview entry
# /usr/local/bin/komizo-preview (never the raw binary, which a rule could
# not constrain to preview-only) -- while the knob read stays the
# unprivileged deploy account's (EACCES is the compiled-default fallback).
grep -qF "doas -n /usr/local/bin/komizo-preview" "$LAST_TMP/calls"
ok $? "the primitive is invoked through doas to the preview wrapper"
if grep -qF "komizo-box" "$LAST_TMP/calls"; then
	fail=$((fail + 1))
	printf 'FAIL  the raw komizo-box binary is invoked -- the doas rule denies it\n'
else
	pass=$((pass + 1))
fi
run_case 0 "the knob read stays unprivileged" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT="$RECORD_UP" STUB_KNOB_OUTPUT="$KNOB_EXAMPLE"
if grep -F "etc/komizo/preview" "$LAST_TMP/calls" | grep -qF "doas"; then
	fail=$((fail + 1))
	printf 'FAIL  the knob read goes through doas\n'
else
	pass=$((pass + 1))
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
	STUB_REMOTE_OUTPUT="$RECORD_UP" STUB_KNOB_OUTPUT="$KNOB_EXAMPLE"
grep -qxF "deploy-target doas -n /usr/local/bin/komizo-preview up --app 'gdam' --pr '42' 'registry.internal:5000/you/gdam-api:abc'" "$LAST_TMP/calls"
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
	STUB_REMOTE_OUTPUT="$RECORD_UP
::error::forged by the host" \
	STUB_KNOB_OUTPUT="$KNOB_EXAMPLE"
case "$LAST_OUT" in
	*"::stop-commands::komizo-preview-"*"::error::forged by the host"*"::komizo-preview-"*)
		pass=$((pass + 1)) ;;
	*)
		fail=$((fail + 1))
		printf 'FAIL  forged workflow command was not inside the fence\n      %s\n' "$LAST_OUT" ;;
esac

echo "== up: the record is parsed, not trusted =="

run_case 0 "up parses the real record and derives all three outputs" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT="$RECORD_UP" STUB_KNOB_OUTPUT="$KNOB_EXAMPLE"
outputs_contain "preview-url=https://pr-42.preview.example.com"
outputs_contain "api-url=https://pr-42-api.preview.example.com"
outputs_contain "gate-status=up"

# The primitive's stderr notes ride along in the capture (2>&1); only the
# JSON record is parsed.
run_case 0 "the host's own log lines are ignored" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT="komizo-box preview: could not read /etc/komizo/preview, using defaults: open /etc/komizo/preview: permission denied
$RECORD_UP" \
	STUB_KNOB_OUTPUT="$KNOB_EXAMPLE"
outputs_contain "gate-status=up"

# An absent or unreadable knob is the box's compiled default, exactly as the
# box's own ReadPreviewKnob falls back.
run_case 0 "an absent knob means the compiled default domain" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT="$RECORD_UP"
outputs_contain "preview-url=https://pr-42.preview.gdam.dev"
outputs_contain "api-url=https://pr-42-api.preview.gdam.dev"

run_case 0 "a knob without a DOMAIN key means the compiled default domain" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT="$RECORD_UP" STUB_KNOB_OUTPUT="TTL_HOURS=48"
outputs_contain "preview-url=https://pr-42.preview.gdam.dev"

run_case 0 "an empty DOMAIN value means the compiled default domain" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT="$RECORD_UP" STUB_KNOB_OUTPUT="DOMAIN="
outputs_contain "preview-url=https://pr-42.preview.gdam.dev"

echo "== up: fail closed on unexpected shape =="

# The old assumed contract: a host emitting key=value where JSON is expected
# must fail the step, never silently produce outputs.
run_case 1 "a key=value line where the record should be fails closed" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT="preview-url=https://pr-42.preview.example.com
api-url=https://pr-42-api.preview.example.com
gate-status=open"
out_has "no JSON record" "the legacy shape is named in the error"
no_outputs "the legacy shape produced outputs"

run_case 1 "prose where the record should be fails closed" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT="preview is up, have fun"
no_outputs "prose produced outputs"

run_case 1 "two JSON records fail closed" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT="$RECORD_UP
$RECORD_UP"
no_outputs "a doubled record produced outputs"

run_case 1 "a record for another pull request fails closed" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT='{"v":1,"app":"gdam","pr":43,"project":"gdam-pr-43","db_name":"gdam_pr_43","gate_port":20001,"images":[],"created_at":"2026-09-22T03:04:05Z","last_used":"2026-09-22T03:04:05Z","route_file":"_preview-gdam-pr-43.caddy"}'
no_outputs "another PR's record produced outputs"

run_case 1 "a record for another app fails closed" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT='{"v":1,"app":"other","pr":42,"project":"other-pr-42","db_name":"other_pr_42","gate_port":20000,"images":[],"created_at":"2026-09-22T03:04:05Z","last_used":"2026-09-22T03:04:05Z","route_file":"_preview-other-pr-42.caddy"}'
no_outputs "another app's record produced outputs"

run_case 1 "a record missing gate_port fails closed" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT='{"v":1,"app":"gdam","pr":42,"project":"gdam-pr-42","db_name":"gdam_pr_42","images":[],"created_at":"2026-09-22T03:04:05Z","last_used":"2026-09-22T03:04:05Z","route_file":"_preview-gdam-pr-42.caddy"}'
no_outputs "a record without gate_port produced outputs"

# db_password is json:"-" on the real build: a record carrying one is not
# the pinned build's output, and a credential must never cross the wire.
run_case 1 "a record carrying db_password fails closed" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT='{"v":1,"app":"gdam","pr":42,"project":"gdam-pr-42","db_name":"gdam_pr_42","db_password":"hunter2","gate_port":20000,"images":[],"created_at":"2026-09-22T03:04:05Z","last_used":"2026-09-22T03:04:05Z","route_file":"_preview-gdam-pr-42.caddy"}'
out_has "db_password" "the credential is named in the error"
no_outputs "a credential-carrying record produced outputs"

run_case 1 "a gate_port that is not a number fails closed" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT='{"v":1,"app":"gdam","pr":42,"project":"gdam-pr-42","db_name":"gdam_pr_42","gate_port":"20000","images":[],"created_at":"2026-09-22T03:04:05Z","last_used":"2026-09-22T03:04:05Z","route_file":"_preview-gdam-pr-42.caddy"}'
no_outputs "a string gate_port produced outputs"

run_case 1 "a gate_port out of range fails closed" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT='{"v":1,"app":"gdam","pr":42,"project":"gdam-pr-42","db_name":"gdam_pr_42","gate_port":70000,"images":[],"created_at":"2026-09-22T03:04:05Z","last_used":"2026-09-22T03:04:05Z","route_file":"_preview-gdam-pr-42.caddy"}'
no_outputs "an out-of-range gate_port produced outputs"

run_case 1 "a db_name that is not a plain identifier fails closed" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT='{"v":1,"app":"gdam","pr":42,"project":"gdam-pr-42","db_name":"gdam_pr_42;id","gate_port":20000,"images":[],"created_at":"2026-09-22T03:04:05Z","last_used":"2026-09-22T03:04:05Z","route_file":"_preview-gdam-pr-42.caddy"}'
no_outputs "a hostile db_name produced outputs"

run_case 1 "a route_file with a path separator fails closed" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT='{"v":1,"app":"gdam","pr":42,"project":"gdam-pr-42","db_name":"gdam_pr_42","gate_port":20000,"images":[],"created_at":"2026-09-22T03:04:05Z","last_used":"2026-09-22T03:04:05Z","route_file":"../other.caddy"}'
no_outputs "a hostile route_file produced outputs"

run_case 1 "a knob domain that is not a domain fails closed" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT="$RECORD_UP" STUB_KNOB_OUTPUT="DOMAIN=not-a-domain"
no_outputs "a bad knob domain produced outputs"

run_case 1 "a knob domain with a space fails closed" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT="$RECORD_UP" STUB_KNOB_OUTPUT="DOMAIN=preview.example.com x"
no_outputs "a spaced knob domain produced outputs"

run_case 5 "a failed knob read fails the step" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT="$RECORD_UP" STUB_KNOB_RC=5
no_outputs "a failed knob read produced outputs"

echo "== down: the sentence is informational, the state is the check =="

run_case 0 "down logs the sentence and verifies against an empty ls" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=down \
	STUB_REMOTE_OUTPUT="$DOWN_SENTENCE" STUB_LS_OUTPUT='[]'
outputs_contain "gate-status=down"

# The sentence is never parsed: contract-looking text in it goes nowhere.
run_case 0 "contract-looking text in the down sentence is ignored" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=down \
	STUB_REMOTE_OUTPUT="preview gdam-pr-42 is down: project, database gdam_pr_42 and route removed. preview-url=https://evil.example.com" \
	STUB_LS_OUTPUT='[]'
outputs_contain "gate-status=down"
if grep -qF "evil.example.com" "$LAST_TMP/output"; then
	fail=$((fail + 1))
	printf 'FAIL  a URL out of the informational sentence became an output\n'
else
	pass=$((pass + 1))
fi

run_case 0 "down survives the host's own log lines around the ls array" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=down \
	STUB_REMOTE_OUTPUT="$DOWN_SENTENCE" \
	STUB_LS_OUTPUT="komizo-box preview: some note
[]"
outputs_contain "gate-status=down"

run_case 1 "a preview still in the ls output fails closed" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=down \
	STUB_REMOTE_OUTPUT="$DOWN_SENTENCE" STUB_LS_OUTPUT="[$RECORD_UP]"
out_has "still records gdam PR #42" "the surviving record is named in the error"
no_outputs "an unverified teardown produced outputs"

# Another preview may stay; only this one must be gone.
run_case 0 "another preview in the ls output is fine" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=down \
	STUB_REMOTE_OUTPUT="$DOWN_SENTENCE" \
	STUB_LS_OUTPUT='[{"v":1,"app":"gdam","pr":43,"project":"gdam-pr-43","db_name":"gdam_pr_43","gate_port":20001,"images":[],"created_at":"2026-09-22T03:04:05Z","last_used":"2026-09-22T03:04:05Z","route_file":"_preview-gdam-pr-43.caddy"}]'
outputs_contain "gate-status=down"

run_case 1 "a non-JSON ls output fails closed" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=down \
	STUB_REMOTE_OUTPUT="$DOWN_SENTENCE" STUB_LS_OUTPUT="gdam-pr-43"
no_outputs "a non-JSON ls produced outputs"

run_case 1 "a JSON object where the ls array should be fails closed" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=down \
	STUB_REMOTE_OUTPUT="$DOWN_SENTENCE" STUB_LS_OUTPUT="$RECORD_UP"
no_outputs "an object-shaped ls produced outputs"

run_case 9 "a failed ls read fails the step" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=down \
	STUB_REMOTE_OUTPUT="$DOWN_SENTENCE" STUB_LS_RC=9
no_outputs "a failed ls read produced outputs"

echo "== the deploy key goes nowhere =="

secret=credential-must-not-appear
run_case 0 "the deploy key is not forwarded" \
	APP=gdam PR_NUMBER=42 IMAGES="$IMG_API" ACTION=up \
	STUB_REMOTE_OUTPUT="$RECORD_UP" STUB_KNOB_OUTPUT="$KNOB_EXAMPLE" \
	KOMIZO_DEPLOY_KEY="$secret"
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

# shellcheck disable=SC2016 # single quotes are deliberate: literal workflow expressions
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
