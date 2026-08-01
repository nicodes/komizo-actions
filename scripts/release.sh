#!/bin/sh
# scripts/release.sh - cut a komizo-actions release with the composed actions pinned.
#
#   sh scripts/release.sh              # bump the patch: v0.0.3 -> v0.0.4
#   sh scripts/release.sh minor        # v0.0.4 -> v0.1.0
#   sh scripts/release.sh major        # v0.1.0 -> v1.0.0
#   sh scripts/release.sh v0.2.0       # or name it outright
#
# Run it on a clean tree, on the branch you want to release. It makes ONE
# commit, creates the tag, and prints the push commands -- it never pushes.
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
# --- why the version, and not a moving v0 ---------------------------------
#
# Releases used to move a single `v0` tag, which meant `@v0` was a mutable ref
# pointing at whatever shipped last. That is the same class of thing this
# script exists to close: a consumer reading `@v0` in their workflow cannot
# tell which six files they are running, and a bad release reaches every
# repository the moment the tag moves, with no way to stay on the previous one
# short of finding its SHA by hand.
#
# So each release is its own immutable tag. `@v0.0.4` names one commit for
# ever, upgrading is a visible edit in a pull request, and rolling back is
# naming the version before it. A SHA pin still works and is still stronger --
# a tag can in principle be deleted and recreated, a SHA cannot -- but a
# version is the one people will actually use, so it should mean something.
#
# --- what it does ---------------------------------------------------------
#
# The five inner refs are rewritten to the CURRENT commit, then the result is
# committed. So the sub-actions come from the commit before the release, and
# deploy/action.yml comes from the release commit itself. Both are immutable,
# which is the whole point: `deploy@v0.0.4` now resolves to exactly one set of
# six files, forever.
#
# The pins stay in the file between releases. That is deliberate -- reading
# deploy/action.yml tells you which sub-actions the last release shipped
# without consulting git. The cost is below.
#
# --- the one gotcha -------------------------------------------------------
#
# Between releases, `deploy@main` runs sub-actions from the LAST release, not
# from main. Testing a change to connect/ or health-check/ through deploy/ means
# either releasing first, or temporarily pointing the refs at your branch. It
# is the standard trade for deterministic pins and worth knowing before it
# confuses you.

set -eu

BUMP="${1:-patch}"
REPO="nicodes/komizo-actions"
# Every action.yml that composes a sibling -- deploy/ today. Discovered rather
# than listed: one added and forgotten here would ship a floating ref, which is
# the one thing this script exists to prevent. Filled in after the cd below.
COMPOSED=""
# Every sibling deploy/ composes. Listed rather than globbed so a new action
# has to be added here on purpose: one that is composed but not pinned would
# float silently, which is exactly the bug this script exists to prevent.
SUBACTIONS="connect publish-config set-secrets activate health-check"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

cd "$(dirname "$0")/.."
[ -f deploy/action.yml ] || die "run this from the komizo-actions checkout (deploy/action.yml not found)"
# The glob never expands to a name starting with '-' (action directories are
# words like deploy/, connect/), and a ./ prefix would leak into the filenames
# in COMPOSED and break the self-reference regex below -- so the glob stays bare.
# shellcheck disable=SC2035
COMPOSED="$(grep -lE "$REPO/[a-z-]+@" */action.yml 2>/dev/null || true)"
[ -n "$COMPOSED" ] || die "no action composes a sibling -- has the repo layout changed?"

[ -z "$(git status --porcelain)" ] || die "working tree is not clean -- commit or stash first"

base="$(git rev-parse HEAD)"
branch="$(git rev-parse --abbrev-ref HEAD)"
[ "$branch" != "HEAD" ] || die "detached HEAD -- check out the branch you are releasing from"

# --- which version --------------------------------------------------------
#
# The tags are the source of truth, not a version file: there is nothing here
# to stamp a version INTO -- the artefact is the repository at a commit -- so a
# file recording one could only ever disagree with the tags.
#
# The old moving `v0` is deliberately not in this list. It matches 'v[0-9]*' but
# not the three-part pattern, so it is filtered out rather than parsed as a
# version and bumped into something meaningless.
git fetch --tags --force >/dev/null 2>&1 || true
latest="$(git tag --list 'v[0-9]*' | sed -E 's/^v//' \
	| grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -n 1 || true)"

case "$BUMP" in
	v[0-9]*)
		# Named outright. Checked for shape, because a typo here becomes a tag
		# that sorts wrongly for every release after it.
		version="${BUMP#v}"
		case "$version" in
			*[!0-9.]*|*..*) die "'$BUMP' is not a version like v0.1.0" ;;
		esac
		printf '%s' "$version" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$' \
			|| die "'$BUMP' is not a version like v0.1.0"
		;;
	major|minor|patch)
		if [ -z "$latest" ]; then
			# Nothing released under this scheme yet. Start at v0.0.1 rather
			# than at v0.0.0, which names a release nobody made.
			version="0.0.1"
		else
			major="${latest%%.*}"
			rest="${latest#*.}"
			minor="${rest%%.*}"
			patch="${rest#*.}"
			case "$BUMP" in
				major) major=$((major + 1)); minor=0; patch=0 ;;
				minor) minor=$((minor + 1)); patch=0 ;;
				patch) patch=$((patch + 1)) ;;
			esac
			version="${major}.${minor}.${patch}"
		fi
		;;
	*)
		die "usage: release.sh [patch|minor|major|vX.Y.Z]"
		;;
esac

TAG="v$version"
# Never move an existing tag. That is the property the version is for: a
# consumer who pinned it must keep getting the same six files.
if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
	die "$TAG already exists -- releases are immutable, pick the next version"
fi

# Every composed action must actually exist at the commit we are about to pin
# to, or the release ships a reference that resolves to nothing.
for a in $SUBACTIONS; do
	git cat-file -e "$base:$a/action.yml" 2>/dev/null \
		|| die "$a/action.yml does not exist at $base"
done

if [ -n "$latest" ]; then
	previous="v$latest"
else
	previous="none"
fi
printf 'Releasing %s from %s (%s)\n' "$TAG" "$branch" "$(git rev-parse --short "$base")"
printf '  previous: %s\n\n' "$previous"

# Rewrite each ref to the base commit, whatever it currently points at -- a
# tag, a branch, or an older release's SHA.
for f in $COMPOSED; do
	for a in $SUBACTIONS; do
		sed -i -E "s|($REPO/$a)@[A-Za-z0-9._/-]+|\1@$base|g" "$f"
	done
	printf '  pinned refs in %-24s -> %s\n' "$f" "$(git rev-parse --short "$base")"
done

# Nothing floating: catch a sibling that was composed but missing from the list
# above, which is the one way this script can silently under-deliver.
#
# Collected BEFORE reverting -- afterwards every ref is unpinned again, so the
# same grep would report all five and hide which one was actually the problem.
#
# Matched broadly -- any sibling, not just the listed ones -- so that composing
# an action without adding it to SUBACTIONS is caught here rather than shipping
# unpinned. An action cannot compose itself, so a self-reference in a comment or
# an example is excluded rather than reported.
unpinned=""
for f in $COMPOSED; do
	self="${f%/action.yml}"
	found="$(grep -nE "$REPO/[a-z-]+@" "$f" \
		| grep -vE "$REPO/$self@" | grep -v "@$base" || true)"
	[ -z "$found" ] || unpinned="$unpinned$f:$found
"
done
if [ -n "$unpinned" ]; then
	# COMPOSED is a newline-separated list of files; splitting it into separate
	# arguments is the intent, and this is POSIX sh with no array to hold them.
	# shellcheck disable=SC2086
	git checkout -- $COMPOSED
	printf '\n%s\n\n' "$unpinned" >&2
	die "the refs above were not pinned -- add them to SUBACTIONS and re-run (reverted)"
fi

# shellcheck disable=SC2086  # word-split COMPOSED into its filenames, as above
if git diff --quiet -- $COMPOSED; then
	printf '\n  already pinned to %s; tagging the commit as it stands\n' "$(git rev-parse --short "$base")"
else
	# shellcheck disable=SC2086
	git add $COMPOSED
	git commit -q -m "release $TAG: pin composed actions to ${base}"
	printf '\n  committed %s\n' "$(git rev-parse --short HEAD)"
fi

# Annotated, so `git describe` and the GitHub UI have a date and a message
# rather than a bare pointer. Never -f: see the existence check above.
git tag -a "$TAG" -m "komizo-actions $TAG" >/dev/null
printf '  tagged %s -> %s\n' "$TAG" "$(git rev-parse --short HEAD)"

cat <<EOF

Not pushed. To publish:

    git push origin $branch
    git push origin $TAG

Consumers then pin a version:

    uses: $REPO/deploy@$TAG

or the commit, which is stronger still -- a tag can in principle be deleted
and recreated, a commit cannot:

    uses: $REPO/deploy@$(git rev-parse HEAD)
EOF
