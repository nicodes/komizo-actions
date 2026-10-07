#!/usr/bin/env bash
# The host owns retention policy. This pins the restricted transport boundary.
set -euo pipefail
cd "$(dirname "$0")/.."
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin"
cat > "$work/bin/ssh" <<'SSH'
#!/usr/bin/env bash
printf '%s\n' "$*" > "$SSH_CALLS"
printf '%s\n' "${REMOTE_OUTPUT:-safe output}"
exit "${SSH_RC:-0}"
SSH
chmod 755 "$work/bin/ssh"
run() {
	: > "$work/calls"
	out=$(env -i PATH="$work/bin:$PATH" SSH_CALLS="$work/calls" "$@" bash deploy/prune.sh)
}
run APP=blog
expected='-n -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 deploy-target doas /usr/local/bin/prune-blog'
grep -qxF -- "$expected" "$work/calls"
# Config-image can be omitted. Caller-selected image identities never cross
# the wire, including hostile values that the previous runner path consumed.
run APP=blog CONFIG_IMAGE='not-a-family' CURRENT='hostile;tag' PREVIOUS="hostile';tag" KOMIZO_DEPLOY_KEY=credential-must-not-appear
grep -qxF -- "$expected" "$work/calls"
if grep -qF credential-must-not-appear "$work/calls"; then echo "FAIL forwarded credential"; exit 1; fi
[[ "$out" != *credential-must-not-appear* ]]
run KOMIZO_APP_NAME=blog
grep -qxF -- "$expected" "$work/calls"
# shellcheck disable=SC2016 # literal hostile command-substitution input
for app in '' 'blog;id' 'blog/other' "blog'" 'blog name' 'blog.$(id)'; do
	run APP="$app"
	test ! -s "$work/calls"
	[[ "$out" == *'::warning::image prune skipped'* ]]
done
for rc in 1 127 255; do
	run APP=blog SSH_RC="$rc" REMOTE_OUTPUT='::error::forged by host'
	grep -qxF -- "$expected" "$work/calls"
	[[ "$out" == *'::stop-commands::komizo-prune-'*'::error::forged by host'*'::komizo-prune-'*'::warning::app-scoped image prune failed'* ]]
	[[ "$out" == *'no Docker fallback'* ]]
done
health_line=$(grep -n 'uses: nicodes/komizo-actions/health-check@' deploy/action.yml | cut -d: -f1)
prune_line=$(grep -n 'name: Prune superseded images' deploy/action.yml | cut -d: -f1)
test "$prune_line" -gt "$health_line"
# shellcheck disable=SC2016 # literal workflow expression
grep -qF 'APP: ${{ steps.cfg.outputs.app }}' deploy/action.yml
if grep -qE 'prune-remote|sh -s|docker|CURRENT|PREVIOUS|CONFIG_IMAGE' deploy/prune.sh; then echo 'FAIL generic fallback or caller-selected identities'; exit 1; fi
printf '%s\n' 'PASS app-scoped prune transport, negative controls and post-health wiring'
