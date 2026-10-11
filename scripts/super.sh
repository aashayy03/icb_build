#!/usr/bin/env bash
# Super image (merge QSSI + vendor) -- runs after tz, xbl, qssi and vendor pass.
# cwd: snapdragon-auto-gen3-la/apps_vendor/LINUX/android
#
# Lunches the vendor target, then runs build_image_standalone.py with:
#   SUPER_TARGET_LUNCH  --target_lunch value (default msmnile_au)  stages.super.target_lunch
#   SUPER_OUTPUT_OTA    true adds --output_ota                      stages.super.output_ota
#   SUPER_NO_TMP        true adds --no_tmp                          stages.super.no_tmp

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_env ICB_STAGE_DIR QSSI_DIR VENDOR_LUNCH_TARGET SUPER_TARGET_LUNCH
icb_log "starting super in $PWD"
icb_print_env QSSI_DIR VENDOR_LUNCH_TARGET SUPER_TARGET_LUNCH SUPER_OUTPUT_OTA SUPER_NO_TMP

icb_source build/envsetup.sh
icb_lunch "$VENDOR_LUNCH_TARGET"

args=(
    --image super
    --qssi_build_path "$QSSI_DIR/"
    --target_build_path ./
    --merged_build_path ./
    --target_lunch "$SUPER_TARGET_LUNCH"
    --skip_qiifa
)
if [[ "${SUPER_OUTPUT_OTA:-false}" == true ]]; then
    args+=(--output_ota)
fi
if [[ "${SUPER_NO_TMP:-false}" == true ]]; then
    args+=(--no_tmp)
fi
icb_run python vendor/qcom/opensource/core-utils/build/build_image_standalone.py "${args[@]}"

icb_done
