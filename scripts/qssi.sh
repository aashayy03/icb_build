#!/usr/bin/env bash
# QSSI (system image) build -- standalone.
# cwd: snapdragon-auto-gen3-la/apps_qssi/LINUX/android
#
# QSSI_LUNCH_TARGET: icbssi-userdebug (default) or icbssi-user  (--qssi-lunch)
# QSSI_JOBS: -j for build.sh, max 90                             (--qssi-jobs)
# Retry (ICB_CLEAN=1): deletes out/ first, then rebuilds from scratch.
# Incremental (ICB_INCREMENTAL=1, --incremental): same commands, out/ is kept.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_env ICB_STAGE_DIR ICB_ATTEMPT QSSI_LUNCH_TARGET QSSI_JOBS
icb_log "starting qssi in $PWD"
icb_print_env QSSI_LUNCH_TARGET QSSI_JOBS

# Retry: delete the QSSI output (out/) before anything touches it (lunch writes there).
if icb_clean_requested; then
    icb_log "attempt ${ICB_ATTEMPT}: cleaning QSSI output before rebuild"
    icb_remove_dir "$ICB_STAGE_DIR/out"
fi

if icb_incremental; then
    icb_log "incremental build: reusing out/"
fi

icb_source build/envsetup.sh
icb_lunch "$QSSI_LUNCH_TARGET"

icb_run bash build.sh -j"$QSSI_JOBS" dist --qssi_only

icb_done
