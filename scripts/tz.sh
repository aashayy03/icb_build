#!/usr/bin/env bash
# TrustZone (TZ) build -- standalone.
# cwd: snapdragon-auto-gen3-la/tz_8155/trustzone_images/build/ms
#
# Overridable through stages.tz.env in the config:
#   TZ_BUILD_ID (default TZ.XF.5.0), TZ_CHIPSET (default sm8150)

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

TZ_BUILD_ID="${TZ_BUILD_ID:-TZ.XF.5.0}"
TZ_CHIPSET="${TZ_CHIPSET:-sm8150}"

require_env ICB_STAGE_DIR ICB_ATTEMPT
icb_log "starting tz in $PWD"
icb_print_env TZ_BUILD_ID TZ_CHIPSET

if icb_clean_requested; then
    icb_log "attempt ${ICB_ATTEMPT}: nothing to clean for tz, re-executing"
fi

icb_run python build_all.py -b "$TZ_BUILD_ID" "CHIPSET=$TZ_CHIPSET"

icb_done
