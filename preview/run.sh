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
# The primitive's invocation shape and output contract are the box binary's
# (komizo main, cmd/komizo-box/preview.go over box/preview.go):
#
#   komizo-box preview up --app <app> --pr <N> <image...>
#       prints the preview's PreviewRecord as ONE JSON object
#   komizo-box preview down --app <app> --pr <N>
#       prints an informational sentence -- logged, never parsed
#   komizo-box preview ls
#       prints the surviving records as a JSON array
#
# The record carries no URL: the preview's hostname is pr-<N>.<domain> (and
# pr-<N>-api.<domain>) where the domain is the host's own knob,
# /etc/komizo/preview -- key=value, DOMAIN the key (box/preview.go
# PreviewKnobPath, default preview.gdam.dev). The host is the authority on
# its own domain, so the knob is read over the same fenced SSH path, as the
# same account the box's own ReadPreviewKnob runs as: the read sees exactly
# what the box saw, and an absent or unreadable knob means the compiled
# default, exactly as the box's own fallback means it. The value is validated
# as a plain domain before it becomes an output; anything else fails closed.
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

# The output contract is JSON, and jq does the parsing. It ships on the
# GitHub-hosted runner images; check rather than discover it mid-parse.
command -v jq >/dev/null || refuse "jq is required on the runner to parse the host's JSON output."

# Remote output is untrusted workflow text. Fence it so a compromised host
# cannot emit ::error::, masking, or another workflow command; the parse below
# is what any value crosses the fence through.
out_file="$(mktemp)"
rc=0
fence="komizo-preview-$(date +%s%N)-$RANDOM"
echo "::stop-commands::$fence"
if [ "$ACTION" = "up" ]; then
	# Every argument single-quoted -- safe because the charsets above exclude
	# quotes. The images ride along as positional arguments, one per ref.
	quoted=""
	for ref in "${image_words[@]}"; do
		quoted="$quoted '$ref'"
	done
	# shellcheck disable=SC2029 # the expansion is deliberate, and every value is charset-guarded above
	ssh deploy-target "komizo-box preview up --app '$APP' --pr '$PR_NUMBER'$quoted" 2>&1 | tee "$out_file" || rc=$?
else
	# Down names the preview; the host's own state records which images ran
	# under it, so the refs validated above stay runner-side.
	# shellcheck disable=SC2029 # the expansion is deliberate, and every value is charset-guarded above
	ssh deploy-target "komizo-box preview down --app '$APP' --pr '$PR_NUMBER'" 2>&1 | tee "$out_file" || rc=$?
fi
echo "::$fence::"
if [ "$rc" -ne 0 ]; then
	echo "::error::komizo-box preview $ACTION failed on the host (ssh exited $rc)."
	exit "$rc"
fi

# In variables rather than inline: an unquoted regex is parsed as shell
# tokens, and the URL class below carries parentheses and a semicolon.
url_re='^https://[A-Za-z0-9][A-Za-z0-9._~:/?#@!$&()*+,;=%-]*$'

if [ "$ACTION" = "up" ]; then
	# The contract is ONE JSON object -- the host's PreviewRecord
	# (box/preview.go: v, app, pr, project, db_name, gate_port, images,
	# created_at, last_used, route_file; db_password is json:"-" and never
	# leaves the host). The capture can also carry the primitive's stderr
	# notes (2>&1 above), so non-JSON lines are skipped -- but exactly one
	# JSON object must remain. A key=value line, prose, two objects, or a
	# record naming another preview is output that does not follow the
	# contract: fail closed rather than pass a half-trusted value on.
	mapfile -t records < <(jq -Rc 'fromjson? | objects' "$out_file")
	if [ "${#records[@]}" -eq 0 ]; then
		refuse "the host's up output carried no JSON record; refusing to guess."
	fi
	if [ "${#records[@]}" -gt 1 ]; then
		refuse "the host's up output carried more than one JSON record."
	fi
	record="${records[0]}"

	# A credential on stdout is the one shape the box is pinned never to
	# emit; a record carrying one is not the pinned build's output.
	if jq -e 'has("db_password")' <<<"$record" >/dev/null; then
		refuse "the host's record carried db_password -- credentials never cross the wire."
	fi

	# The record must name the preview this run asked for: a record for
	# another app or PR is the host answering a different question.
	if ! jq -e --arg app "$APP" --argjson pr "$PR_NUMBER" '
		(.v | type) == "number"
		and .app == $app
		and .pr == $pr
		and .project == ($app + "-pr-" + ($pr | tostring))
		and (.db_name | type) == "string"
		and (.gate_port | type) == "number"
		and (.route_file | type) == "string"
	' <<<"$record" >/dev/null; then
		refuse "the host's record is not the PreviewRecord of $APP PR #$PR_NUMBER: $record"
	fi

	# Every value consumed is revalidated against its own charset before it
	# crosses into an output.
	gate_port="$(jq -r '.gate_port' <<<"$record")"
	db_name="$(jq -r '.db_name' <<<"$record")"
	route_file="$(jq -r '.route_file' <<<"$record")"
	if [[ ! "$gate_port" =~ ^[0-9]+$ ]] || [ "$gate_port" -lt 1 ] || [ "$gate_port" -gt 65535 ]; then
		refuse "the host's gate_port is not a port number: '$gate_port'."
	fi
	if [[ ! "$db_name" =~ ^[a-z][a-z0-9_]*$ ]]; then
		refuse "the host's db_name is not a plain identifier: '$db_name'."
	fi
	if [[ ! "$route_file" =~ ^[A-Za-z0-9._-]+$ ]]; then
		refuse "the host's route_file is not a plain file name: '$route_file'."
	fi

	# The preview domain is the host's to say: read its knob over the same
	# fenced path and mirror the box's own ReadPreviewKnob -- an absent or
	# unreadable file is the compiled default (the box falls back the same
	# way, as this same account), a present one is parsed for the first
	# DOMAIN= line, an empty value is the default.
	knob_file="$(mktemp)"
	rc=0
	fence="komizo-preview-$(date +%s%N)-$RANDOM"
	echo "::stop-commands::$fence"
	ssh deploy-target "if [ -r /etc/komizo/preview ]; then cat /etc/komizo/preview; fi" 2>&1 | tee "$knob_file" || rc=$?
	echo "::$fence::"
	if [ "$rc" -ne 0 ]; then
		echo "::error::the preview domain could not be read from the host (ssh exited $rc)."
		exit "$rc"
	fi
	domain="preview.gdam.dev"
	while IFS= read -r ln || [ -n "$ln" ]; do
		case "$ln" in
			DOMAIN=*)
				v="${ln#DOMAIN=}"
				v="${v#"${v%%[![:space:]]*}"}"
				v="${v%"${v##*[![:space:]]}"}"
				if [ -n "$v" ]; then domain="$v"; fi
				break ;;
		esac
	done < "$knob_file"
	domain_re='^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$'
	if [[ ! "$domain" =~ $domain_re ]]; then
		refuse "the host's preview domain is not a plain domain: '$domain'."
	fi

	# The URLs the record does not carry, derived from the verified record's
	# own naming (pr-<N>.<domain> and pr-<N>-api.<domain> -- box/preview.go
	# PreviewHost and previewRoute) and the host-reported domain, then
	# revalidated like any untrusted value.
	preview_url="https://pr-$PR_NUMBER.$domain"
	api_url="https://pr-$PR_NUMBER-api.$domain"
	if [[ ! "$preview_url" =~ $url_re ]]; then
		refuse "the derived preview URL is not a plain https URL: '$preview_url'."
	fi
	if [[ ! "$api_url" =~ $url_re ]]; then
		refuse "the derived api URL is not a plain https URL: '$api_url'."
	fi

	# Values validated above, one per line -- $GITHUB_OUTPUT is `name=value`
	# per line, and the charsets admit neither a newline nor anything that
	# quotes one. gate-status is the composite's own verification, not a
	# host field: the record parsed and named this preview, so it is up.
	{
		echo "preview-url=$preview_url"
		echo "api-url=$api_url"
		echo "gate-status=up"
	} >> "$GITHUB_OUTPUT"
else
	# down's sentence is informational -- logged inside the fence above and
	# never parsed. Teardown is verified against the host's own state
	# instead: preview ls prints the surviving records as a JSON array, and
	# the preview this run named must no longer be in it.
	ls_file="$(mktemp)"
	rc=0
	fence="komizo-preview-$(date +%s%N)-$RANDOM"
	echo "::stop-commands::$fence"
	ssh deploy-target "komizo-box preview ls" 2>&1 | tee "$ls_file" || rc=$?
	echo "::$fence::"
	if [ "$rc" -ne 0 ]; then
		echo "::error::the teardown could not be verified: komizo-box preview ls failed on the host (ssh exited $rc)."
		exit "$rc"
	fi
	mapfile -t arrays < <(jq -Rc 'fromjson? | arrays' "$ls_file")
	if [ "${#arrays[@]}" -ne 1 ]; then
		refuse "the host's ls output was not one JSON array; the teardown is unverified."
	fi
	if ! jq -e --arg app "$APP" --argjson pr "$PR_NUMBER" '
		[.[] | objects | select(.app == $app and .pr == $pr)] | length == 0
	' <<<"${arrays[0]}" >/dev/null; then
		refuse "the host's state still records $APP PR #$PR_NUMBER after down; the teardown is unverified."
	fi
	echo "gate-status=down" >> "$GITHUB_OUTPUT"
fi
