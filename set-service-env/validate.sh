#!/usr/bin/env bash
# set-service-env/validate.sh - the opt-in gate, before any remote call.
#
# Deploy runs this from its validate step, with ALLOW_EMPTY_PROFILE=1 and
# REQUIRE_HEALTH=1. An empty profile is the legacy set-secrets path and exits
# 0 without reading the scoped values. A set profile must be the one approved
# profile, for the one approved app, with all ten values nonempty, with no
# legacy secret names beside it, and — when REQUIRE_HEALTH=1 — with a
# health-urls list. Nothing here opens a connection.
#
# Inputs (environment):
#   SERVICE_ENV_PROFILE   empty, or fields-postgres-v1
#   APP                   resolved app name; KOMIZO_APP_NAME is the fallback
#   SECRET_NAMES          the deploy secrets: input
#   HEALTH_URLS           the deploy health-urls input
#   REQUIRE_HEALTH        1 to refuse an empty health-urls (the deploy opt-in)
#   ALLOW_EMPTY_PROFILE   1 to succeed when the profile is empty
#   KOMIZO_SCOPED_*       the ten values, required only when the profile is set
#   KOMIZO_SECRET_*       refused when the profile is set
set -euo pipefail

# shellcheck source=set-service-env/lib.sh
source "$(dirname "$0")/lib.sh"

: "${SERVICE_ENV_PROFILE:=}"
: "${APP:=}"
: "${SECRET_NAMES:=}"
: "${HEALTH_URLS:=}"

if [ -z "${SERVICE_ENV_PROFILE//[[:space:]]/}" ]; then
	if [ "${ALLOW_EMPTY_PROFILE:-0}" = 1 ]; then
		exit 0
	fi
	scoped_refuse "service-env-profile is empty. The only approved profile is ${SCOPED_PROFILE}."
fi

scoped_check_profile
scoped_check_app
scoped_check_no_legacy_secrets
scoped_check_values
scoped_check_health_required
