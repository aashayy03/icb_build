#!/usr/bin/env bash
# XBL (boot loader) build -- standalone.
# cwd: snapdragon-auto-gen3-la/boot_8155/boot_images/QcomPkg
#
# Overridable through stages.xbl.env in the config:
#   XBL_ENV_SCRIPT (default /opt/qcom-build-env/xbl-env.sh, sourced if present)
#   XBL_VARIANT (default AU), XBL_RELEASE (default RELEASE),
#   XBL_TARGETS (default SDM855Pkg,QcomToolsPkg)

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

XBL_ENV_SCRIPT="${XBL_ENV_SCRIPT:-/opt/qcom-build-env/xbl-env.sh}"
XBL_VARIANT="${XBL_VARIANT:-AU}"
XBL_RELEASE="${XBL_RELEASE:-RELEASE}"
XBL_TARGETS="${XBL_TARGETS:-SDM855Pkg,QcomToolsPkg}"

require_env ICB_STAGE_DIR ICB_ATTEMPT
icb_log "starting xbl in $PWD"
icb_print_env XBL_ENV_SCRIPT XBL_VARIANT XBL_RELEASE XBL_TARGETS

icb_source_if_present "$XBL_ENV_SCRIPT"

if icb_clean_requested; then
    icb_log "attempt ${ICB_ATTEMPT}: nothing to clean for xbl, re-executing"
fi

icb_run python ./buildex.py --variant "$XBL_VARIANT" -r "$XBL_RELEASE" -t "$XBL_TARGETS"

icb_done
