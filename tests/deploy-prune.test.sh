#!/usr/bin/env bash
# tests/deploy-prune.test.sh - pin the deploy action's image self-prune
# safety contract against fixture ssh and docker.
#
# WHY THIS EXISTS. The prune deletes data on production hosts, and it exists
# because one of them filled its disk with superseded images. Every clause of
# the contract is what keeps the cure from being worse than the disease:
#
#   * only the deploying app's own image family prefix
#   * never an image any container, running or stopped, still uses
#   * never other products' images
#   * never volumes
#   * never a blanket prune (no prune verb, no -a)
#   * keep the current and the previous revision (the rollback target)
#   * the prune's own failure warns and never fails the deploy
#
# Two halves, tested separately because they run on different machines:
# deploy/prune.sh on the runner (prefix derivation, charset guards, the exact
# ssh invocation, warn-don't-fail) and deploy/prune-remote.sh on the host
# (the keep-set computation and the targeted removals), plus the wiring in
# deploy/action.yml.
#
# Run: bash tests/deploy-prune.test.sh

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

# --- the runner half ---------------------------------------------------------

# run_runner <expected-rc> <label> [VAR=VALUE ...]
#
# A fake ssh records its argv and captures its stdin (which must be exactly
# prune-remote.sh, streamed verbatim). Each case gets a fresh environment for
# the values the script reads.
run_runner() {
	local want="$1" label="$2"
	shift 2
	local tmp out rc
	tmp="$(mktemp -d)"
	mkdir -p "$tmp/bin"
	cat > "$tmp/bin/ssh" <<'SSH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$SSH_CALLS"
cat > "$SSH_STDIN"
printf '%s\n' "${STUB_REMOTE_OUTPUT:-safe remote output}"
exit "${STUB_SSH_RC:-0}"
SSH
	chmod 755 "$tmp/bin/ssh"
	: > "$tmp/calls"
	out="$(
		env -i \
			PATH="$tmp/bin:$PATH" HOME="$tmp" \
			SSH_CALLS="$tmp/calls" SSH_STDIN="$tmp/stdin" \
			GITHUB_ACTION_PATH="$PWD/deploy" \
			CONFIG_IMAGE= CURRENT= PREVIOUS= \
			"$@" \
			bash deploy/prune.sh 2>&1
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

echo "== prune.sh: the family prefix comes from config-image =="

run_runner 0 "the documented shape deploys the prefix" \
	CONFIG_IMAGE=ghcr.io/you/blog-config CURRENT=111aaa PREVIOUS=000bbb
expected="deploy-target sh -s -- 'ghcr.io/you/blog-' '111aaa' '000bbb'"
if grep -qxF "$expected" "$LAST_TMP/calls"; then
	pass=$((pass + 1))
else
	fail=$((fail + 1))
	printf 'FAIL  exact ssh argv\n      want: %s\n      got:  %s\n' "$expected" "$(cat "$LAST_TMP/calls")"
fi
# The remote script crosses the wire on stdin, byte for byte.
cmp -s deploy/prune-remote.sh "$LAST_TMP/stdin"
ok $? "the host receives prune-remote.sh verbatim on stdin"

run_runner 0 "a registry with a port keeps its port in the prefix" \
	CONFIG_IMAGE=registry.internal:5000/you/blog-config CURRENT=111aaa PREVIOUS=
expected="deploy-target sh -s -- 'registry.internal:5000/you/blog-' '111aaa' ''"
if grep -qxF "$expected" "$LAST_TMP/calls"; then
	pass=$((pass + 1))
else
	fail=$((fail + 1))
	printf 'FAIL  exact ssh argv with port\n      want: %s\n      got:  %s\n' "$expected" "$(cat "$LAST_TMP/calls")"
fi

run_runner 0 "a first deploy prunes with an empty previous" \
	CONFIG_IMAGE=ghcr.io/you/blog-config CURRENT=111aaa PREVIOUS=
grep -qxF "deploy-target sh -s -- 'ghcr.io/you/blog-' '111aaa' ''" "$LAST_TMP/calls"
ok $? "previous is passed as an empty positional, not omitted"

echo "== prune.sh: unprovable lineage skips, and skips never fail =="

run_runner 0 "no config-image skips with a notice" CONFIG_IMAGE= CURRENT=111aaa
no_ssh "no config-image reached ssh"
out_has "::notice::" "no config-image notes the skip"

run_runner 0 "a config image not ending in -config skips" \
	CONFIG_IMAGE=ghcr.io/you/blog CURRENT=111aaa
no_ssh "non -config image reached ssh"
out_has "::notice::" "non -config image notes the skip"

run_runner 0 "a config image with a quote skips" \
	CONFIG_IMAGE="ghcr.io/you/blog'-config" CURRENT=111aaa
no_ssh "quoted config image reached ssh"
out_has "::warning::" "quoted config image warns"

echo "== prune.sh: the keep revisions are guarded before the wire =="

run_runner 0 "a version with shell in it skips" \
	CONFIG_IMAGE=ghcr.io/you/blog-config CURRENT='111;id'
no_ssh "bad version reached ssh"

# The previous version is host-supplied. A hostile value must not reach the
# remote shell -- and must not drop the previous revision from the keep set
# either, so the whole prune is skipped rather than just that clause.
run_runner 0 "a hostile previous version skips the whole prune" \
	CONFIG_IMAGE=ghcr.io/you/blog-config CURRENT=111aaa PREVIOUS="000'; id; '"
no_ssh "hostile previous version reached ssh"
out_has "Nothing was pruned" "hostile previous version explains the skip"

echo "== prune.sh: warn, never fail =="

run_runner 0 "an ssh failure still exits 0" \
	CONFIG_IMAGE=ghcr.io/you/blog-config CURRENT=111aaa STUB_SSH_RC=1
out_has "::warning::image prune failed" "ssh failure warns"
case "$LAST_OUT" in
	*"::stop-commands::komizo-prune-"*)
		pass=$((pass + 1)) ;;
	*)
		fail=$((fail + 1))
		printf 'FAIL  remote-output fence was not opened\n      %s\n' "$LAST_OUT" ;;
esac

# The host's output is untrusted; it must arrive fenced so workflow commands
# in it cannot execute.
run_runner 0 "host output is fenced" \
	CONFIG_IMAGE=ghcr.io/you/blog-config CURRENT=111aaa \
	STUB_REMOTE_OUTPUT='::error::forged by the host'
case "$LAST_OUT" in
	*"::stop-commands::komizo-prune-"*"::error::forged by the host"*"::komizo-prune-"*)
		pass=$((pass + 1)) ;;
	*)
		fail=$((fail + 1))
		printf 'FAIL  forged workflow command was not inside the fence\n      %s\n' "$LAST_OUT" ;;
esac

secret=credential-must-not-appear
run_runner 0 "the deploy key is not forwarded" \
	CONFIG_IMAGE=ghcr.io/you/blog-config CURRENT=111aaa KOMIZO_DEPLOY_KEY="$secret"
if ! grep -qF "$secret" "$LAST_TMP/calls" && ! printf '%s' "$LAST_OUT" | grep -qF "$secret"; then
	pass=$((pass + 1))
else
	fail=$((fail + 1))
	printf 'FAIL  the deploy key reached the prune invocation or its output\n'
fi

# --- the host half -----------------------------------------------------------

# Fixture state: a family (blog), a neighbour sharing the owner (blog2),
# another owner, an unrelated image, a dangling layer, and a stopped
# container still using an old family image.
STUB_IMAGES='ghcr.io/you/blog-web 111cur sha256:aaacur
ghcr.io/you/blog-web 000prev sha256:bbbprev
ghcr.io/you/blog-web 999old sha256:cccold
ghcr.io/you/blog-api 999old sha256:dddold
ghcr.io/you/blog-config 111cur sha256:eeecfgcur
ghcr.io/you/blog-config 555old sha256:fffcfgold
ghcr.io/you/blog2-web 999old sha256:gggother
ghcr.io/other/blog-web 999old sha256:hhhother
postgres 16-alpine sha256:iiipg
<none> <none> sha256:jjjdangle
ghcr.io/you/blog-worker 777old sha256:kkkused'

# run_remote <expected-rc> <label> <prefix> <current> <previous>
#
# A fake docker serves the fixture state and records every call; `image rm`
# calls are the deletions, recorded apart so they can be asserted exactly.
# Any docker verb the script is not allowed to use (prune, volume, system,
# rmi) makes the stub exit 2 -- which the sweep would surface.
run_remote() {
	local want="$1" label="$2" prefix="$3" current="$4" previous="$5"
	local tmp out rc
	tmp="$(mktemp -d)"
	mkdir -p "$tmp/bin"
	printf '%s\n' "$STUB_CID_IMAGES" > "$tmp/cid_images"
	cat > "$tmp/bin/docker" <<'DOCKER'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$DOCKER_CALLS"
case "$1" in
	ps)
		[ "${STUB_PS_RC:-0}" -eq 0 ] || exit "$STUB_PS_RC"
		printf '%s\n' "$STUB_PS" ;;
	inspect)
		cid="${!#}"
		awk -v c="$cid" '$1 == c { print $2; found=1 } END { exit !found }' "$STUB_CID_IMAGES_FILE" ;;
	images)
		printf '%s\n' "$STUB_IMAGES" ;;
	image)
		[ "$2" = "rm" ] || exit 2
		# shellcheck disable=SC2053 # a glob is exactly what is wanted here
		case "$3" in
			${STUB_RM_FAIL:-nothing-matches-this}) exit 1 ;;
		esac
		printf '%s\n' "$3" >> "$DOCKER_RM" ;;
	prune|volume|system|rmi|builder|container)
		exit 2 ;;
	*)
		exit 2 ;;
esac
DOCKER
	chmod 755 "$tmp/bin/docker"
	: > "$tmp/calls"
	: > "$tmp/rm"
	out="$(
		env -i \
			PATH="$tmp/bin:$PATH" HOME="$tmp" \
			DOCKER_CALLS="$tmp/calls" DOCKER_RM="$tmp/rm" \
			STUB_CID_IMAGES_FILE="$tmp/cid_images" \
			STUB_PS="${STUB_PS:-}" STUB_IMAGES="$STUB_IMAGES" \
			STUB_PS_RC="${STUB_PS_RC:-0}" STUB_RM_FAIL="${STUB_RM_FAIL:-}" \
			sh deploy/prune-remote.sh "$prefix" "$current" "$previous" 2>&1
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

removed_exactly() { # <label> <expected-lines...>
	local label="$1"
	shift
	local expected
	expected="$(printf '%s\n' "$@" | sort)"
	if [ "$(sort "$LAST_TMP/rm")" = "$expected" ]; then
		pass=$((pass + 1))
	else
		fail=$((fail + 1))
		printf 'FAIL  %s\n      want removals:\n%s\n      got:\n%s\n' \
			"$label" "$expected" "$(sort "$LAST_TMP/rm")"
	fi
}

never_removed() { # <substring> <label>
	if grep -qF "$1" "$LAST_TMP/rm"; then
		fail=$((fail + 1))
		printf 'FAIL  %s\n      removals were:\n%s\n' "$2" "$(cat "$LAST_TMP/rm")"
	else
		pass=$((pass + 1))
	fi
}

no_blanket_verbs() { # <label> -- no prune, no -a, no volumes, no system
	if grep -qE 'prune|volume|system|rmi| -a( |$)' "$LAST_TMP/calls"; then
		fail=$((fail + 1))
		printf 'FAIL  %s\n      docker calls were:\n%s\n' "$1" "$(cat "$LAST_TMP/calls")"
	else
		pass=$((pass + 1))
	fi
}

no_docker() { # <label> -- validation refused before touching the daemon
	if [ ! -s "$LAST_TMP/calls" ]; then
		pass=$((pass + 1))
	else
		fail=$((fail + 1))
		printf 'FAIL  %s\n      docker was called: %s\n' "$1" "$(cat "$LAST_TMP/calls")"
	fi
}

STUB_PS=$'c1\nc2'
STUB_CID_IMAGES='c1 sha256:aaacur
c2 sha256:kkkused'

echo "== prune-remote.sh: the contract, clause by clause =="

run_remote 0 "a normal sweep" 'ghcr.io/you/blog-' 111cur 000prev

# Only the family's superseded tags go.
removed_exactly "only superseded family tags are removed" \
	'ghcr.io/you/blog-web:999old' \
	'ghcr.io/you/blog-api:999old' \
	'ghcr.io/you/blog-config:555old'
# Clause: keep the current revision's images.
never_removed ':111cur' "the current revision is kept"
# Clause: keep the previous revision -- the rollback target.
never_removed ':000prev' "the previous revision (rollback target) is kept"
# Clause: never an image a container uses, even stopped ones.
never_removed 'blog-worker' "a stopped container's image is kept"
# Clause: never other products' images -- same owner, different project...
never_removed 'blog2-web' "a sibling project's image is kept"
# ...and a different owner entirely.
never_removed 'ghcr.io/other' "another owner's image is kept"
never_removed 'postgres' "an unrelated image is kept"
# Clause: dangling lineage is unprovable, so it is kept (fail closed).
never_removed 'sha256:jjjdangle' "a dangling image is kept"
# Clause: no blanket prune, no -a, no volumes, ever.
no_blanket_verbs "only targeted 'image rm <ref>' calls were made"

echo "== prune-remote.sh: the keep set is exactly current + previous =="

# With no previous revision (a first deploy), what was the previous tag is no
# longer protected -- the one deliberate contrast pinning why previous is
# passed at all.
run_remote 0 "a first deploy has no rollback target to keep" 'ghcr.io/you/blog-' 111cur ''
removed_exactly "without a previous revision its tags are superseded" \
	'ghcr.io/you/blog-web:999old' \
	'ghcr.io/you/blog-api:999old' \
	'ghcr.io/you/blog-config:555old' \
	'ghcr.io/you/blog-web:000prev'
never_removed ':111cur' "the current revision is still kept on a first deploy"
never_removed 'blog-worker' "a stopped container's image is still kept"

echo "== prune-remote.sh: fail closed when nothing is provable =="

STUB_PS_RC=1 run_remote 1 "a failed container listing prunes nothing" 'ghcr.io/you/blog-' 111cur 000prev
unset STUB_PS_RC
if [ ! -s "$LAST_TMP/rm" ]; then
	pass=$((pass + 1))
else
	fail=$((fail + 1))
	printf 'FAIL  removals happened after a failed container listing\n'
fi

# A container whose image cannot be resolved is the same class: refuse.
STUB_PS=$'c1\nc2\nc3' run_remote 1 "an uninspectable container prunes nothing" 'ghcr.io/you/blog-' 111cur 000prev
STUB_PS=$'c1\nc2'
if [ ! -s "$LAST_TMP/rm" ]; then
	pass=$((pass + 1))
else
	fail=$((fail + 1))
	printf 'FAIL  removals happened after a failed container inspect\n'
fi

echo "== prune-remote.sh: the host re-validates its arguments =="

run_remote 1 "a prefix without a registry path is refused" 'blog-' 111cur 000prev
no_docker "a bad prefix reached docker"

run_remote 1 "a prefix with shell in it is refused" "ghcr.io/you/blog-'; id; '" 111cur 000prev
no_docker "a hostile prefix reached docker"

run_remote 1 "a current tag with shell in it is refused" 'ghcr.io/you/blog-' '111;id' 000prev
no_docker "a hostile current tag reached docker"

# shellcheck disable=SC2016 # the backticks are a LITERAL test input
run_remote 1 "a previous tag with shell in it is refused" 'ghcr.io/you/blog-' 111cur '000`id`'
no_docker "a hostile previous tag reached docker"

echo "== prune-remote.sh: one failure does not stop the sweep =="

STUB_RM_FAIL='ghcr.io/you/blog-api:999old' \
	run_remote 0 "a refused removal warns and the rest still go" 'ghcr.io/you/blog-' 111cur 000prev
unset STUB_RM_FAIL
removed_exactly "the other superseded tags are still removed" \
	'ghcr.io/you/blog-web:999old' \
	'ghcr.io/you/blog-config:555old'
case "$LAST_OUT" in
	*"could not remove ghcr.io/you/blog-api:999old"*)
		pass=$((pass + 1)) ;;
	*)
		fail=$((fail + 1))
		printf 'FAIL  the refused removal was not reported\n      %s\n' "$LAST_OUT" ;;
esac

echo "== the composite wiring =="

# The prune must come after the health check, and must read the previous
# revision from the activate step's output -- the value read off the host
# before the deploy flipped anything.
health_line=$(grep -n 'uses: nicodes/komizo-actions/health-check@' deploy/action.yml | cut -d: -f1)
prune_line=$(grep -n 'name: Prune superseded images' deploy/action.yml | cut -d: -f1)
if [ -n "$health_line" ] && [ -n "$prune_line" ] && [ "$prune_line" -gt "$health_line" ]; then
	pass=$((pass + 1))
else
	fail=$((fail + 1))
	printf 'FAIL  the prune step is not after the health check in deploy/action.yml\n'
fi

# shellcheck disable=SC2016 # single quotes are deliberate: a literal workflow expression
grep -q 'PREVIOUS: \${{ steps.version.outputs.previous-version }}' deploy/action.yml
ok $? "the prune keeps the previous revision from the activate output"

# shellcheck disable=SC2016 # single quotes are deliberate: a literal workflow expression
grep -q 'CURRENT: \${{ inputs.version }}' deploy/action.yml
ok $? "the prune keeps the deploying revision"

# No step in the composite may introduce a blanket prune of its own.
if grep -nE 'docker (image )?prune|docker system|docker volume|--all| -a( |$)' deploy/action.yml; then
	fail=$((fail + 1))
	printf 'FAIL  deploy/action.yml gained a blanket prune\n'
else
	pass=$((pass + 1))
fi

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
