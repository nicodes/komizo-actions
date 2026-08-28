#!/usr/bin/env bash
set -euo pipefail

: "${APP:=${KOMIZO_APP_NAME:-}}"

# Metadata `required: true` does not reject a missing composite-action input,
# so all three values are checked at runtime. These are exact literals rather
# than merely shell-safe character sets: this release provisions one app task
# and must not accidentally become a generic root/Docker command transport.
if [ "$APP" != "termcade" ]; then
  echo "::error::app must be termcade for this allowlisted task capability."
  exit 64
fi
if [ "$TASK" != "release-identity-backfill" ]; then
  echo "::error::task must be release-identity-backfill."
  exit 64
fi
case "$MODE" in
  dry-run|apply|constrain) ;;
  *)
    echo "::error::mode must be dry-run, apply, or constrain."
    exit 64
    ;;
esac

# connect owns authentication and host-key pinning and leaves only this alias.
# Check the seam explicitly so a missing connect step fails before any command
# is assembled. Match an alias token, not an arbitrary substring in the file.
ssh_config="${SSH_CONFIG:-$HOME/.ssh/config}"
if [ ! -f "$ssh_config" ] || ! awk '
  $1 == "Host" { for (i = 2; i <= NF; i++) if ($i == "deploy-target") found = 1 }
  END { exit !found }
' "$ssh_config"; then
  echo "::error::No deploy-target SSH alias. Run nicodes/komizo-actions/connect first."
  exit 1
fi

echo "Running allowlisted task: app=termcade task=release-identity-backfill mode=$MODE"

# Remote output is untrusted workflow text. Fence it so a compromised host
# cannot emit ::error::, ::set-output::, masking, or another workflow command.
fence="komizo-task-$(date +%s%N)-$RANDOM"
echo "::stop-commands::$fence"
rc=0
# The wrapper path, app and task are fixed literals. MODE has passed an exact
# three-value allowlist and therefore cannot alter remote-shell tokenization.
# No secret is in with:, argv, output, or this log line.
# shellcheck disable=SC2029 # the exact local mode literal is intentionally sent.
ssh deploy-target \
  "doas /usr/local/bin/task-termcade release-identity-backfill $MODE" 2>&1 || rc=$?
echo "::$fence::"
exit "$rc"
