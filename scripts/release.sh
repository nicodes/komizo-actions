#!/bin/sh
# scripts/release.sh - cut a komizo release with the composed actions pinned.
#
#   sh scripts/release.sh [<tag>]        # tag defaults to v0
#
# Run it on a clean tree, on the branch you want to release. It makes ONE
# commit, moves the tag, and prints the push command -- it never pushes.
#
# --- why this exists ------------------------------------------------------
#
# deploy is a composite that calls five sibling actions. GitHub
# resolves the caller's ref for deploy/action.yml ALONE; each inner `uses:`
# resolves independently, at whatever ref is written in the file. So while
# those said "@v0", a consumer who carefully pinned
#
#     uses: nicodes/komizo-actions/deploy@<sha>
#
# pinned one file and left five floating on a mutable tag. The pin looked
# complete and was not, which is worse than not pinning: it is the difference
# between a risk you have accepted and one you think you have closed.
#
# There is no way to inherit the caller's ref. `uses:` must be a string
# literal -- it accepts no ${{ }} expressions at all, so github.action_ref
# cannot be forwarded. Rewriting the refs at release time is the only thing
# that makes a SHA pin mean what it says.
#
# --- what it does ---------------------------------------------------------
#
# The five inner refs are rewritten to the CURRENT commit, then the result is
# committed. So the sub-actions come from the commit before the release, and
# deploy/action.yml comes from the release commit itself. Both are immutable,
# which is the whole point: `deploy@<release sha>` now resolves to exactly one
# set of six files, forever.
#
# The pins stay in the file between releases. That is deliberate -- reading
# deploy/action.yml tells you which sub-actions the last release shipped
# without consulting git. The cost is below.
#
# --- the one gotcha -------------------------------------------------------
#
# Between releases, `deploy@main` runs sub-actions from the LAST release, not
# from main. Testing a change to connect/ or healthcheck/ through deploy/ means
# either releasing first, or temporarily pointing the refs at your branch. It
# is the standard trade for deterministic pins and worth knowing before it
# confuses you.

set -eu

TAG="${1:-v0}"
REPO="nicodes/komizo-actions"
DEPLOY="deploy/action.yml"
# Every sibling deploy/ composes. Listed rather than globbed so a new action
# has to be added here on purpose: one that is composed but not pinned would
# float silently, which is exactly the bug this script exists to prevent.
SUBACTIONS="connect publish-config set-secrets set-version healthcheck"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

cd "$(dirname "$0")/.."
[ -f "$DEPLOY" ] || die "run this from the komizo-actions checkout ($DEPLOY not found)"

[ -z "$(git status --porcelain)" ] || die "working tree is not clean -- commit or stash first"

base="$(git rev-parse HEAD)"
branch="$(git rev-parse --abbrev-ref HEAD)"
[ "$branch" != "HEAD" ] || die "detached HEAD -- check out the branch you are releasing from"

# Every composed action must actually exist at the commit we are about to pin
# to, or the release ships a reference that resolves to nothing.
for a in $SUBACTIONS; do
	git cat-file -e "$base:$a/action.yml" 2>/dev/null \
		|| die "$a/action.yml does not exist at $base"
done

printf 'Releasing %s from %s (%s)\n\n' "$TAG" "$branch" "$(git rev-parse --short "$base")"

# Rewrite each ref to the base commit, whatever it currently points at -- a
# tag, a branch, or an older release's SHA.
for a in $SUBACTIONS; do
	sed -i -E "s|($REPO/$a)@[A-Za-z0-9._/-]+|\1@$base|g" "$DEPLOY"
	printf '  pinned %-16s -> %s\n' "$a" "$(git rev-parse --short "$base")"
done

# Nothing floating: catch a sibling that was composed but missing from the list
# above, which is the one way this script can silently under-deliver.
#
# Collected BEFORE reverting -- afterwards every ref is unpinned again, so the
# same grep would report all five and hide which one was actually the problem.
#
# Matched broadly -- any sibling, not just the listed ones -- so that adding an
# action to deploy/ without adding it to SUBACTIONS is caught here rather than
# shipping unpinned. deploy/ cannot compose itself, so a self-reference in a
# comment or example is excluded rather than reported.
unpinned="$(grep -nE "$REPO/[a-z-]+@" "$DEPLOY" \
	| grep -vE "$REPO/deploy@" | grep -v "@$base" || true)"
if [ -n "$unpinned" ]; then
	git checkout -- "$DEPLOY"
	printf '\n%s\n\n' "$unpinned" >&2
	die "the refs above were not pinned -- add them to SUBACTIONS and re-run (reverted)"
fi

if git diff --quiet -- "$DEPLOY"; then
	printf '\nAlready pinned to %s; nothing to commit.\n' "$(git rev-parse --short "$base")"
else
	git add "$DEPLOY"
	git commit -q -m "release $TAG: pin composed actions to ${base}"
	printf '\n  committed %s\n' "$(git rev-parse --short HEAD)"
fi

git tag -f "$TAG" >/dev/null
printf '  moved tag %s -> %s\n' "$TAG" "$(git rev-parse --short HEAD)"

cat <<EOF

Not pushed. To publish:

    git push origin $branch
    git push origin --force $TAG

Consumers can then pin the whole thing, and mean it:

    uses: $REPO/deploy@$(git rev-parse HEAD)
EOF
