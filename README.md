# icb_build

Build orchestrator for the ICB (infotainment control board) Qualcomm AOSP tree.

It runs the split build in dependency order, with a live status display:

```
TZ ─┐
XBL ─┤
QSSI ─┼──▶ Super ──▶ Meta
Vendor ─┘
(parallel)
```

Each stage is a shell script in `scripts/` holding the real build commands. The Python orchestrator only
decides what runs when, passes settings in, watches the processes and writes logs.

- Linux (Ubuntu) only, Python **3.8.2+**, standard library only.
- Optional: `PyYAML` (YAML configs), `rich` (nicer live table). See `requirements.txt`.

Design details: [`docs/PLAN.md`](docs/PLAN.md).

## Quick start

```bash
cp config/icb.example.yaml icb_build.yaml      # optional; picked up automatically from the cwd

python3 icb_build.py -r /mnt/icb_ws --dry-run  # show resolved settings per stage
python3 icb_build.py -r /mnt/icb_ws --stub     # run the pipeline, log commands, build nothing
python3 icb_build.py -r /mnt/icb_ws            # build
```

`-r/--root-dir` is the directory that **contains** `snapdragon-auto-gen3-la/`.

## Command line

| Flag | Meaning |
|---|---|
| `-r, --root-dir PATH` | **required**, workspace root (CLI only, not allowed in the config) |
| `-c, --config PATH` | YAML/JSON config (default: `./icb_build.yaml`, `.yml` or `.json` if present) |
| `--qssi-lunch TARGET` / `--qssi-jobs N` | QSSI lunch target (default `icbssi-userdebug`) / `-j` (1..90) |
| `--vendor-lunch TARGET` / `--vendor-jobs N` | Vendor lunch target (default `icb8155-userdebug`) / `-j` (1..90) |
| `--gki true\|false` | whether the GKI is provided by the user |
| `--gki-version STR` | e.g. `android14-6.1-2026-06_r6@16037590` (required when `--gki true`) |
| `--only a,b` / `--skip a,b` | stage selection: `tz,xbl,qssi,vendor,super,meta` |
| `--dry-run` | print resolved config and per-stage cwd/command/env, run nothing |
| `--incremental` | incremental qssi/vendor build: keep `out/`, skip vendor's kernel steps (see below) |
| `--stub` | run the pipeline, but scripts only log their commands (see Stub mode) |
| `--log-dir PATH` | where run logs go (default `<tool-repo>/logs`) |
| `--ui auto\|rich\|ansi\|plain\|tui` | status display (`tui` = interactive, see below) |

Precedence: built-in defaults < config file < command line.

```bash
# user-provided GKI
python3 icb_build.py -r /mnt/icb_ws --gki true --gki-version android14-6.1-2026-06_r6@16037590

# rebuild QSSI + Vendor, then super + meta (reuse existing TZ/XBL output)
python3 icb_build.py -r /mnt/icb_ws --skip tz,xbl

# only regenerate the final meta image
python3 icb_build.py -r /data/icb_ws --only meta
```

A skipped stage counts as done, so its dependents still run using its earlier output.

## Configuration

See [`config/icb.example.yaml`](config/icb.example.yaml) (JSON equivalent:
[`config/icb.example.json`](config/icb.example.json)). Every key is optional; built-in
defaults hold the standard stage paths and lunch targets. Unknown keys are rejected so typos don't go unnoticed.

Rules checked before anything runs:
- `jobs` for qssi/vendor must be 1..90 (shared build server). Higher values are an error, never silently lowered.
- `gki: true` needs a `gki_version`.
- The qssi lunch target is required when qssi runs, and the vendor lunch target when vendor or super runs.
- The working directory and script of every selected stage must exist (only a warning with `--dry-run`).

## Stage scripts

The orchestrator runs `bash scripts/<stage>.sh` with `cwd` set to the stage directory.
stdout and stderr go to the stage log, and **exit code 0 means success**. Every command a
script runs is logged as `+ <command>` first.

| Stage | cwd (under `snapdragon-auto-gen3-la/`) | Commands |
|---|---|---|
| tz | `tz_8155/trustzone_images/build/ms` | `python build_all.py -b TZ.XF.5.0 CHIPSET=sm8150` |
| xbl | `boot_8155/boot_images/QcomPkg` | source `/opt/qcom-build-env/xbl-env.sh` if present; `python ./buildex.py --variant AU -r RELEASE -t SDM855Pkg,QcomToolsPkg` |
| qssi | `apps_qssi/LINUX/android` | `source build/envsetup.sh`; `lunch $QSSI_LUNCH_TARGET`; `bash build.sh -j$QSSI_JOBS dist --qssi_only` |
| vendor | `apps_vendor/LINUX/android` | source `/opt/qcom-build-env/vendor-env.sh` if present; envsetup; `lunch $VENDOR_LUNCH_TARGET`; GKI steps (below); `bash build.sh -j$VENDOR_JOBS dist --target_only` |
| super | `apps_vendor/LINUX/android` | envsetup; `lunch $VENDOR_LUNCH_TARGET`; `python vendor/qcom/opensource/core-utils/build/build_image_standalone.py --image super --qssi_build_path $QSSI_DIR/ --target_build_path ./ --merged_build_path ./ --target_lunch msmnile_au --skip_qiifa [--output_ota] [--no_tmp]` |
| meta | `common/build` | `python build.py --flavors=8155` |

**Vendor GKI steps**

- `--gki true --gki-version V` runs these two commands:
  - `./vendor/alpine/kernel/alap-linux/scripts/prebuilt_kernel_setup.sh gki-fetch -c V`
  - `ALAP_GKI_FRAGMENT=0 RECOMPILE_KERNEL=1 RECOMPILE_ABL=1 PREBUILT_ARCHIVE=1 ./kernel_platform/build/android/prepare_vendor.sh gen3auto-alap gki`
- `--gki false` skips gki-fetch and runs prepare_vendor.sh without `ALAP_GKI_FRAGMENT=0`.

Lunch defaults are `icbssi-userdebug` and `icb8155-userdebug`. For user builds, pass
`--qssi-lunch icbssi-user --vendor-lunch icb8155-user`. Super's `--target_lunch`,
`--output_ota` and `--no_tmp` come from `stages.super` in the config.

`envsetup.sh`, `lunch` and the `/opt` env scripts run with `set -eu` relaxed, because they
aren't safe under it. Their exit status is still checked.

**Settings passed in as environment variables:**

| Variable | Stages |
|---|---|
| `ICB_ROOT_DIR`, `ICB_STAGE`, `ICB_STAGE_DIR`, `ICB_LOG_DIR`, `ICB_RUN_ID`, `ICB_STUB` | all |
| `ICB_ATTEMPT`, `ICB_CLEAN` (`1`/`0`; a retry sets `2`+/`1`) | all |
| `QSSI_LUNCH_TARGET`, `QSSI_JOBS` | qssi, super, meta |
| `VENDOR_LUNCH_TARGET`, `VENDOR_JOBS`, `GKI_ENABLED`, `GKI_VERSION` | vendor, super, meta |
| `QSSI_DIR`, `VENDOR_DIR` | super, meta |
| `SUPER_TARGET_LUNCH`, `SUPER_OUTPUT_OTA`, `SUPER_NO_TMP` | super |
| `stages.<name>.env` from the config | that stage |

Values hardcoded in the scripts can be overridden through `stages.<name>.env`:
- tz: `TZ_BUILD_ID`, `TZ_CHIPSET`
- xbl: `XBL_ENV_SCRIPT`, `XBL_VARIANT`, `XBL_RELEASE`, `XBL_TARGETS`
- vendor: `VENDOR_ENV_SCRIPT`
- meta: `META_FLAVORS`

`scripts/common.sh` provides the helpers: `icb_run`, `icb_source`, `icb_source_if_present`,
`icb_lunch`, `icb_clean_requested`, `icb_remove_dir`, `icb_log`, `icb_die`, `require_env` and `icb_done`.

### Stub mode

`--stub` runs the whole pipeline with every command logged but **not run**. Each command
sleeps instead. Use it to check orchestration, paths and settings on the build server in
seconds:

```bash
python3 icb_build.py -r /mnt/icb_ws --stub
ICB_STUB_SLEEP=0.2 ICB_STUB_FAIL=vendor python3 icb_build.py -r /mnt/icb_ws --stub   # test a failure
```

- `ICB_STUB_SLEEP=<sec>`: sleep per command (default 1)
- `ICB_STUB_FAIL=<stage>[,<stage>...]`: make those stages fail

## Status display and logs

- **Terminal:** a live table showing state, elapsed time and last output line per stage. It uses `rich` if installed, otherwise a built-in ANSI table.
- **Not a terminal** (CI, `| tee`, `nohup`): one status line per state change and every `status_interval` seconds.

Every run gets its own folder:

```
logs/
├── 20261009-201500/
│   ├── tz.log xbl.log qssi.log vendor.log super.log meta.log
│   ├── orchestrator.log        # what the orchestrator did and when
│   └── resolved_config.json    # exact merged settings of this run
└── latest -> 20261009-201500
```

Each stage log starts with a header (command, cwd, the variables passed in) and ends with a
footer (end time, duration, exit code, result), including on failure and Ctrl+C. Watch a
stage live with `tail -f logs/latest/qssi.log`.

If the tool repo lives in a home directory with a small quota, clone it onto `/mnt` or
`/data`, or pass `--log-dir`.

## Incremental build (`--incremental`)

Rebuilds QSSI and Vendor **without deleting `out/`**:

| Stage | With `--incremental` |
|---|---|
| qssi | same commands as a normal build: envsetup, lunch, `build.sh --qssi_only` (out/ is kept) |
| vendor | vendor-env.sh (if present), envsetup, lunch, `build.sh --target_only`. **Kernel steps skipped** (gki-fetch, `prepare_vendor.sh`), because the kernel has no incremental build |
| tz, xbl, super, meta | unchanged |

```bash
python3 icb_build.py -r /mnt/icb_ws --incremental                        # full pipeline
python3 icb_build.py -r /mnt/icb_ws --incremental --skip tz,xbl          # reuse TZ/XBL output
python3 icb_build.py -r /mnt/icb_ws --incremental --only qssi,vendor     # just the two builds
```

- `--only`/`--skip` decide which stages run, as usual. `--incremental` only changes how qssi and vendor build.
- There's no check that a previous build exists. If `out/` (or the kernel output) is missing, the build fails the normal way.
- **A retry is still a full clean build**, including the kernel steps.
- Works with every display, including the TUI: `--incremental --ui tui`. Incremental builds are chosen only on the command line; the TUI has no incremental key.
- The stage log header shows `build    : incremental` or `full`.
- `--incremental` is a command-line flag only; it can't be set in the config file.

## Interactive TUI (`--ui tui`)

```bash
python3 icb_build.py -r /mnt/icb_ws --ui tui
```

A full-screen terminal UI (curses, standard library only) with:
- a header showing the run, its lunch targets, the GKI mode (user-provided plus its version, or not provided) and the build mode (`full` or `incremental`)
- the stage table
- a live tail of the selected stage's log
- a full-screen log viewer
- keyboard control, including retrying failed stages

It's opt-in: `--ui auto` never picks it, and it needs a real terminal (exit 2 otherwise).

| Key | Main screen |
|---|---|
| Up/Down, k/j | select a stage (the log pane follows the selection) |
| Enter | open the selected stage's log full screen |
| r | clean and retry the selected FAILED stage (asks y/n) |
| c | cancel the run (asks y/n), same as Ctrl+C |
| q | quit (asks to cancel first if stages are still running) |
| ? | help |

| Key | Log viewer |
|---|---|
| Up/Down, PgUp/PgDn, g/G | scroll, top, bottom |
| f | follow the end of the log on/off (on while the stage runs) |
| / , n , N | search (case-insensitive), next, previous match |
| q / Esc | back |

- **Stays open:** the TUI stays open after the run ends (success, failure or Ctrl+C) so you can read logs and retry. Press `q` to leave; the normal summary is then printed.
- **Retry:** retries work while other stages are still running. In TUI mode the end-of-run `[y/N]` prompt isn't used; `r` replaces it.
- **Exit codes:** the same as for the other displays.
- **Terminal size:** it needs at least 60x15.

## Retrying a failed stage

When a run finishes with FAILED stages in a terminal, the tool asks about each one. In the TUI,
select the stage and press `r` instead.

```
qssi FAILED (attempt 1, exit 2), log: logs/20261018-101500/qssi.log
Clean and retry qssi? [y/N]
```

Answering `y` restarts that stage in a fresh shell. The env scripts, envsetup and lunch run
again, after the stage's clean step (`ICB_CLEAN=1`):

| Stage | Clean on retry |
|---|---|
| tz, xbl, super, meta | nothing: re-execute |
| qssi | delete `apps_qssi/LINUX/android/out` |
| vendor | delete `apps_vendor/LINUX/android/out` and `.../kernel_platform/out` |

Stages it blocked, such as super and meta, then continue automatically. If it fails again,
you're asked again. A QSSI/Vendor retry rebuilds from scratch, so expect a full build time.

- Enter or `n` means no. EOF or Ctrl+C stops asking.
- It never asks in CI or when output is piped, and never after Ctrl+C.
- `<stage>.log` is the latest attempt; earlier attempts are kept as `<stage>.attempt<N>.log`.

Deletes go through `icb_remove_dir` in `scripts/common.sh`, which refuses any path outside
the stage's own directory. In `--stub` mode the deletes are only logged.

## Failures and Ctrl+C

- If a stage fails, the other running stages finish. Stages that depend on it are `BLOCKED`, and the tool offers a retry at the end (see above).
- **First Ctrl+C:** SIGTERM goes to every running stage's process group (make, ninja, … included). After 10 s, SIGKILL.
- **Second Ctrl+C:** SIGKILL immediately.

| Exit code | Meaning |
|---|---|
| 0 | all selected stages completed |
| 1 | a stage failed or was blocked |
| 2 | config/usage error (nothing ran) |
| 130 | interrupted |

## Development

```bash
python3 -m unittest discover -s tests -t .
```

To test against Python 3.8 without touching the system Python (everything stays in the
gitignored `.deps/`):

```bash
python3 -m pip install --target .deps uv
UV_PYTHON_INSTALL_DIR=$PWD/.deps/pythons .deps/bin/uv python install 3.8
.deps/pythons/cpython-3.8.*/bin/python3.8 -m unittest discover -s tests -t .
```

Code must stay 3.8-compatible: `typing.List`/`Optional` rather than `list[str]` or `X | Y`, and no
`match`, `str.removeprefix`, `functools.cache` or `argparse.BooleanOptionalAction`.
