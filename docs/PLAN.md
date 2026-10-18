# ICB Build Orchestrator: Design Plan

> Status: **implemented (v0.1.0)**, real build commands in place, `--stub` for dry pipeline runs, retry of failed stages (section 13). interactive TUI `--ui tui` (`docs/TUI_PLAN.md`). See README.md for usage.

---

## 1. Why we need this

The ICB (infotainment control board) software is a Qualcomm AOSP tree, synced with `repo`. The source tree starts at `snapdragon-auto-gen3-la/`, directly under `--root-dir`. It isn't one build but several:

| Stage | Type | Depends on |
|---|---|---|
| **TZ** (TrustZone) | standalone | none |
| **XBL** (bootloader) | standalone | none |
| **QSSI** (Qualcomm single system image) | standalone | none |
| **Vendor** (GKI + vendor) | standalone | none |
| **Super** (super.img) | combines the above | TZ, XBL, QSSI, Vendor |
| **Meta** (final flashable image) | packages everything | Super |

Running these by hand is slow and error-prone: you have to remember the order, watch several terminals and find the log that failed. The goal is one command that:

1. runs **TZ, XBL, QSSI and Vendor in parallel**
2. runs **Super** once all four succeed, then **Meta**. (Strictly, super.img only needs QSSI + Vendor, but for ICB it waits for all four. TZ/XBL are quick, so it costs little.)
3. shows **live status** for every stage (running / failed / completed …)
4. reads settings from a **YAML (recommended) or JSON** config, which **CLI arguments can override**
5. runs every build through a **`.sh` script owned by this repo**, so the build commands stay in shell, where Android developers expect them

Constraints: **Linux (Ubuntu) only**, **Python ≥ 3.8.2**.

---

## 2. How it fits together

```
                    ┌──────────────────────────────┐
  CLI args ───────▶ │                              │
                    │   icb_build.py (Python)      │
  icb.yaml ───────▶ │   orchestrator               │
  (or .json)        │                              │
                    └──────────────┬───────────────┘
                                   │ starts each stage as: bash scripts/<stage>.sh
                                   │ (cwd + env vars set by orchestrator)
        ┌──────────────┬───────────┼───────────┬──────────────┐
        ▼              ▼           ▼           ▼              │
     tz.sh          xbl.sh      qssi.sh    vendor.sh          │  Wave 1 (parallel)
        └──────────────┴─────┬─────┴───────────┘              │
                             ▼  all four COMPLETED            │
                          super.sh                            │  Wave 2
                             ▼  COMPLETED                     │
                          meta.sh                             │  Wave 3
                                                              │
     each stage's stdout/stderr ──▶ logs/<run-id>/<stage>.log │
     live status table ◀──────────────────────────────────────┘
```

**Split of responsibilities**

- **Python orchestrator:** decides *what* runs *when*, passes configuration in, watches processes, shows status, handles failures and Ctrl+C, and writes logs. It knows nothing about Android build commands.
- **Shell scripts:** decide *how* to build a stage (`source build/envsetup.sh`, `lunch`, `make`, `build.py`, …). Each logs every command before running it (see section 6). If the build procedure changes, you only edit shell, never Python.

---

## 3. Project layout

```
icb_build/
├── icb_build.py                 # entry point:  python3 icb_build.py -r <root> [options]
├── orchestrator/
│   ├── cli.py                   # argument parsing, maps flags onto config keys
│   ├── config.py                # defaults → config file → CLI merge, validation
│   ├── stages.py                # stage definitions, dependency graph, states
│   ├── runner.py                # starts one .sh in its own process group, writes its log
│   ├── scheduler.py             # main loop: start ready stages, propagate failures, Ctrl+C
│   └── display/
│       ├── rich_display.py      # pretty live table (used if `rich` is installed)
│       ├── ansi_display.py      # stdlib-only live table (fallback)
│       ├── plain_display.py     # periodic one-line status (CI / piped output)
│       ├── tui_display.py       # interactive curses TUI (--ui tui)
│       └── logview.py           # log tail / line index / viewer state for the TUI
├── scripts/
│   ├── common.sh                # helpers: icb_run/icb_source/icb_lunch (logging + stub mode)
│   └── tz.sh  xbl.sh  qssi.sh  vendor.sh  super.sh  meta.sh     # real build commands
├── config/
│   ├── icb.example.yaml
│   └── icb.example.json
├── tests/                       # stdlib unittest, runs the scripts in stub mode
├── requirements.txt             # optional extras: PyYAML, rich
└── README.md
```

The folder will also be initialised as a git repo, with a `.gitignore` for `logs/` and `__pycache__/`.

---

## 4. Configuration

### 4.1 Precedence
```
built-in defaults   <   config file (yaml/json)   <   CLI arguments
```
Each layer overrides the one before it, key by key. So the config file can hold your usual setup, and a CLI flag changes one value for one run.

### 4.2 Config file format
- `.yaml` / `.yml` is loaded with **PyYAML**. If PyYAML isn't installed you get a clear error that suggests `pip install pyyaml` or using JSON.
- `.json` uses the standard library, so it always works.
- The file is chosen with `-c/--config`. If that isn't given, `./icb_build.yaml` (or `.json`) is used if it exists. Otherwise defaults and CLI only.

### 4.3 Example (`config/icb.example.yaml`)
```yaml
log_dir: logs              # relative to this repo unless absolute
status_interval: 30        # seconds between status lines in plain (CI) mode
ui: auto                   # auto | rich | ansi | plain

stages:                    # every path is relative to --root-dir
  tz:
    path: snapdragon-auto-gen3-la/tz_8155/trustzone_images/build/ms
  xbl:
    path: snapdragon-auto-gen3-la/boot_8155/boot_images/QcomPkg
  qssi:
    path: snapdragon-auto-gen3-la/apps_qssi/LINUX/android
    lunch_target: icbssi-userdebug     # or icbssi-user
    jobs: 60               # hard max 60 (shared build server)
  vendor:
    path: snapdragon-auto-gen3-la/apps_vendor/LINUX/android
    lunch_target: icb8155-userdebug    # or icb8155-user
    jobs: 60               # hard max 60 (shared build server)
    gki: false
    gki_version: ""        # e.g. android14-6.1-2026-06_r6@16037590
  super:
    path: snapdragon-auto-gen3-la/apps_vendor/LINUX/android     # same tree as vendor
    target_lunch: msmnile_au   # --target_lunch of build_image_standalone.py
    output_ota: false          # true adds --output_ota
    no_tmp: false              # true adds --no_tmp
  meta:
    path: snapdragon-auto-gen3-la/common/build

  # Optional on any stage:
  #   script: scripts/<name>.sh     # override which script runs
  #   env:    { KEY: value }        # extra env vars passed only to that stage
```
All values above are also the **built-in defaults**, so the config file only needs the values that differ.

**`root_dir` cannot be set in the config.** It comes only from `-r/--root-dir`. If a config file contains `root_dir`, that's an error with a clear message, so nobody thinks it's being used.

### 4.4 Validation (before anything runs)
- The platform must be Linux. Otherwise exit with a clear message.
- `--root-dir` is turned into an **absolute path with symlinks resolved** (`os.path.realpath`) before use, so every path the scripts and logs see is absolute. On the build server workspaces live under `/mnt/...` or `/data/...`, but any location is accepted, including a local test tree.
- `--root-dir` must exist and contain `snapdragon-auto-gen3-la/`. Otherwise it's an error: you probably passed the wrong directory, e.g. `snapdragon-auto-gen3-la` itself instead of its parent.
- **`jobs` (QSSI and Vendor) must be an integer from 1 to 60.** 60 is a hard limit because the 192-core build server is shared. A value above 60 from the config or CLI is an **error (exit 2)**, not quietly lowered, so you always know the real `-j` used. The default is `min(60, nproc)`. The limit is one constant (`MAX_JOBS = 60` in `config.py`) if it ever needs to change.
- `gki: true` requires a non-empty `gki_version`.
- Stage names in `--only` / `--skip` must be valid.
- The working dir of every stage that will run must exist. In `--dry-run` this is only a warning.

---

## 5. Command line

```
python3 icb_build.py -r ROOT_DIR [options]
```

| Flag | Meaning | Config key it overrides |
|---|---|---|
| `-r, --root-dir PATH` | **Required.** Root of the repo-synced workspace | none |
| `-c, --config PATH` | YAML/JSON config file | none |
| `--qssi-lunch TARGET` | QSSI lunch target (default `icbssi-userdebug`) | `stages.qssi.lunch_target` |
| `--qssi-jobs N` | QSSI `-j` (1–60) | `stages.qssi.jobs` |
| `--vendor-lunch TARGET` | Vendor lunch target (default `icb8155-userdebug`) | `stages.vendor.lunch_target` |
| `--vendor-jobs N` | Vendor `-j` (1–60) | `stages.vendor.jobs` |
| `--gki true\|false` | Whether you provide the GKI (also accepts yes/no/1/0) | `stages.vendor.gki` |
| `--gki-version STR` | e.g. `android14-6.1-2026-06_r6@16037590` | `stages.vendor.gki_version` |
| `--only a,b` | Run only these stages | none |
| `--skip a,b` | Run all stages except these | none |
| `--dry-run` | Show the resolved config and exact command/cwd/env for each stage, run nothing | none |
| `--stub` | Run the whole pipeline, but scripts only log their commands (`ICB_STUB=1`) | none |
| `--log-dir PATH` | Where run logs go | `log_dir` |
| `--ui MODE` | `auto` / `rich` / `ansi` / `plain` / `tui` (interactive, see `docs/TUI_PLAN.md`) | `ui` |

New flags can be added easily: each one is a single line mapping a flag to a config key.

**Examples**
```bash
# Full build using the config file
python3 icb_build.py -r /mnt/icb_ws -c config/icb.yaml

# Same, but with a user-provided GKI
python3 icb_build.py -r /mnt/icb_ws -c config/icb.yaml --gki true --gki-version android14-6.1-2026-06_r6@16037590

# Only rebuild QSSI + Vendor, then super + meta (reuse existing TZ/XBL output)
python3 icb_build.py -r /mnt/icb_ws --skip tz,xbl

# Only regenerate the final meta image
python3 icb_build.py -r /mnt/icb_ws --only meta

# Check what would run
python3 icb_build.py -r /data/icb_ws --dry-run
```

---

## 6. Contract between the orchestrator and the `.sh` scripts

Each stage runs as:
```
bash scripts/<stage>.sh       # cwd = <root-dir>/<stage.path>
```
stdout and stderr are merged into that stage's log file. **All settings are passed as environment variables**, so the scripts never have to parse arguments.

| Variable | Given to | Example |
|---|---|---|
| `ICB_ROOT_DIR` | all | `/mnt/icb_ws` |
| `ICB_STAGE` | all | `qssi` |
| `ICB_STAGE_DIR` | all | `/mnt/icb_ws/snapdragon-auto-gen3-la/apps_qssi/LINUX/android` |
| `ICB_LOG_DIR` | all | `/.../logs/20261009-201500` |
| `ICB_RUN_ID` | all | `20261009-201500` |
| `ICB_STUB` | all | `0`, or `1` with `--stub` |
| `ICB_ATTEMPT`, `ICB_CLEAN` | all | `1`, `0`; on a retry `2`, `1` (see section 13) |
| `QSSI_LUNCH_TARGET`, `QSSI_JOBS` | qssi, super, meta | `icbssi-userdebug`, `60` |
| `VENDOR_LUNCH_TARGET`, `VENDOR_JOBS` | vendor, super, meta | `icb8155-userdebug`, `60` |
| `GKI_ENABLED` | vendor (+ super, meta) | `true` / `false` |
| `GKI_VERSION` | vendor (+ super, meta) | `android14-6.1-2026-06_r6@16037590` |
| `QSSI_DIR`, `VENDOR_DIR` | super, meta | absolute paths, since these steps need both trees |
| `SUPER_TARGET_LUNCH`, `SUPER_OUTPUT_OTA`, `SUPER_NO_TMP` | super | `msmnile_au`, `false`, `false` |
| anything in `stages.<x>.env` | that stage | user-defined |

**What GKI means is decided in `vendor.sh`** (see below). The orchestrator only passes `GKI_ENABLED` / `GKI_VERSION`.

**Exit code is the only success signal:** 0 means COMPLETED, anything else means FAILED.

### Build commands per stage
cwd is relative to `snapdragon-auto-gen3-la/`.

| Stage | cwd | Commands |
|---|---|---|
| tz | `tz_8155/trustzone_images/build/ms` | `python build_all.py -b TZ.XF.5.0 CHIPSET=sm8150` |
| xbl | `boot_8155/boot_images/QcomPkg` | source `/opt/qcom-build-env/xbl-env.sh` (if present) → `python ./buildex.py --variant AU -r RELEASE -t SDM855Pkg,QcomToolsPkg` |
| qssi | `apps_qssi/LINUX/android` | `source build/envsetup.sh` → `lunch $QSSI_LUNCH_TARGET` → `bash build.sh -j$QSSI_JOBS dist --qssi_only` |
| vendor | `apps_vendor/LINUX/android` | source `/opt/qcom-build-env/vendor-env.sh` (if present) → envsetup → `lunch $VENDOR_LUNCH_TARGET` → GKI steps → `bash build.sh -j$VENDOR_JOBS dist --target_only` |
| super | `apps_vendor/LINUX/android` | envsetup → `lunch $VENDOR_LUNCH_TARGET` → `python vendor/qcom/opensource/core-utils/build/build_image_standalone.py --image super --qssi_build_path $QSSI_DIR/ --target_build_path ./ --merged_build_path ./ --target_lunch msmnile_au --skip_qiifa [--output_ota] [--no_tmp]` |
| meta | `common/build` | `python build.py --flavors=8155` |

**Vendor GKI steps:**
- `GKI_ENABLED=true`:
  - `./vendor/alpine/kernel/alap-linux/scripts/prebuilt_kernel_setup.sh gki-fetch -c $GKI_VERSION`
  - `ALAP_GKI_FRAGMENT=0 RECOMPILE_KERNEL=1 RECOMPILE_ABL=1 PREBUILT_ARCHIVE=1 ./kernel_platform/build/android/prepare_vendor.sh gen3auto-alap gki`
- `GKI_ENABLED=false`: no gki-fetch. prepare_vendor.sh runs without `ALAP_GKI_FRAGMENT=0`.

Values that are hardcoded in the scripts can be overridden per stage through `stages.<x>.env`:
- tz: `TZ_BUILD_ID`, `TZ_CHIPSET`
- xbl: `XBL_ENV_SCRIPT`, `XBL_VARIANT`, `XBL_RELEASE`, `XBL_TARGETS`
- vendor: `VENDOR_ENV_SCRIPT`
- meta: `META_FLAVORS`

**Script conventions (`common.sh`):**
- Scripts run with `set -euo pipefail`.
- Every command goes through `icb_run`, which logs `+ <command>` before running it.
- `icb_source` and `icb_lunch` relax `set -eu` around `envsetup.sh`, `lunch` and the `/opt` env scripts, which aren't safe under it, then check the exit status explicitly.

### Stub mode (`--stub`)
`--stub` sets `ICB_STUB=1` for every stage. `icb_run`, `icb_source` and `icb_lunch` then only log the command and sleep `ICB_STUB_SLEEP` seconds (default 1). This checks orchestration, paths and settings on the real build server in seconds, and the test suite uses it.

`ICB_STUB_FAIL=<stage>[,<stage>]` makes those stages fail, to test failure handling. `ICB_STUB` is always passed explicitly (`0` or `1`), so a value exported in the user's shell can't switch a real build into stub mode.

---

## 7. Execution model

### 7.1 Stage states
| State | Meaning |
|---|---|
| `PENDING` | waiting for its turn or its dependencies |
| `RUNNING` | its script is running |
| `COMPLETED` | script exited 0 |
| `FAILED` | script exited non-zero |
| `SKIPPED` | excluded by `--only`/`--skip`. **Counts as done**, so `--skip tz` still lets Super run (it assumes the earlier TZ output is still there) |
| `BLOCKED` | not run because a dependency failed, was blocked or was cancelled |
| `CANCELLED` | stopped by Ctrl+C |

### 7.2 Main loop
The scheduler doesn't hard-code "wave 1, then 2, then 3". It works from the dependency graph and, about every 0.5 s:

1. starts every `PENDING` stage whose dependencies are all `COMPLETED` or `SKIPPED`
2. marks as `BLOCKED` every `PENDING` stage with a dependency that is `FAILED`, `BLOCKED` or `CANCELLED`
3. polls running processes and records exit codes and durations
4. refreshes the display

This gives the required behaviour (four in parallel → Super → Meta). If dependencies change later (for example Super only needing QSSI and Vendor), it's a one-line edit.

### 7.3 Failure policy (as you requested)
If one of TZ/XBL/QSSI/Vendor fails, **the others keep running to the end**, so their output is still usable and you get every failure from one run. Super and Meta become `BLOCKED`. The run exits with code 1.

### 7.4 Ctrl+C
Each stage runs in **its own process group**, so stopping it also stops everything it started (make, ninja, soong, …):
- **1st Ctrl+C:** SIGTERM goes to every running stage's process group, with up to 10 s to shut down cleanly, then SIGKILL. Running stages become `CANCELLED` and the rest `BLOCKED`.
- **2nd Ctrl+C:** immediate SIGKILL.
- No orphaned build processes are left behind.

### 7.5 Implementation choice
The orchestrator uses threads and `subprocess.Popen`, not asyncio. asyncio's child-process handling on Python 3.8 has known quirks, and threads are simple and predictable here. Each stage has one reader thread that copies its output to the log file and remembers the last line for the display.

### 7.6 Exit codes
| Code | Meaning |
|---|---|
| 0 | every stage COMPLETED or SKIPPED |
| 1 | at least one stage FAILED or BLOCKED |
| 2 | config or usage error (nothing was run) |
| 130 | interrupted by Ctrl+C |

---

## 8. Live status display

The display mode is chosen automatically (`--ui auto`, overridable):

```
stdout is not a terminal (CI, | tee, nohup)?  ──yes──▶  plain
            │ no
            ▼
     is `rich` installed?  ──yes──▶  rich
            │ no
            ▼
          ansi
```

**rich / ansi (interactive terminal):** a table that updates in place about once a second:
```
 STAGE    STATE       ELAPSED   LAST OUTPUT
 tz       COMPLETED   00:04:12  Build succeeded
 xbl      RUNNING     00:06:40  Compiling QcomPkg/...
 qssi     RUNNING     00:06:40  [ 41% 51234/124000] ...
 vendor   FAILED      00:03:05  ninja: build stopped: subcommand failed.
 super    BLOCKED     -
 meta     BLOCKED     -
```
States are colour-coded (green completed, yellow running, red failed, grey pending/blocked). The last-output column is cut to fit the terminal width.

- **rich** looks nicer (spinners, clean redraw, handles terminal resizing) but is an optional install.
- **ansi** is our own standard-library version. It looks plainer but always works.

**plain (CI / piped):** one line every `status_interval` seconds and whenever any stage changes state:
```
[00:06:40] tz=COMPLETED xbl=RUNNING qssi=RUNNING vendor=FAILED super=BLOCKED meta=BLOCKED
```

When the run ends, all modes print a **summary table** with each stage's final state, duration and log file path.

---

## 9. Logs

### 9.1 Layout
**Each stage logs to its own file.** Every run gets a new timestamped folder, so nothing is overwritten:
```
logs/
├── 20261009-201500/              ← one folder per run (its start time = ICB_RUN_ID)
│   ├── tz.log                    ← everything tz.sh printed
│   ├── xbl.log
│   ├── qssi.log
│   ├── vendor.log
│   ├── super.log
│   ├── meta.log
│   ├── orchestrator.log          ← the orchestrator's own record of the run
│   └── resolved_config.json      ← the exact merged config used for this run
├── 20261008-174210/              ← an earlier run, kept as is
└── latest -> 20261009-201500     ← symlink to the most recent run
```
A retried stage keeps earlier attempts as `<stage>.attempt<N>.log` next to `<stage>.log` (section 13).

### 9.2 Stage logs (`<stage>.log`)
- The script's stdout and stderr both go into one file, so errors appear in order with the rest of the output.
- Each parallel stage writes to its own file, so output from the four builds is never mixed.
- Lines are written as the build prints them, so you can watch a stage live with `tail -f logs/latest/qssi.log`.
- A log file is created only for a stage that actually starts. A `SKIPPED` or `BLOCKED` stage has no log. Its state is recorded in `orchestrator.log`.
- Build output does not scroll on the terminal. The status table's "last output" column shows the most recent line of each stage log, and the final summary prints each log's path.

### 9.3 Header and footer in each stage log
The orchestrator writes a **header** before the script starts and a **footer** after it ends, so each log can be understood on its own (for example when attached to a bug report):

```
================================================================================
ICB BUILD  stage=qssi  run=20261009-201500
start    : 2026-10-09 20:15:00 +0530
host     : build-server-01  (user: aashay)
command  : bash /home/aashay/projects/icb_build/scripts/qssi.sh
cwd      : /mnt/icb_ws/snapdragon-auto-gen3-la/apps_qssi/LINUX/android
env      :
  ICB_ROOT_DIR=/mnt/icb_ws
  ICB_STAGE=qssi
  ICB_STAGE_DIR=/mnt/icb_ws/snapdragon-auto-gen3-la/apps_qssi/LINUX/android
  ICB_LOG_DIR=/home/aashay/projects/icb_build/logs/20261009-201500
  ICB_RUN_ID=20261009-201500
  QSSI_LUNCH_TARGET=qssi-userdebug
  QSSI_JOBS=32
================================================================================
... script output ...
================================================================================
end      : 2026-10-09 21:02:41 +0530
duration : 00:47:41
exit code: 0
result   : COMPLETED
================================================================================
```

- **env** lists only the variables the orchestrator passes in (the `ICB_*`, `QSSI_*`, `VENDOR_*`, `GKI_*` variables and any per-stage `env:` from the config), sorted. It does not dump the whole shell environment, which is long and may contain tokens.
- **The footer is always written.** That includes failures (`result: FAILED`, with the exit code), Ctrl+C (`result: CANCELLED`, with the signal used) and a script that couldn't start (`result: FAILED`, with the error, e.g. script not found). A log without a footer means the orchestrator itself was killed.
- The header and footer are written only by the orchestrator. The scripts don't need to do anything.

### 9.4 `orchestrator.log`
A timestamped record of what the orchestrator did:
- the resolved config, and which stages were selected or skipped
- each stage's start, end, duration and exit code
- failures, and which stages were blocked because of them
- Ctrl+C and the signals sent

This answers "what happened in this run?" without opening six files.

### 9.5 `resolved_config.json`
The final merged settings (defaults, then config file, then CLI), e.g. lunch targets, jobs and GKI version. It lets you see exactly which settings a past build used.

### 9.6 What these logs don't cover
Files the Android/QCOM build writes itself, such as soong's `out/verbose.log.gz` and `out/error.log`, stay where the build puts them. The stage log captures what the script prints, not those internal files.

---

## 10. Python 3.8.2 compatibility

The code avoids features added after 3.8:
- no `list[str]` / `X | Y` type syntax (uses `typing.List`, `Optional`, …)
- no `match`, `str.removeprefix`, `functools.cache` or `argparse.BooleanOptionalAction`

Required dependencies: **none** (standard library only).
Optional: `PyYAML` for YAML configs and `rich` for the nicer display. Both support 3.8.

---

## 11. Testing and verification

1. **Unit/integration tests** (`python3 -m unittest discover -s tests -t .`), run against the real scripts in stub mode:
   - config precedence: defaults < file < CLI, for both YAML and JSON
   - happy path: all six stages complete, the four parallel stages overlap in time, and Super starts only after all four
   - `ICB_STUB_FAIL=qssi`: the other three still complete, Super and Meta are `BLOCKED`, exit code 1
   - `--skip tz,xbl`: Super still runs
   - `--only meta`: only Meta runs
   - `--qssi-jobs 61` (or `jobs: 61` in config) is rejected with exit 2; `root_dir` in the config is rejected; a root without `snapdragon-auto-gen3-la/` is rejected
   - each stage log has a header (command, cwd, passed-in env) and a footer with the right exit code/result for COMPLETED, FAILED and CANCELLED; skipped/blocked stages have no log file
2. **Manual checks**
   - `--dry-run` prints the right cwd, env and command for each stage
   - in a real run the live table updates, and the log folder and `latest` symlink are created
   - Ctrl+C mid-run stops cleanly with no leftover processes (`pgrep -f scripts/`)
   - with `| tee out.txt` the plain mode is used automatically
3. **Python 3.8 check:** run the tests under 3.8 (e.g. `uv run --python 3.8 ...`) and/or run `vermin -t=3.8-`.

---

## 12. Resolved decisions

| Topic | Decision |
|---|---|
| Source layout | `--root-dir` is the **parent** of `snapdragon-auto-gen3-la/`. Stage paths (built-in defaults): TZ `snapdragon-auto-gen3-la/tz_8155/trustzone_images/build/ms`, XBL `.../boot_8155/boot_images/QcomPkg`, QSSI `.../apps_qssi/LINUX/android`, Vendor + Super `.../apps_vendor/LINUX/android`, Meta `.../common/build` |
| Super dependencies | Super waits for **all four** (TZ, XBL, QSSI, Vendor) to pass. TZ/XBL are quick, so the delay is small |
| Jobs limit | QSSI and Vendor `jobs` are each **at most 60** (shared 192-core server). Values over 60 are rejected (exit 2). Default `min(60, nproc)` |
| `--root-dir` | **CLI only.** `root_dir` in a config file is an error |
| Workspace location | On the build server, workspaces live under `/mnt` or `/data`. This is informational only: any `--root-dir` is accepted and resolved to an absolute path. The examples use `/mnt/icb_ws` and `/data/icb_ws` |
| Log location | Logs stay in the tool's repo (`<tool-repo>/logs/`) by default. Can be overridden with `--log-dir` or `log_dir` |
| Super / Meta location | Super runs in the Vendor tree (`apps_vendor/LINUX/android`), Meta in `common/build` |
| Build commands | As listed in section 6. Script names fixed to `prebuilt_kernel_setup.sh` / `RECOMPILE_KERNEL` |
| GKI false | Skip gki-fetch; prepare_vendor.sh without `ALAP_GKI_FRAGMENT=0` |
| Super | envsetup + lunch vendor target first; `target_lunch`/`output_ota`/`no_tmp` configurable |
| Testing without building | `--stub` mode |
| Retry | Asked at the end of a terminal run. Clean: QSSI `out/`; Vendor `out/` and `kernel_platform/out/`; TZ, XBL, Super and Meta re-execute only. Logs kept per attempt (section 13) |

No open questions remain.

---

## 13. Retry of failed stages

When stages fail, the tool can clean and rebuild them without restarting the whole run.

**When it asks.** After every stage has finished, the tool asks about each FAILED stage, in pipeline order. It only asks if the run wasn't interrupted (Ctrl+C) and both stdin and stdout are terminals:
```
qssi FAILED (attempt 1, exit 2), log: logs/20261018-101500/qssi.log
Clean and retry qssi? [y/N]
```
- `y`/`yes` means retry.
- Enter or anything else means no.
- EOF or Ctrl+C stops asking. Stages already answered `y` are still retried.
- With no terminal (CI, `| tee`, nohup, cron) it never asks; the run ends as before.

**What a retry does:**
- All chosen stages restart together as attempt n+1, each in a **new shell**. Env scripts, `envsetup.sh` and `lunch` are re-sourced.
- The orchestrator passes `ICB_ATTEMPT=<n>` and `ICB_CLEAN=1`, and each script runs its **clean step** (table below) before rebuilding.
- Stages that were BLOCKED by the failure go back to PENDING and run automatically once the retried stage passes. They stay blocked if they also depend on another stage that is still failed.
- If a stage fails again, the tool asks again. There is no fixed retry limit.
- The exit code reflects the final states.

**Logs.** `<stage>.log` is always the latest attempt. Earlier attempts are kept as `<stage>.attempt1.log`, `<stage>.attempt2.log`, and so on. Each attempt has its own header and footer, and the header includes `attempt  : 2 (clean before build)`. `orchestrator.log` records each question, the answers and each retry. The summary shows e.g. `COMPLETED (attempt 2)`.

**Code:**
- `Scheduler.can_retry(name)` and `Scheduler.retry(name)` are public, so the planned TUI (`docs/TUI_PLAN.md`) can call them from its `r` key.
- The prompt is `cli.make_retry_prompt`.
- `Display.resume()` restarts the live display after the prompt.

**Clean step per stage (on a retry):**

| Stage | Clean before re-executing |
|---|---|
| tz, xbl | nothing: re-execute |
| qssi | `rm -rf apps_qssi/LINUX/android/out` |
| vendor | `rm -rf apps_vendor/LINUX/android/out` and `apps_vendor/LINUX/android/kernel_platform/out` |
| super, meta | nothing: re-execute (super leaves Vendor's output alone) |

- For qssi and vendor, the delete runs **before** the env scripts, `envsetup.sh` and `lunch`, because lunch writes soong state into `out/`.
- Deletes go through `icb_remove_dir` (`scripts/common.sh`). It refuses any path that isn't strictly inside the stage's own directory (`ICB_STAGE_DIR`), as well as empty paths and paths containing `..`.
- In `--stub` mode the `rm -rf` is logged, not run.
- If an `out/` directory doesn't exist, nothing is deleted: the step logs `... does not exist, nothing to delete` and the build continues.
- Assumptions, confirmed for the ICB build server:
  - `out` is never a symlink, so `rm -rf` removes the real output.
  - The env scripts set `OUT_DIR`, but it's always `out`, so there's no separate `OUT_DIR` to clean.
- A full delete of `out/` means the retried QSSI/Vendor build starts from scratch, so expect a full build time.

---

## 14. Incremental build (`--incremental`)

> Status: **implemented, CLI and TUI.** The TUI part (the header's `build:` field and the `i` key) is described in `docs/TUI_PLAN.md` ("Incremental build in the TUI").

### What it does
`--incremental` rebuilds **QSSI and Vendor without deleting `out/`**. Each stage still runs in a new shell, as always, so its env setup runs again.

| Stage | Normal build | `--incremental` |
|---|---|---|
| qssi | envsetup → lunch → `build.sh -j N dist --qssi_only` | **same commands** (out/ was never deleted on a normal build either) |
| vendor | vendor-env.sh (if present) → envsetup → lunch → gki-fetch (GKI only) → `prepare_vendor.sh` → `build.sh -j N dist --target_only` | vendor-env.sh (if present) → envsetup → lunch → `build.sh -j N dist --target_only`. **The kernel steps (gki-fetch, prepare_vendor.sh) are skipped** because the kernel has no incremental build |
| tz, xbl, super, meta | unchanged | unchanged |

- **Stage selection is separate from this flag.**
  - `--incremental` alone runs the full pipeline: TZ, XBL, QSSI (incremental), Vendor (incremental), Super, Meta.
  - With `--only`/`--skip`, only the selected stages run. For example, `--incremental --only qssi,vendor,super,meta` reuses the existing TZ/XBL output.
- **No `out/` checks.** The tool doesn't verify that a previous build exists. An incremental Vendor build with no earlier kernel output fails in `build.sh` like any other build failure.

### Retry inside an incremental run
A retry (`[y/N]` prompt now, `r` in the TUI) stays a **full clean build**. It deletes `out/` (Vendor: both out dirs) and runs every command, **including the kernel steps**. The orchestrator therefore passes `ICB_INCREMENTAL=0` for attempts after the first.

### Design
- **CLI:** `--incremental` (`store_true`). It is a per-run choice like `--stub`, so it can't be set in the config file.
- **Config** (`config.py`):
  - `Config.incremental: bool`
  - `stage_env()` passes `ICB_INCREMENTAL=1` to **qssi and vendor** on attempt 1 of an incremental run. Every other case gets `0`, including other stages, whose build doesn't change, and retries. It's always passed explicitly, so a value exported in the user's shell can't leak in.
  - `to_dict()` / `resolved_config.json` include `incremental`.
- **Scripts:**
  - `common.sh`: new `icb_incremental()` helper.
  - `vendor.sh`: in incremental mode, skip gki-fetch and prepare_vendor.sh and log `incremental build: skipping kernel steps (gki-fetch, prepare_vendor.sh)`.
  - `qssi.sh`: commands unchanged, plus one log line: `incremental build: reusing out/`.
- **Log header:** a new line, `build    : incremental` (qssi/vendor in an incremental run) or `build    : full` (all other cases).
- **Run start:**
  - like stub mode, a message `INCREMENTAL BUILD: qssi/vendor reuse out/, vendor kernel steps skipped` goes to the display and `orchestrator.log`
  - `--dry-run` shows `Build mode: incremental`
- **GKI flags:** still validated as today (`--gki true` needs `--gki-version`), because a retry in an incremental run is a full build that uses them.
- **Warning:** `--incremental` without qssi or vendor selected (e.g. `--only tz`) prints `warning: --incremental only affects qssi and vendor; neither is selected` and the run continues.

### Files
- `orchestrator/cli.py`: the flag, dry-run line, run-start message
- `orchestrator/config.py`: `incremental`, env and warning
- `orchestrator/runner.py`: header line
- `orchestrator/scheduler.py`: start message
- `scripts/common.sh`, `scripts/vendor.sh`, `scripts/qssi.sh`
- `README.md`, this section
- tests:
  - `test_config.py`: env values, retry attempts get 0, the warning
  - `test_scripts.py`: incremental vendor command list without kernel steps, both GKI modes; qssi unchanged
  - `test_retry.py`: a retry in an incremental run gets `ICB_INCREMENTAL=0` and runs the kernel steps
  - `test_cli.py`: dry-run output, the flag together with `--only`/`--skip`

### Verification
Full suite on Python 3.13 and 3.8, plus manual checks:
- `--stub --incremental`: the vendor log has no gki-fetch or prepare_vendor lines
- `--stub --incremental --only qssi,vendor`: only those two run
- `ICB_STUB_FAIL=vendor --stub --incremental`, then answer `y`: the attempt-2 vendor log shows the clean step and the kernel steps

### Found while implementing: a dead output no longer kills the build
A closed output pipe (e.g. `| head`, or a reader that exited) used to raise `BrokenPipeError` in the display. That crashed the orchestrator, and its cleanup force-killed the running builds.

The scheduler now wraps its display in `GuardedDisplay` (`orchestrator/display/base.py`). When the display's output fails:
- the display is switched off and stdout is pointed at `/dev/null`
- a warning goes to `orchestrator.log`
- the build continues, and the stage logs and the exit code are unaffected
- a TUI whose terminal is gone no longer holds the run open
- a retry prompt that can't be shown counts as "no"

