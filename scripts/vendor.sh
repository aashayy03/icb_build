#!/usr/bin/env bash
# Vendor (GKI + vendor) build -- standalone.
# cwd: snapdragon-auto-gen3-la/apps_vendor/LINUX/android
#
# VENDOR_LUNCH_TARGET: icb8155-userdebug (default) or icb8155-user  (--vendor-lunch)
# VENDOR_JOBS: -j for build.sh, max 90                               (--vendor-jobs)
# GKI_ENABLED=true:  fetch the user-provided GKI GKI_VERSION, and prepare the
#                    vendor kernel with ALAP_GKI_FRAGMENT=0          (--gki true --gki-version ...)
# GKI_ENABLED=false: no fetch, and prepare without ALAP_GKI_FRAGMENT (--gki false)
# Retry (ICB_CLEAN=1): deletes out/ and kernel_platform/out/ first, then rebuilds.
# Incremental (ICB_INCREMENTAL=1, --incremental): keeps out/ and skips the kernel
# steps (gki-fetch, prepare_vendor.sh); only build.sh --target_only runs.
#
# Overridable through stages.vendor.env in the config:
#   VENDOR_ENV_SCRIPT (default /opt/qcom-build-env/vendor-env.sh, sourced if present)

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

VENDOR_ENV_SCRIPT="${VENDOR_ENV_SCRIPT:-/opt/qcom-build-env/vendor-env.sh}"
GKI_SETUP_SCRIPT=./vendor/alpine/kernel/alap-linux/scripts/prebuilt_kernel_setup.sh
PREPARE_VENDOR=./kernel_platform/build/android/prepare_vendor.sh

require_env ICB_STAGE_DIR ICB_ATTEMPT VENDOR_LUNCH_TARGET VENDOR_JOBS GKI_ENABLED
icb_log "starting vendor in $PWD"
icb_print_env VENDOR_LUNCH_TARGET VENDOR_JOBS GKI_ENABLED GKI_VERSION VENDOR_ENV_SCRIPT

# Retry: delete both vendor outputs before anything touches them (lunch writes to out/).
if icb_clean_requested; then
    icb_log "attempt ${ICB_ATTEMPT}: cleaning vendor output before rebuild"
    icb_remove_dir "$ICB_STAGE_DIR/out"
    icb_remove_dir "$ICB_STAGE_DIR/kernel_platform/out"
fi

icb_source_if_present "$VENDOR_ENV_SCRIPT"
icb_source build/envsetup.sh
icb_lunch "$VENDOR_LUNCH_TARGET"

if icb_incremental; then
    # The kernel has no incremental build: reuse the existing kernel output.
    icb_log "incremental build: reusing out/, skipping kernel steps (gki-fetch, prepare_vendor.sh)"
else
    kernel_env=(RECOMPILE_KERNEL=1 RECOMPILE_ABL=1 PREBUILT_ARCHIVE=1)
    if [[ "$GKI_ENABLED" == true ]]; then
        require_env GKI_VERSION
        icb_run "$GKI_SETUP_SCRIPT" gki-fetch -c "$GKI_VERSION"
        kernel_env=(ALAP_GKI_FRAGMENT=0 "${kernel_env[@]}")
    else
        icb_log "GKI not provided (GKI_ENABLED=false): skipping gki-fetch"
    fi
    icb_run env "${kernel_env[@]}" "$PREPARE_VENDOR" gen3auto-alap gki
fi

icb_run bash build.sh -j"$VENDOR_JOBS" dist --target_only

icb_done
