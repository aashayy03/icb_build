#!/usr/bin/env bash
# Meta build (final flashable image) -- runs after super passes.
# cwd: snapdragon-auto-gen3-la/common/build
#
# Overridable through stages.meta.env in the config:
#   META_FLAVORS (default 8155)

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

META_FLAVORS="${META_FLAVORS:-8155}"

require_env ICB_STAGE_DIR
icb_log "starting meta in $PWD"
icb_print_env META_FLAVORS

icb_run python build.py --flavors="$META_FLAVORS"

icb_done
