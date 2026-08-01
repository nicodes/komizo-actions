#!/usr/bin/env bash
# tests/parse-config.test.sh - drive publish-config/parse-config.py over the
# manifests it should accept and the ones it must refuse.
#
# The values in a komizo.yml end up as lines in the file the host parses and
# turns into reverse-proxy routes, so "what does an odd value do here" is a
# question with a real answer on somebody's server. It used to be str(): a YAML
# boolean became the word "True", and a newline became a hostname claim nobody
# had made.
#
# Run: bash tests/parse-config.test.sh

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

if ! python3 -c 'import yaml' 2>/dev/null; then
	echo "SKIP: PyYAML is not installed"
	exit 0
fi

P=publish-config/parse-config.py
pass=0
fail=0
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# check <expected-rc> <label> <manifest>
check() {
	local want="$1" label="$2" manifest="$3"
	printf '%s' "$manifest" > "$tmp/komizo.yml"
	: > "$tmp/hostnames"
	local out rc
	out="$(python3 "$P" "$tmp/komizo.yml" "$tmp/hostnames" 2>&1)"
	rc=$?
	if [ "$rc" -eq "$want" ]; then
		pass=$((pass + 1))
	else
		fail=$((fail + 1))
		printf 'FAIL  %s\n      expected rc=%s, got rc=%s\n      %s\n' \
			"$label" "$want" "$rc" "$out"
	fi
	LAST_OUT="$out"
}

# hostnames_are <expected file contents> -- assert what the last run emitted.
hostnames_are() {
	local want="$1" got
	got="$(cat "$tmp/hostnames")"
	if [ "$got" = "$want" ]; then
		pass=$((pass + 1))
	else
		fail=$((fail + 1))
		printf 'FAIL  hostnames mismatch\n      want: %q\n      got:  %q\n' "$want" "$got"
	fi
}

echo "== accepted manifests =="

check 0 "a bare name" 'hostnames:
  - app.example.com
'
hostnames_are 'app.example.com'

check 0 "a name with a container" 'hostnames:
  - name: api.example.com
    container: api
'
hostnames_are 'api.example.com -> api'

check 0 "a wildcard with a container and a tls mode" 'hostnames:
  - name: "*.preview.example.com"
    container: api
    tls: on-demand
'
hostnames_are '*.preview.example.com -> api on-demand'

check 0 "a tls mode with no container" 'hostnames:
  - name: "*.preview.example.com"
    tls: on-demand
'
hostnames_are '*.preview.example.com on-demand'

check 0 "no hostnames at all (a worker)" 'compose: compose.yml
'
hostnames_are ''

check 0 "the compose path is echoed, resolved beside the manifest" 'compose: prod.yml
'
if [ "$LAST_OUT" = "$tmp/prod.yml" ]; then
	pass=$((pass + 1))
else
	fail=$((fail + 1))
	printf 'FAIL  compose path: want %q, got %q\n' "$tmp/prod.yml" "$LAST_OUT"
fi

check 0 "compose defaults to compose.yml" 'hostnames:
  - app.example.com
'
if [ "$LAST_OUT" = "$tmp/compose.yml" ]; then
	pass=$((pass + 1))
else
	fail=$((fail + 1))
	printf 'FAIL  default compose path: got %q\n' "$LAST_OUT"
fi

echo "== YAML types that are not text =="

# THE REGRESSION. `tls: yes` parses as a boolean, and str() rendered it "True" --
# so the host received "name -> api True" and complained about arrows.
check 1 "tls: yes is refused as a boolean, not rendered as True" 'hostnames:
  - name: api.example.com
    container: api
    tls: yes
'
case "$LAST_OUT" in
	*boolean*) pass=$((pass + 1)) ;;
	*) fail=$((fail + 1)); printf 'FAIL  expected the message to name the boolean, got: %s\n' "$LAST_OUT" ;;
esac

check 1 "tls: on is refused (also a YAML boolean)" 'hostnames:
  - name: api.example.com
    tls: on
'
check 1 "a numeric container is refused" 'hostnames:
  - name: api.example.com
    container: 3
'

echo "== values that would forge extra lines =="

# A newline used to emit two lines, the second of which the downstream shell
# validator would have happily accepted as a hostname.
check 1 "a newline in a name is refused" 'hostnames:
  - "app.example.com\nevil.example.com"
'
check 1 "a newline in a container is refused" 'hostnames:
  - name: app.example.com
    container: "api\nevil.example.com"
'
check 1 "a newline in the compose path is refused" 'compose: "a.yml\nb.yml"
'

echo "== charsets =="

check 1 "a space in a name is refused" 'hostnames:
  - "app example.com"
'
check 1 "a name with an interior wildcard is refused" 'hostnames:
  - "a.*.example.com"
'
check 1 "a bare wildcard is refused" 'hostnames:
  - "*"
'
check 1 "a container with a slash is refused" 'hostnames:
  - name: app.example.com
    container: "api/x"
'
check 1 "an unknown tls mode is refused" 'hostnames:
  - name: app.example.com
    tls: sometimes
'

echo "== shape =="

check 1 "a mapping with no name is refused" 'hostnames:
  - container: api
'
check 1 "an unknown key is refused" 'hostnames:
  - name: app.example.com
    contaner: api
'
check 1 "a duplicate name is refused" 'hostnames:
  - app.example.com
  - name: app.example.com
    container: api
'
check 1 "hostnames as a mapping is refused" 'hostnames:
  app: example.com
'
check 1 "a top-level list is refused" '- app.example.com
'
check 1 "malformed YAML is refused" 'hostnames: [unclosed
'

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
