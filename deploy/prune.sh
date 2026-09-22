#!/usr/bin/env bash
# deploy/prune.sh - the runner half of the deploy action's image self-prune.
#
# Superseded images accumulate on the host one deploy at a time until the
# disk fills. This removes them AFTER the deploy and health check have
# succeeded, scoped to the deploying app's own image family and keeping
# everything a rollback could need. It is deliberately not a composed
# sub-action and deliberately the last step: nothing here may fail the
# deploy, so every failure path warns (or notes) and exits 0.
#
# The deletion itself runs on the host - see prune-remote.sh, which this
# streams over ssh. The two are separate files so each half can be tested
# against fixtures - see tests/deploy-prune.test.sh.
#
# Inputs (environment):
#   CONFIG_IMAGE  the config-image: input - where the family prefix comes from
#   CURRENT       the version: input - the revision just deployed
#   PREVIOUS      the previous-version output of the activate step - what was
#                 live before, i.e. the rollback target. Host-supplied, so
#                 treated as untrusted and validated here.
#   GITHUB_ACTION_PATH  where prune-remote.sh lives (tests set it explicitly)
set -uo pipefail # deliberately no -e: no failure of this script is a deploy failure

: "${CONFIG_IMAGE:=}"
: "${CURRENT:=}"
: "${PREVIOUS:=}"
: "${GITHUB_ACTION_PATH:=$(cd "$(dirname "$0")" && pwd)}"

skip() { # <message> -- a skipped prune is never a failed deploy
	echo "$1"
	exit 0
}

# --- the family prefix ------------------------------------------------------
#
# ghcr.io/<owner>/<project>-config is the documented config-image shape, and
# the app's images are ghcr.io/<owner>/<project>-<component>, so the family
# prefix is the config image with its last component dropped. Derived from
# config-image rather than from the app name because the app name says
# nothing about the registry owner, and a guessed prefix prunes the wrong
# thing. Anything that does not fit the shape is unprovable lineage: skip.
case "$CONFIG_IMAGE" in
	"")
		skip "::notice::image prune skipped: no config-image input, so the app's image family cannot be derived."
		;;
	*[!A-Za-z0-9._:/-]*)
		skip "::warning::image prune skipped: config-image '$CONFIG_IMAGE' is not a plain image reference."
		;;
	*-config)
		prefix="${CONFIG_IMAGE%-config}-"
		;;
	*)
		skip "::notice::image prune skipped: config-image '$CONFIG_IMAGE' does not end in '-config', so the family prefix cannot be derived safely."
		;;
esac

# --- the two revisions to keep ----------------------------------------------
#
# activate already validates CURRENT; checked again because this script
# single-quotes it into the remote command.
case "$CURRENT" in
	""|*[!A-Za-z0-9._-]*)
		skip "::warning::image prune skipped: version '$CURRENT' is not a plain image tag."
		;;
esac
# PREVIOUS comes off the host's deploy output. If it is not a plain tag the
# rollback target cannot be identified, and pruning without keeping it could
# delete exactly what a rollback needs -- so a bad value skips the whole
# prune, not just that clause.
case "$PREVIOUS" in
	"") ;;
	*[!A-Za-z0-9._-]*)
		skip "::warning::image prune skipped: the previous version the host reported ('$PREVIOUS') is not a plain image tag, so the rollback target cannot be identified. Nothing was pruned."
		;;
esac

# --- run it on the host -----------------------------------------------------
#
# The remote script is fixed text with no interpolation; the three values
# arrive as positional arguments, single-quoted -- safe because the charsets
# above exclude quotes. Remote output is fenced: it comes from the host, and
# a compromised host could otherwise forge workflow commands in this job.
fence="komizo-prune-$(date +%s%N)-$RANDOM"
echo "::stop-commands::$fence"
rc=0
# shellcheck disable=SC2029 # the expansion is deliberate, and every value is charset-guarded above
ssh deploy-target "sh -s -- '$prefix' '$CURRENT' '$PREVIOUS'" \
	< "$GITHUB_ACTION_PATH/prune-remote.sh" 2>&1 || rc=$?
echo "::$fence::"
if [ "$rc" -ne 0 ]; then
	echo "::warning::image prune failed (ssh exited $rc); the deploy itself succeeded and is unaffected."
fi
exit 0
