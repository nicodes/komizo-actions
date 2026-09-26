#!/usr/bin/env bash
# set-service-env/validate.sh - the opt-in gate, before any remote call.
#
# Deploy runs this with ALLOW_EMPTY_PROFILE=1 and REQUIRE_HEALTH=1. An empty
# profile is the legacy path for every app except fieldsofrevik, which fails
# closed. A set profile must be fields-postgres-v2 for that app, with a
# 32-hex expected generation, no profile values, and a health-urls list.
# Nothing here opens a connection.
#
# Inputs (environment):
#   SERVICE_ENV_PROFILE   empty, or fields-postgres-v2
#   APP                   resolved app name; KOMIZO_APP_NAME is the fallback
#   SECRET_NAMES          the deploy secrets: input
#   HEALTH_URLS           the deploy health-urls input
#   EXPECTED_GENERATION   required when the profile is set; refused if set
#                         on the legacy path so it cannot be silently dropped
#   REQUIRE_HEALTH        1 to refuse an empty health-urls
#   ALLOW_EMPTY_PROFILE   1 to succeed when the profile is empty and the app
#                         is not fieldsofrevik
set -euo pipefail

# shellcheck source=set-service-env/lib.sh
source "$(dirname "$0")/lib.sh"

: "${SERVICE_ENV_PROFILE:=}"
: "${APP:=}"
: "${SECRET_NAMES:=}"
: "${HEALTH_URLS:=}"
: "${EXPECTED_GENERATION:=}"

if [ -z "${APP:-}" ]; then
	APP="${KOMIZO_APP_NAME:-}"
fi

if [ -z "${SERVICE_ENV_PROFILE//[[:space:]]/}" ]; then
	if [ "$APP" = "$SCOPED_APP" ]; then
		scoped_refuse "app ${SCOPED_APP} requires service-env-profile ${SCOPED_PROFILE}. An empty profile is not the legacy set-secrets path for this app."
	fi
	if [ -n "$EXPECTED_GENERATION" ]; then
		scoped_refuse "expected-generation is set but service-env-profile is empty. Refusing so it is not silently dropped."
	fi
	if [ "${ALLOW_EMPTY_PROFILE:-0}" = 1 ]; then
		exit 0
	fi
	scoped_refuse "service-env-profile is empty. The only approved profile is ${SCOPED_PROFILE}."
fi

scoped_check_profile
scoped_check_app
scoped_check_no_profile_values
scoped_check_generation
scoped_check_health_required
