#!/usr/bin/env bash
# set-service-env/abort-if-staged.sh - deploy's failure and cancellation cleanup.
#
# Composite actions cannot declare a post: step. Deploy runs this under
# if: always() so a failed activate, a failed health check, or a cancellation
# that still lets the runner execute the step will abort a stage this job
# actually completed. It does not abort when:
#
#   * nothing was staged (no marker) and stage did not succeed or get cancelled
#   * confirm already completed (confirmed marker) — restoring the link after
#     a confirm the host accepted would undo the generation the deploy proved
#
# A cancellation during the stage ssh itself leaves no marker. This script
# fails loud in that case and says so. It cannot close the host's cancellation
# lease, and it does not delete a retained previous generation. Abort restores
# the link only; it does not roll back containers, database role credentials
# or migrations.
#
# Inputs (environment):
#   SCOPED_STAGE_OUTCOME   the stage step's outcome (success|failure|cancelled|skipped)
#   SERVICE_ENV_PROFILE, APP, SSH_CONFIG, RUNNER_TEMP  as for run.sh
set -euo pipefail

# shellcheck source=set-service-env/lib.sh
source "$(dirname "$0")/lib.sh"

: "${SCOPED_STAGE_OUTCOME:=}"

if [ -f "$(scoped_confirmed_marker)" ]; then
	echo "Scoped env already confirmed; not aborting."
	exit 0
fi

if [ ! -f "$(scoped_staged_marker)" ]; then
	case "$SCOPED_STAGE_OUTCOME" in
		success)
			scoped_refuse "scoped env stage was recorded as successful but the staged marker is missing; not aborting blind." ;;
		cancelled)
			scoped_refuse "scoped env stage was cancelled before this job recorded success. The host may still hold a staged generation. The cancellation lease and previous-generation retention are unresolved on the host; this action cannot prove the link was restored. Inspect with 'doas /usr/local/bin/set-scoped-env-fieldsofrevik status' and abort if a stage is pending." ;;
		*)
			exit 0 ;;
	esac
fi

OPERATION=abort bash "$(dirname "$0")/run.sh"
