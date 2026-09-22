#!/usr/bin/env bash
# preview/run.sh - the preview action's one step: validate the inputs, invoke
# the host's komizo-box preview primitive over the deploy-target alias, and
# parse the outputs out of what it prints.
#
# A file rather than a `run:` block so a test can drive it over the whole
# input matrix -- see tests/preview.test.sh. The safety contract, the same bar
# as deploy's prune:
#
#   * no interpolation into the remote command: every value is
#     charset-validated below and single-quoted into the argv, so a quote or a
#     dollar sign in any input stops the run rather than reaching a shell
#   * the remote output is untrusted: it is fenced while it streams, and the
#     outputs are PARSED out of it -- each value validated before it is
#     written to $GITHUB_OUTPUT, never trusted unparsed
#   * fail closed: a missing deploy-target alias, a malformed input, a failed
#     ssh, or output that does not follow the contract all stop the step
#   * the deploy key is never touched: connect owns it, this script does not
#     read it, forward it, or log it
#
# The primitive's invocation shape is
#   komizo-box preview up   <app> <pr-number> <image...>
#   komizo-box preview down <app> <pr-number>
# and its contract output is `key=value` lines carrying preview-url, api-url
# and gate-status. (The komizo repo's main carries no preview docs yet; this
# shape is the stated Phase-2 interface -- see docs/actions.md.)
#
# Inputs (environment):
#   APP         the product slug
#   PR_NUMBER   the pull request number
#   IMAGES      space-separated image refs (passed to the primitive on up)
#   ACTION      up | down
#   SSH_CONFIG  ssh config to look for deploy-target in; defaults to
#               ~/.ssh/config (a seam for tests)
#   GITHUB_OUTPUT  where the parsed outputs go
set -euo pipefail

: "${APP:=}"
: "${PR_NUMBER:=}"
: "${IMAGES:=}"
: "${ACTION:=}"
: "${SSH_CONFIG:=$HOME/.ssh/config}"

refuse() {
	echo "::error::$1"
	exit 1
}

# Metadata `required: true` does not reject a missing composite-action input,
# so every required input is checked at runtime (see run-task/run.sh).
case "$ACTION" in
	up|down) ;;
	*) refuse "action must be 'up' or 'down'; got '$ACTION'." ;;
esac

# The product slug. The host derives per-preview names from it -- the database,
# the routes, the state directory -- so it is constrained to the release
# naming's own slug shape (the rule publish/validate.sh checks for PROJECT),
# not merely to characters a shell tolerates.
if [ -z "$APP" ]; then
	refuse "app is empty. Pass the product slug the images publish under, e.g. gdam."
fi
if [[ ! "$APP" =~ ^[a-z][a-z0-9-]*$ ]]; then
	refuse "app must be a lowercase slug: a leading letter, then letters, digits or hyphens; got '$APP'."
fi

# A positive integer, no leading zeros: it names the preview (pr-<N>) and a
# padded or signed value would name a different one than the pull request.
if [[ ! "$PR_NUMBER" =~ ^[1-9][0-9]*$ ]]; then
	refuse "pr-number must be a positive integer; got '$PR_NUMBER'."
fi

# Split on whitespace without globbing -- read does not expand wildcards the
# way a bare `for x in $IMAGES` would against the files in the workspace.
# Whitespace-only is not a list (same call deploy/resolve.sh makes).
read -r -a image_words <<<"$IMAGES"
if [ "${#image_words[@]}" -eq 0 ]; then
	refuse "images is empty. Pass the space-separated image refs for the preview, e.g. \"ghcr.io/you/myapp-api:<sha> ghcr.io/you/myapp-web:<sha>\"."
fi
# Each ref must match the products' release naming,
# ghcr.io/<owner>/<project>-<component>:<sha> -- the same family shape the
# deploy prune derives from config-image. Checked even on down, when the refs
# are not passed on: a malformed value is a workflow bug, and failing here is
# cheaper than a teardown aimed at a preview the workflow did not mean.
for ref in "${image_words[@]}"; do
	case "$ref" in
		*[!A-Za-z0-9._:/-]*)
			refuse "image ref '$ref' contains characters outside a plain image reference." ;;
	esac
	if [[ ! "$ref" =~ ^[A-Za-z0-9._-]+(:[0-9]+)?(/[A-Za-z0-9._-]+)+:[A-Za-z0-9._-]+$ ]]; then
		refuse "image ref '$ref' is not of the form registry/owner/name:tag."
	fi
	# The repository name itself carries the project-component split, which is
	# what scopes the preview -- and the host's prune family -- to one product.
	image_name="${ref##*/}"
	image_name="${image_name%%:*}"
	case "$image_name" in
		*-*) ;;
		*) refuse "image ref '$ref' does not follow the <project>-<component> release naming." ;;
	esac
done

# connect owns authentication and host-key pinning and leaves only this alias.
# Check the seam explicitly so a missing connect step fails before any command
# is assembled. Match an alias token, not an arbitrary substring in the file.
if [ ! -f "$SSH_CONFIG" ] || ! awk '
	$1 == "Host" { for (i = 2; i <= NF; i++) if ($i == "deploy-target") found = 1 }
	END { exit !found }
' "$SSH_CONFIG"; then
	refuse "No deploy-target SSH alias. Pass host:/key:/known-hosts: to this action, or run nicodes/komizo-actions/connect in an earlier step."
fi

# Remote output is untrusted workflow text. Fence it so a compromised host
# cannot emit ::error::, masking, or another workflow command; the parse below
# is what any value crosses the fence through.
out_file="$(mktemp)"
fence="komizo-preview-$(date +%s%N)-$RANDOM"
echo "::stop-commands::$fence"
rc=0
if [ "$ACTION" = "up" ]; then
	# Every argument single-quoted -- safe because the charsets above exclude
	# quotes. The images ride along as positional arguments, one per ref.
	quoted=""
	for ref in "${image_words[@]}"; do
		quoted="$quoted '$ref'"
	done
	# shellcheck disable=SC2029 # the expansion is deliberate, and every value is charset-guarded above
	ssh deploy-target "komizo-box preview up '$APP' '$PR_NUMBER'$quoted" 2>&1 | tee "$out_file" || rc=$?
else
	# Down names the preview; the host's own state records which images ran
	# under it, so the refs validated above stay runner-side.
	# shellcheck disable=SC2029 # the expansion is deliberate, and every value is charset-guarded above
	ssh deploy-target "komizo-box preview down '$APP' '$PR_NUMBER'" 2>&1 | tee "$out_file" || rc=$?
fi
echo "::$fence::"
if [ "$rc" -ne 0 ]; then
	echo "::error::komizo-box preview $ACTION failed on the host (ssh exited $rc)."
	exit "$rc"
fi

# Parse the outputs out of the fenced capture. The contract is `key=value`
# lines; everything else the primitive prints is its own log and is ignored.
# A line naming one of the contract keys with a value that fails validation,
# or naming one twice, is output that does not follow the contract: fail
# closed rather than pass a half-trusted value on.
preview_url=""
api_url=""
gate_status=""
# In variables rather than inline: an unquoted regex is parsed as shell
# tokens, and the URL class below carries parentheses and a semicolon.
url_re='^https://[A-Za-z0-9][A-Za-z0-9._~:/?#@!$&()*+,;=%-]*$'
token_re='^[a-z][a-z0-9._-]*$'
while IFS='=' read -r key value; do
	case "$key" in
		preview-url|api-url)
			# An https URL and nothing else: no whitespace, no quotes, nothing
			# that becomes a second line or a shell word downstream.
			if [[ ! "$value" =~ $url_re ]]; then
				refuse "the host's $key is not a plain https URL: '$value'."
			fi
			;;
		gate-status)
			if [[ ! "$value" =~ $token_re ]]; then
				refuse "the host's gate-status is not a plain token: '$value'."
			fi
			;;
		*)
			continue ;;
	esac
	case "$key" in
		preview-url)
			[ -z "$preview_url" ] || refuse "the host reported preview-url twice."
			preview_url="$value" ;;
		api-url)
			[ -z "$api_url" ] || refuse "the host reported api-url twice."
			api_url="$value" ;;
		gate-status)
			[ -z "$gate_status" ] || refuse "the host reported gate-status twice."
			gate_status="$value" ;;
	esac
done < "$out_file"

# An up that does not report all three has not done what the contract says --
# a preview URL guessed from the inputs instead would point wherever the
# workflow's assumption pointed, not where the host put the preview.
if [ "$ACTION" = "up" ]; then
	[ -n "$preview_url" ] || refuse "the host's output carried no preview-url line; refusing to guess."
	[ -n "$api_url" ] || refuse "the host's output carried no api-url line; refusing to guess."
	[ -n "$gate_status" ] || refuse "the host's output carried no gate-status line; refusing to guess."
fi

# Values validated above, one per line -- $GITHUB_OUTPUT is `name=value` per
# line, and the charsets admit neither a newline nor anything that quotes one.
[ -z "$preview_url" ] || echo "preview-url=$preview_url" >> "$GITHUB_OUTPUT"
[ -z "$api_url" ] || echo "api-url=$api_url" >> "$GITHUB_OUTPUT"
[ -z "$gate_status" ] || echo "gate-status=$gate_status" >> "$GITHUB_OUTPUT"
