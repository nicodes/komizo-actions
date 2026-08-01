#!/usr/bin/env bash
# tests/deploy-inputs.test.sh - drive deploy/resolve.sh and deploy/validate.sh
# over the input combinations the docs promise, and the ones they forbid.
#
# WHY THIS EXISTS. Everything else in this repository is a thin wrapper around
# ssh or docker: running it in a test means having a server and a registry, and
# the wrapper is not where the bugs were. These two scripts are different --
# they are pure functions of the inputs, they decide whether any of the rest
# runs, and they had no coverage at all.
#
# The bug that prompted this: the config-image rule was written as
# `[ -z "$CONFIG_COMPOSE" ] && [ -n "$CONFIG_IMAGE" ]`, which rejects every
# workflow using the `config:` (komizo.yml) form -- the documented, recommended
# one. It shipped, and the feature could not run at all. The `config: + image`
# row below is that case.
#
# Run: bash tests/deploy-inputs.test.sh

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

pass=0
fail=0

# run <script> <expected-rc> <label> [VAR=VALUE ...]
#
# Each case gets a fresh environment for the variables the scripts read, so a
# value left over from an earlier row cannot make a later one pass. GITHUB_OUTPUT
# and SSH_CONFIG point at temp files: the scripts write step outputs and look for
# a deploy-target block, and neither should touch the real ones.
#
# SSH_CONFIG_BODY, if set by the caller, is written into that config first --
# the seam for the one case that needs connect to have already run.
run() {
	local script="$1" want="$2" label="$3"
	shift 3
	local tmp out rc
	tmp="$(mktemp -d)"
	: > "$tmp/output"
	printf '%s' "${SSH_CONFIG_BODY:-}" > "$tmp/ssh_config"

	out="$(
		env -i \
			PATH="$PATH" HOME="$tmp" \
			GITHUB_OUTPUT="$tmp/output" SSH_CONFIG="$tmp/ssh_config" \
			HOST= APP= SECRET_NAMES= \
			CONFIG= CONFIG_COMPOSE= CONFIG_HOSTNAMES= CONFIG_IMAGE= \
			"$@" \
			bash "$script" 2>&1
	)"
	rc=$?

	if [ "$rc" -eq "$want" ]; then
		pass=$((pass + 1))
	else
		fail=$((fail + 1))
		printf 'FAIL  %s\n      expected rc=%s, got rc=%s\n' "$label" "$want" "$rc"
		[ -n "$out" ] && printf '      %s\n' "$out"
	fi
	LAST_OUTPUT_FILE="$tmp/output"
}

# outputs_contain <line> -- assert the last run wrote this step output.
outputs_contain() {
	if grep -qxF "$1" "$LAST_OUTPUT_FILE"; then
		pass=$((pass + 1))
	else
		fail=$((fail + 1))
		printf 'FAIL  expected step output %s, got:\n' "$1"
		sed 's/^/      /' "$LAST_OUTPUT_FILE"
	fi
}

V=deploy/validate.sh
R=deploy/resolve.sh

echo "== validate.sh: the two config forms =="

# THE REGRESSION. A komizo.yml plus the registry path it publishes to is the
# documented modern form, and it must be accepted.
run $V 0 "config: + config-image is accepted" \
	HOST=box.example.com APP=blog \
	CONFIG=deploy/komizo.yml CONFIG_IMAGE=ghcr.io/you/blog-config

run $V 0 "config-compose: + config-image is accepted" \
	HOST=box.example.com APP=blog \
	CONFIG_COMPOSE=deploy/compose.yml CONFIG_IMAGE=ghcr.io/you/blog-config

run $V 0 "config-compose: + hostnames + config-image is accepted" \
	HOST=box.example.com APP=blog \
	CONFIG_COMPOSE=deploy/compose.yml CONFIG_HOSTNAMES=deploy/hostnames \
	CONFIG_IMAGE=ghcr.io/you/blog-config

run $V 0 "no config at all is accepted (deploy without republishing)" \
	HOST=box.example.com APP=blog

echo "== validate.sh: contradictory config =="

run $V 1 "config: and config-compose: together are refused" \
	HOST=box.example.com APP=blog \
	CONFIG=deploy/komizo.yml CONFIG_COMPOSE=deploy/compose.yml \
	CONFIG_IMAGE=ghcr.io/you/blog-config

# publish-config ignores config-hostnames when config: is set, so accepting both
# would ship the manifest's names and silently drop the file.
run $V 1 "config: and config-hostnames: together are refused" \
	HOST=box.example.com APP=blog \
	CONFIG=deploy/komizo.yml CONFIG_HOSTNAMES=deploy/hostnames \
	CONFIG_IMAGE=ghcr.io/you/blog-config

run $V 1 "config: without config-image is refused" \
	HOST=box.example.com APP=blog CONFIG=deploy/komizo.yml

run $V 1 "config-compose: without config-image is refused" \
	HOST=box.example.com APP=blog CONFIG_COMPOSE=deploy/compose.yml

run $V 1 "config-image without any config is refused" \
	HOST=box.example.com APP=blog CONFIG_IMAGE=ghcr.io/you/blog-config

run $V 1 "config-hostnames without any config is refused" \
	HOST=box.example.com APP=blog CONFIG_HOSTNAMES=deploy/hostnames

echo "== validate.sh: app name =="

run $V 1 "a missing app name is refused" HOST=box.example.com
run $V 1 "an app name with a slash is refused" HOST=box.example.com APP=my/app
run $V 1 "an app name with a space is refused" HOST=box.example.com APP='my app'
run $V 0 "hyphens and underscores are allowed" HOST=box.example.com APP=my_app-2

echo "== validate.sh: no host means connect must have run =="

run $V 1 "no host and no deploy-target entry is refused" APP=blog

# With a deploy-target block in the ssh config, an empty host is the documented
# "connect already ran in an earlier step" case.
SSH_CONFIG_BODY=$'Host deploy-target\n    HostName box.example.com\n' \
	run $V 0 "no host but a deploy-target entry is accepted" APP=blog
unset SSH_CONFIG_BODY

echo "== resolve.sh =="

run $R 0 "a plain hostname passes through" HOST=box.example.com
outputs_contain "host=box.example.com"

# KOMIZO_SERVER_URL is what komizo tells people to store the address under, and
# pasting a whole URL into something named _URL is the obvious mistake.
run $R 0 "a scheme and path are stripped" HOST=https://box.example.com/deploy
outputs_contain "host=box.example.com"

run $R 0 "the KOMIZO_SERVER_URL fallback is used when host is empty" \
	KOMIZO_SERVER_URL=fallback.example.com
outputs_contain "host=fallback.example.com"

run $R 0 "an explicit host wins over the fallback" \
	HOST=explicit.example.com KOMIZO_SERVER_URL=fallback.example.com
outputs_contain "host=explicit.example.com"

run $R 0 "the KOMIZO_APP_NAME fallback is used when app is empty" \
	HOST=box.example.com KOMIZO_APP_NAME=blog
outputs_contain "app=blog"

# A newline would forge a second line in $GITHUB_OUTPUT, which is name=value per
# line -- so a host carrying "\nhas-secrets=false" would skip set-secrets.
run $R 1 "a newline in the host is refused" HOST=$'box.example.com\nhas-secrets=false'
run $R 1 "a newline in the app name is refused" HOST=box.example.com APP=$'blog\nx=y'

echo "== resolve.sh: which secrets exist =="

run $R 0 "no secrets anywhere reports has-secrets=false" HOST=box.example.com
outputs_contain "has-secrets=false"

run $R 0 "a KOMIZO_SECRET_ variable is detected" \
	HOST=box.example.com KOMIZO_SECRET_DATABASE_URL=postgres://x
outputs_contain "has-secrets=true"

run $R 0 "an explicit names list is detected" \
	HOST=box.example.com SECRET_NAMES=$'DATABASE_URL\nSTRIPE_KEY'
outputs_contain "has-secrets=true"

# Whitespace-only is not a list. It used to be, which pushed zero secrets and
# then failed in set-secrets with "nothing to push".
run $R 0 "a whitespace-only names list is not a list" \
	HOST=box.example.com SECRET_NAMES=$'   \n  \n'
outputs_contain "has-secrets=false"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
