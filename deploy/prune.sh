#!/usr/bin/env bash
# Run only the app-bound maintenance command installed by reviewed Komizo.
# It reads trusted deployment state itself. No Docker script, image family,
# tags or credentials are streamed to the restricted deployment account.
set -uo pipefail
APP="${APP:-${KOMIZO_APP_NAME:-}}"
case "$APP" in
	''|*[!A-Za-z0-9_-]*)
		echo '::warning::image prune skipped: app must be letters, digits, underscore or hyphen.'
		exit 0 ;;
esac
fence="komizo-prune-$(date +%s%N)-$RANDOM"
echo "::stop-commands::$fence"
rc=0
# The app name is charset-validated above; no caller-selected arguments.
# shellcheck disable=SC2029
ssh -n -o BatchMode=yes -o ConnectTimeout=10 \
	-o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
	deploy-target "doas /usr/local/bin/prune-$APP" 2>&1 || rc=$?
echo "::$fence::"
if [ "$rc" -ne 0 ]; then
	echo "::warning::app-scoped image prune failed (ssh exited $rc); deployment succeeded. Update the host's Komizo app setup if the command is missing. Images remain governed by the host retention command; no Docker fallback is attempted."
fi
exit 0
