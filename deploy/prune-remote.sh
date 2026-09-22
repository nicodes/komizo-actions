#!/bin/sh
# deploy/prune-remote.sh - the host half of the deploy action's image
# self-prune. Runs ON THE HOST, over ssh, as the deploy account, after the
# deploy and health check have already succeeded.
#
# It removes superseded images of ONE app's image family and nothing else.
# The rules, in the order they are applied:
#
#   * only repositories under the app's own family prefix
#     (ghcr.io/<owner>/<project>-, derived runner-side from config-image)
#   * never a dangling <none> entry - its lineage cannot be proved, and the
#     default when lineage is unprovable is to keep
#   * never the current or the previous revision's tags (the rollback target)
#   * never an image any container - running or stopped - still uses
#   * targeted `docker image rm` only - no prune verb, no -a, no volumes
#
# Arguments (positional; charset-validated again here because the host
# executes the deletions, so it refuses anything that is not a plain
# reference rather than trusting the channel):
#   $1  family prefix, e.g. ghcr.io/you/blog-
#   $2  current revision (just deployed)
#   $3  previous revision (live before the deploy; empty on a first deploy)
set -u

prefix=$1
current=$2
previous=$3

refuse() {
	echo "prune: refusing to run: $1" >&2
	exit 1
}

case $prefix in
	'') refuse "empty family prefix" ;;
	*[!A-Za-z0-9._:/-]*) refuse "family prefix '$prefix' is not a plain image reference prefix" ;;
	*/?*-) ;;
	*) refuse "family prefix '$prefix' does not look like registry/owner/project-" ;;
esac
case $current in
	''|*[!A-Za-z0-9._-]*) refuse "current revision '$current' is not a plain image tag" ;;
esac
case $previous in
	'') ;;
	*[!A-Za-z0-9._-]*) refuse "previous revision '$previous' is not a plain image tag" ;;
esac

# Every image any container uses, resolved to an image ID, so a container
# referencing repo:tag, repo@digest or a bare ID all keep the same image.
# Failing to establish this fails CLOSED: with no reference list, nothing is
# provably unreferenced.
if ! containers=$(docker ps -aq); then
	refuse "cannot list containers (docker ps -aq failed) - nothing is provably unreferenced"
fi
used_ids=$(
	for cid in $containers; do
		docker inspect --format '{{.Image}}' "$cid" || exit 1
	done
) || refuse "cannot inspect containers - nothing is provably unreferenced"

if ! images=$(docker images --no-trunc --format '{{.Repository}} {{.Tag}} {{.ID}}'); then
	refuse "cannot list images"
fi

removed=0
failed=0
# A heredoc rather than a pipe, so the loop runs in THIS shell and the
# counters survive it.
while IFS=' ' read -r repo tag id; do
	# Only this app's family. The prefix ends in '-', so project 'blog'
	# does not match project 'blog2', and no other product is in scope.
	case $repo in
		"$prefix"*) ;;
		*) continue ;;
	esac
	# Dangling entries have no provable lineage; fail closed and keep them.
	case $tag in
		'<none>') continue ;;
	esac
	# The two revisions that matter: what is live now and what a rollback
	# redeploys.
	[ "$tag" = "$current" ] && continue
	[ -n "$previous" ] && [ "$tag" = "$previous" ] && continue
	# Anything a container still uses, in any reference form.
	if printf '%s\n' "$used_ids" | grep -qxF "$id"; then
		continue
	fi
	# One targeted removal per image. Docker itself refuses to remove an
	# image a container uses -- the backstop if one started using it between
	# the listing and now -- and a refusal warns rather than stopping the
	# rest of the sweep.
	if docker image rm "$repo:$tag" >/dev/null 2>&1; then
		echo "prune: removed $repo:$tag"
		removed=$((removed + 1))
	else
		echo "prune: could not remove $repo:$tag; leaving it" >&2
		failed=$((failed + 1))
	fi
done <<IMAGES
$images
IMAGES

echo "prune: done - removed $removed image(s) of family $prefix, $failed left after removal errors"
