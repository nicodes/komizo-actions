#!/usr/bin/env bash
# scripts/host-secret-drift.sh - the half of the secret rule a repository
# cannot see.
#
# check-secrets reads a repository and answers "is this app asking for its
# secrets correctly". It cannot answer "is that what is actually on the box",
# and the two come apart in one direction by design: the host's set-secret
# writes a key and never deletes one. Drop a name from cd.yml and the value
# stays in secrets.env indefinitely -- unreferenced, unrotated, and readable by
# whichever service reads that file. Every stale credential found across this
# portfolio got there exactly that way.
#
# Reads key NAMES only. No value is fetched, printed or written anywhere, so
# this is safe to run and safe to paste. It needs root on the host because
# secrets.env is 0600 root -- which is the property that makes the file worth
# having.
#
#   scripts/host-secret-drift.sh root@myhost
#   scripts/host-secret-drift.sh root@myhost --app blog --repo ~/src/blog
#
# Without --repo it lists what each app has, which is the inventory. With
# --repo it runs check-secrets against that clone in drift mode, which is the
# judgement.

set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
host=""
app=""
repo=""
compose="deploy/compose.yml"
workflow=".github/workflows/cd.yml"

usage() {
	cat >&2 <<'USAGE'
usage: host-secret-drift.sh [user@]HOST [--app NAME] [--repo PATH]
                            [--compose PATH] [--workflow PATH]

  --app       one app instead of every app on the host
  --repo      a local clone to compare against; implies --app if it is omitted
              and the directory name is the app name
  --compose   compose path inside the clone (default deploy/compose.yml)
  --workflow  deploy workflow inside the clone (default .github/workflows/cd.yml)
USAGE
	exit 2
}

[ $# -gt 0 ] || usage
host="$1"
shift
while [ $# -gt 0 ]; do
	case "$1" in
		--app) app="${2:-}"; shift 2 ;;
		--repo) repo="${2:-}"; shift 2 ;;
		--compose) compose="${2:-}"; shift 2 ;;
		--workflow) workflow="${2:-}"; shift 2 ;;
		-h|--help) usage ;;
		*) echo "unknown argument: $1" >&2; usage ;;
	esac
done

if [ -n "$repo" ] && [ -z "$app" ]; then
	app="$(basename "$repo")"
fi

# One ssh round trip, and the remote side prints names only. `grep -o` on the
# key charset the host's set-secret enforces: anything that is not
# NAME=<value> at the start of a line -- a comment, a blank, a stray edit --
# is not a key and is not reported as one.
list_keys() {
	local target="$1"
	# shellcheck disable=SC2029  # the app name is interpolated deliberately
	ssh -o BatchMode=yes "$host" "
		set -eu
		for dir in /srv/${target}; do
			[ -d \"\$dir\" ] || continue
			name=\$(basename \"\$dir\")
			printf '%s\n' \"### \$name\"
			grep -oE '^[A-Za-z0-9_]+=' \"\$dir/secrets.env\" 2>/dev/null | tr -d '=' || true
		done
	"
}

if [ -z "$repo" ]; then
	list_keys "${app:-*}"
	exit 0
fi

keys="$(mktemp)"
trap 'rm -f "$keys"' EXIT
list_keys "$app" | grep -v '^### ' > "$keys"

echo "### $app: $(wc -l < "$keys") key(s) on $host, checked against $repo"
cd "$repo"
python3 "$here/check-secrets/check.py" \
	--compose "$compose" \
	--workflow "$workflow" \
	--host-keys "$keys"
