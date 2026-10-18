# shellcheck shell=bash
# Shared helpers for the ICB stage scripts. Source it, don't run it:
#   source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
#
# The orchestrator runs each stage script with cwd = the stage's directory and
# passes all settings as environment variables (ICB_*, QSSI_*, VENDOR_*, GKI_*,
# SUPER_*). Output goes to logs/<run-id>/<stage>.log; the exit code is the only
# result signal.
#
# Stub mode (icb_build.py --stub, i.e. ICB_STUB=1): every command is logged but
# not run, and each one sleeps ICB_STUB_SLEEP seconds (default 1) instead. Use it
# to check the whole pipeline without building. ICB_STUB_FAIL=<stage>[,<stage>]
# makes those stages fail at the end, to test failure handling.
#
# Retries: when a failed stage is retried, the orchestrator sets ICB_ATTEMPT=<n>
# and ICB_CLEAN=1; each script then runs its clean block before building.
#
# Incremental builds (--incremental): ICB_INCREMENTAL=1; qssi/vendor keep out/ and
# vendor skips its kernel steps (see icb_incremental).

icb_log() {
    printf '[%s] [%s] %s\n' "$(date '+%H:%M:%S')" "${ICB_STAGE:-?}" "$*"
}

icb_die() {
    icb_log "ERROR: $*" >&2
    exit 1
}

# require_env VAR...  -- fail fast if any variable is unset or empty.
require_env() {
    local var
    for var in "$@"; do
        [[ -n "${!var:-}" ]] || icb_die "required environment variable $var is not set"
    done
}

# icb_print_env VAR...  -- log the given variables.
icb_print_env() {
    local var
    for var in "$@"; do
        icb_log "  $var=${!var:-}"
    done
}

icb_stub() {
    [[ "${ICB_STUB:-0}" == 1 ]]
}

# Render a command for the log: plain words as-is, anything else shell-quoted.
_icb_quote() {
    local arg out=()
    for arg in "$@"; do
        if [[ "$arg" =~ ^[A-Za-z0-9_@%+=:,./-]+$ ]]; then
            out+=("$arg")
        else
            out+=("$(printf '%q' "$arg")")
        fi
    done
    printf '%s' "${out[*]}"
}

_icb_stub_wait() {
    sleep "${ICB_STUB_SLEEP:-1}"
}

# icb_run CMD [ARGS...]  -- log a command, then run it (stub mode: only log it).
# For VAR=value prefixes use: icb_run env VAR=value ./tool args
icb_run() {
    icb_log "+ $(_icb_quote "$@")"
    if icb_stub; then
        _icb_stub_wait
        return 0
    fi
    "$@"
}

# icb_source FILE [ARGS...]  -- source a file with 'set -eu' relaxed
# (build/envsetup.sh and vendor env scripts are not errexit/nounset-safe).
icb_source() {
    icb_log "+ source $*"
    if icb_stub; then
        _icb_stub_wait
        return 0
    fi
    local rc=0
    set +eu
    # shellcheck disable=SC1090
    source "$@"
    rc=$?
    set -eu
    (( rc == 0 )) || icb_die "source $1 failed (exit $rc)"
}

# icb_source_if_present FILE  -- like icb_source, but skip a missing file.
icb_source_if_present() {
    if icb_stub; then
        icb_log "+ source $1 (if present)"
        _icb_stub_wait
        return 0
    fi
    if [[ -f "$1" ]]; then
        icb_source "$1"
    else
        icb_log "$1 not present, skipping"
    fi
}

# icb_lunch TARGET  -- run lunch (defined by build/envsetup.sh) with 'set -eu' relaxed.
icb_lunch() {
    icb_log "+ lunch $1"
    if icb_stub; then
        _icb_stub_wait
        return 0
    fi
    local rc=0
    set +eu
    lunch "$1"
    rc=$?
    set -eu
    (( rc == 0 )) || icb_die "lunch $1 failed (exit $rc)"
}

# icb_clean_requested  -- true on a retry (ICB_CLEAN=1, set by the orchestrator
# for attempt > 1): the script must clean the stage's output before rebuilding.
icb_clean_requested() {
    [[ "${ICB_CLEAN:-0}" == 1 ]]
}

# icb_incremental  -- true for an incremental build (icb_build.py --incremental sets
# ICB_INCREMENTAL=1 on the first attempt of qssi/vendor): keep out/ and skip
# steps that can't build incrementally. Retries always get ICB_INCREMENTAL=0.
icb_incremental() {
    [[ "${ICB_INCREMENTAL:-0}" == 1 ]]
}

# icb_remove_dir DIR  -- delete a build output directory for a retry clean.
# Refuses anything that is not strictly inside ICB_STAGE_DIR.
icb_remove_dir() {
    local dir="$1"
    if [[ -z "$dir" || "$dir" != "${ICB_STAGE_DIR:?}"/* || "$dir" == *..* ]]; then
        icb_die "refusing to delete '$dir': not inside ICB_STAGE_DIR ($ICB_STAGE_DIR)"
    fi
    if ! icb_stub && [[ ! -e "$dir" ]]; then
        icb_log "$dir does not exist, nothing to delete"
        return 0
    fi
    icb_run rm -rf -- "$dir"
}

# icb_done  -- call last in every stage script.
icb_done() {
    if icb_stub && [[ ",${ICB_STUB_FAIL:-}," == *",${ICB_STAGE},"* ]]; then
        icb_die "STUB: simulated failure (ICB_STUB_FAIL=${ICB_STUB_FAIL})"
    fi
    icb_log "${ICB_STAGE} done$(icb_stub && echo ' (stub mode: nothing was built)')"
}
