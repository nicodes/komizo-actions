#!/usr/bin/env bash
set -euo pipefail
: "${VERSION:=}" "${DEPLOY_CMD:=}" "${REGISTRY:=}" "${REGISTRY_USER:=}" "${REGISTRY_TOKEN:=}"
: "${RUNNER_TEMP:=}" "${GITHUB_OUTPUT:=}" "${ACTION_LIBRARY_ROOT:=}"
# Rejected here as well as on the host: a bad value should fail with a
# clear message rather than surface as a remote sed error. Everything
# downstream single-quotes this value, which is only safe because the
# character set is constrained to exclude quotes entirely.
case "$VERSION" in
  ""|*[!A-Za-z0-9._-]*)
    echo "::error::version must be a plain image tag (letters, digits, dot, underscore, hyphen); got '$VERSION'."
    exit 1
    ;;
esac

set -euo pipefail

# Constrained for the same reason every other value here is: it is
# interpolated into the remote command string below, so the remote shell
# parses it before the host's own validation runs. It was the one input
# that went unchecked while version, registry and registry-user were all
# guarded. See set-secrets, which had the same gap.
case "$DEPLOY_CMD" in
  "doas /usr/local/bin/deploy-"*) ;;
  *)
    echo "::error::command must be the privileged deploy command komizo installed, e.g. 'doas /usr/local/bin/deploy-blog'; got '$DEPLOY_CMD'."
    exit 1 ;;
esac
case "${DEPLOY_CMD#doas /usr/local/bin/deploy-}" in
  ''|*[!A-Za-z0-9_-]*)
    echo "::error::command names an app that is not letters, digits, underscore or hyphen; got '$DEPLOY_CMD'."
    exit 1 ;;
esac

echo "Deploying $VERSION"

# Registry credentials go to the DEPLOY COMMAND, which runs as root, and
# not to a separate `ssh docker login`. The pull happens as root, so it
# reads root's ~/.docker/config.json; a login over this SSH session would
# write to the deploy user's home instead and the pull would still be
# anonymous. That fails only against a private registry, and looks like a
# missing image rather than an auth problem.
#
# The token travels on stdin, never as an argument — arguments are
# visible in the host's process list to every other user on the box. The
# host drops the credentials when the deploy exits, however it exits.
#
# REGISTRY and REGISTRY_USER are single-quoted straight into the remote
# command string, so the LOCAL charset check just below is what makes
# that quoting safe: the remote shell parses the whole string before the
# host's own validation ever runs, so a value containing a quote here
# would break out and execute as the deploy account. Constrained to
# characters that cannot close the quoting.
creds=""
if [ -n "$REGISTRY" ] && [ -n "$REGISTRY_TOKEN" ]; then
  if [ -z "$REGISTRY_USER" ]; then
    echo "::error::registry-token is set but registry-user is empty; the host cannot log in without a username."
    exit 1
  fi
  case "$REGISTRY" in
    *[!A-Za-z0-9._:/-]*)
      echo "::error::registry contains characters that are not valid in a registry host: '$REGISTRY'."
      exit 1 ;;
  esac
  # Brackets are admitted for bot logins: a post-merge dispatch runs
  # as github-actions[bot], and callers pass registry-user straight
  # from github.actor. Bot logins are valid GitHub usernames, and the
  # username is display/routing for a ghcr login with GITHUB_TOKEN --
  # the token is what authorizes. Brackets cannot close the single
  # quoting below, so the injection guard is unchanged.
  case "$REGISTRY_USER" in
    *[!]A-Za-z0-9._@[-]*)
      echo "::error::registry-user must be letters, digits, dot, underscore, at-sign, brackets or hyphen; got '$REGISTRY_USER'."
      exit 1 ;;
  esac
  creds=" '$REGISTRY' '$REGISTRY_USER'"
fi

# tee so the deploy's own output still reaches the log while we keep a
# copy to read the previous version out of.
#
# The remote output is fenced with an unpredictable stop-commands token:
# it comes from the host, and a compromised host could echo `::error::`
# or other workflow commands and forge annotations in this job. Inside
# the fence GitHub treats them as literal text.
#
# The pipeline's status is captured explicitly (|| rc=$?) rather than
# left to `set -e`, so the closing token is always emitted -- otherwise a
# failed deploy would leave workflow commands stopped for the rest of the
# job. pipefail (set above) makes a failed ssh, not just a failed tee,
# the status we act on.
# shellcheck source=activate/release-wire.sh
source "$(dirname "$0")/release-wire.sh"
trap '[ -z "$proof_file" ] || rm -f "$proof_file"' EXIT
komizo_prepare_release_wire
log="$RUNNER_TEMP/set-version.log"
fence="komizo-$(date +%s%N)-$RANDOM"
echo "::stop-commands::$fence"
rc=0
# The command string IS meant to expand here, on the client: DEPLOY_CMD
# is our fixed command name and VERSION is charset-validated above, so
# what crosses the wire is built deliberately rather than on the host.
# shellcheck disable=SC2029
komizo_release_wire \
  | ssh deploy-target "$DEPLOY_CMD '$VERSION'$creds" 2>&1 | tee "$log" || rc=$?
echo "::$fence::"
if [ "$rc" -ne 0 ]; then
  exit "$rc"
fi

# The host prints this because the deploy user cannot read .env itself.
# Absent on a host bootstrapped by an older version, so treat it as
# optional rather than failing a deploy that otherwise worked.
previous="$(sed -n 's/^deploy: previous-version=//p' "$log" | head -n 1)"
if [ -z "$previous" ]; then
  echo "::notice::Host did not report a previous version — nothing to roll back to, or the host predates this feature (re-run 'komizo add' on it to update)."
fi
{
  echo "version=$VERSION"
  echo "previous-version=$previous"
} >> "$GITHUB_OUTPUT"
