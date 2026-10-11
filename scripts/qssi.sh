#!/usr/bin/env bash
# QSSI (system image) build -- standalone.
# cwd: snapdragon-auto-gen3-la/apps_qssi/LINUX/android
#
# QSSI_LUNCH_TARGET: icbssi-userdebug (default) or icbssi-user  (--qssi-lunch)
# QSSI_JOBS: -j for build.sh, max 60                             (--qssi-jobs)

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_env ICB_STAGE_DIR QSSI_LUNCH_TARGET QSSI_JOBS
icb_log "starting qssi in $PWD"
icb_print_env QSSI_LUNCH_TARGET QSSI_JOBS

icb_source build/envsetup.sh
icb_lunch "$QSSI_LUNCH_TARGET"
icb_run bash build.sh -j"$QSSI_JOBS" dist --qssi_only

icb_done
