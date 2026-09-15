#!/usr/bin/env bash
# deploy/validate.sh - everything this action alone can check, checked before
# anything is published or restarted.
#
# A file rather than a `run:` block so a test can drive it over the whole input
# matrix -- see tests/deploy-inputs.test.sh. The bug that motivated the split:
# the config-image rule below had no exemption for the `config:` form, so every
# workflow using a komizo.yml was rejected by its own precondition and the
# feature could not run at all. Nothing executed these actions, so nothing
# noticed.
#
# Deliberately NOT re-checking what the composed actions check for themselves.
# The deploy key, the host keys and every secret's value used to be validated
# here as well, in forty lines that had already drifted from the originals in
# their wording. connect runs first and set-secrets runs before activate, so
# those failures still land before anything on the host changes -- which is the
# property that matters, and it is theirs to keep.
#
# Inputs (environment):
#   HOST, APP                        as resolved by resolve.sh
#   CONFIG                           path to a komizo.yml
#   CONFIG_COMPOSE, CONFIG_HOSTNAMES the older explicit-file pair
#   CONFIG_IMAGE                     registry path the host is pinned to
#   SSH_CONFIG                       ssh config to look for deploy-target in;
#                                    defaults to ~/.ssh/config (a seam for tests)
set -euo pipefail

: "${SSH_CONFIG:=$HOME/.ssh/config}"

if [ -z "${HOST:-}" ]; then
	# No host given, so an earlier step must have run connect. Check the file
	# connect writes rather than asking ssh: OpenSSH resolves ~/.ssh/config from
	# the passwd database, not $HOME, which makes `ssh -G` answer for the wrong
	# home in some environments.
	if ! grep -qE '^[[:space:]]*Host[[:space:]]+deploy-target[[:space:]]*$' "$SSH_CONFIG" 2>/dev/null; then
		echo "::error::No 'host' input and no deploy-target entry in ~/.ssh/config. Either pass host/key/known-hosts to this action, or run the connect action in an earlier step."
		exit 1
	fi
	echo "Using the deploy-target configured by an earlier step."
fi

# Interpolated into a command path below, so constrain it. Required: without it
# the deploy would target `deploy-` and `komizo-`, which is not a smaller
# mistake than a wrong name, only a stranger one.
if [ -z "${APP:-}" ]; then
	D='$'
	echo "::error::No app name. Set the repository variable KOMIZO_APP_NAME and pass 'KOMIZO_APP_NAME: ${D}{{ vars.KOMIZO_APP_NAME }}' under env:, or pass 'app:' directly. It is the name you gave the app in komizo."
	exit 1
fi
case "$APP" in
	*[!A-Za-z0-9_-]*)
		echo "::error::app must be letters, digits, underscore or hyphen; got '$APP'."
		exit 1 ;;
esac

# --- which form of config, if any ------------------------------------------
#
# There are two, and they are alternatives rather than layers: `config:` names a
# komizo.yml that carries the compose path and the hostnames together, and the
# older `config-compose:`/`config-hostnames:` pair names the two files directly.
# Both publish the same image, so both need `config-image:` and neither is
# meaningful without it.
#
# Expressed as one "is a config being published" question rather than as a rule
# per input. Written as four independent rules, the config-image one read
# `[ -z "$CONFIG_COMPOSE" ] && [ -n "$CONFIG_IMAGE" ]` and rejected the whole
# `config:` form -- which is what a rule that names one input can always do
# once a second input means the same thing.

if [ -n "$CONFIG" ] && [ -n "$CONFIG_COMPOSE" ]; then
	echo "::error::pass either config: (a komizo.yml) or config-compose:, not both."
	exit 1
fi
# publish-config derives the hostnames from the manifest and never reads this
# input when config: is set, so passing both silently ships the manifest's
# names and ignores the file -- which looks exactly like a working deploy.
if [ -n "$CONFIG" ] && [ -n "$CONFIG_HOSTNAMES" ]; then
	echo "::error::config-hostnames: has no meaning alongside config: -- a komizo.yml carries its own hostnames. Move them into it, or use config-compose: instead."
	exit 1
fi

if [ -n "$CONFIG" ] || [ -n "$CONFIG_COMPOSE" ]; then
	publishing=1
else
	publishing=0
fi

if [ "$publishing" = 1 ] && [ -z "$CONFIG_IMAGE" ]; then
	echo "::error::config is set but config-image is empty. Pass the registry path the host was bootstrapped with (its CONFIG_IMAGE)."
	exit 1
fi
if [ "$publishing" = 0 ] && [ -n "$CONFIG_IMAGE" ]; then
	echo "::error::config-image is set but no config was named. Pass config: (a komizo.yml) or config-compose:."
	exit 1
fi
# The hostnames ride along with the compose file in one image, so naming them
# without it publishes nothing and silently does nothing.
if [ "$publishing" = 0 ] && [ -n "$CONFIG_HOSTNAMES" ]; then
	echo "::error::config-hostnames is set but no config was named. They are published together in one image, so nothing would be shipped."
	exit 1
fi
