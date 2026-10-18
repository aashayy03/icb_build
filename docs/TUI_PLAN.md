# ICB Build TUI: Saved Plan

> Status: **implemented.** Usage is in README.md ("Interactive TUI"). Code:
> - `orchestrator/display/tui_display.py`: curses screen and keys
> - `orchestrator/display/logview.py`: log tail, line index and viewer state, with no curses drawing
> - tests: `tests/test_tui.py`
>
> Notes from implementation:
> - The pieces are sized for a 60x15 minimum.
> - A drawing error is logged to `orchestrator.log` and never stops the build.
> - The header's second line shows the run settings: `lunch: qssi <target>, vendor <target>   GKI: user-provided <version>` (or `GKI: not provided`).

## Goal
An interactive terminal UI for a build run, opt-in with `--ui tui`. It lets you:
- watch each stage and its log live
- open a full log
- control the run from the keyboard
- retry a FAILED stage with `r`

The existing displays stay unchanged and stay the default: the rich/ANSI live table in a terminal, plain lines in CI or when piped.

## Decisions (agreed)
- **Library:** `curses` from the standard library. No new dependency, and it works on Python 3.8.2.
- **Activation:** only with `--ui tui`. It fails with exit 2 if stdout isn't a terminal.
- **v1 features:**
  - live log pane
  - full-screen log viewer
  - keyboard controls
  - retry of FAILED stages from the TUI

## Screen
```
+ ICB build  run 20261018-101500  elapsed 00:42:10  logs .../20261018-101500 --------+
| STAGE    STATE      ATT  ELAPSED   LAST OUTPUT                                    |
| tz       COMPLETED  1    00:04:12  tz done                                        |
| xbl      COMPLETED  1    00:06:01  xbl done                                       |
|>qssi     FAILED     1    00:38:40  ninja: build stopped: subcommand failed.       |
| vendor   RUNNING    1    00:42:10  + bash build.sh -j60 dist --target_only        |
| super    BLOCKED    -    -         blocked by qssi                                |
| meta     BLOCKED    -    -         blocked by super                               |
+ qssi.log ------------------------------------------------------------------------+
| FAILED: out/soong/.intermediates/...                                              |
| ninja: build stopped: subcommand failed.                                          |
+ message: qssi FAILED. Press r to clean and retry ---------------------------------+
  Up/Down select  Enter full log  r retry  c cancel run  q quit  ? help
```
Parts, top to bottom:
1. **Header:** run ID, total elapsed time, log folder, and `STUB MODE` when relevant.
2. **Stage table:** state (coloured), attempt, elapsed time and last output. `>` marks the selected row.
3. **Log pane:** a live tail of the selected stage's current `<stage>.log`. It reads only the end of the file, and each line is cleaned with `runner.clean_line`.
4. **Message line:** scheduler messages, confirm prompts, and the result banner when the run finishes.
5. **Footer:** key hints.

## Keys

Main screen:

| Key | Action |
|---|---|
| Up/Down, k/j | select stage |
| Enter | full-screen log viewer for the selected stage |
| r | retry the selected FAILED stage: confirm `Clean and retry qssi? y/n`, then call `Scheduler.retry()` |
| c | cancel the run (confirm), same as the first Ctrl+C |
| q | quit. If stages are running: confirm, then cancel and quit once everything has stopped |
| ? | help overlay |

Full-screen log viewer:

| Key | Action |
|---|---|
| Up/Down, PgUp/PgDn | scroll |
| g / G | top / bottom |
| f | follow on/off (on by default while the stage runs) |
| / and n | search, next match |
| q / Esc | back to the main screen |

- The viewer builds a line-offset index of the log incrementally, so large logs open fast.
- Ctrl+C keeps its current meaning: first press stops gracefully, second press force-kills. curses runs in cbreak mode, not raw, so SIGINT still reaches the scheduler's handler.

## Behaviour
- The TUI **stays open after the run ends** until `q`, so you can browse logs and retry failures.
- `r` works while other stages are still running: the retried stage starts right away.
- In TUI mode the end-of-run `[y/N]` prompt is not used; the `r` key replaces it.
- After `q`, curses is shut down and the normal text summary is printed to the terminal.

## Design

### Display ↔ scheduler contract
Additions to `orchestrator/display/base.py`. The defaults keep the existing displays unchanged.
- `attach(controller)`: the scheduler passes itself in, so the TUI can call `request_cancel()`, `can_retry(name)` and `retry(name)`.
- `wait(seconds)`: replaces `time.sleep(poll_interval)` in the scheduler's poll loop. The default sleeps; the TUI reads keys for that time (curses `timeout(50)`).
- `holds_open() -> bool`: default False. The scheduler loop also requires `not display.holds_open()` before it ends.
- `Scheduler.run()` skips the end-of-run prompt when the display handles retries itself, e.g. a `display.handles_retry` flag.

### New module
`orchestrator/display/tui_display.py` (`TuiDisplay`):
- Set up and tear down the terminal in `start()` / `stop()`. The scheduler already calls `stop()` in a `finally`.
- Handle `KEY_RESIZE`.
- Below about 60x15, show "terminal too small".
- ASCII borders. Colour only if `curses.has_colors()`, and `NO_COLOR` is respected.
- Wiring:
  - `select_display()`: add `tui`
  - `config.UI_MODES`: add `tui`
  - argparse `--ui` choices: add `tui`

### Reused
- `runner.clean_line`, `runner.fmt_duration`
- `display.base.STATE_*`, `result_label`, `stage_detail`, `overall_result`
- `Scheduler.request_cancel`, `can_retry`, `retry`

## Tests
- **Pure helpers:** log tail reader, line index and search, row truncation, key-to-action mapping.
- **pty smoke tests** with the stdlib `pty` module:
  - `icb_build.py --ui tui --stub`, wait, send `q`: exit code 0, terminal restored, summary printed
  - with `ICB_STUB_FAIL=qssi`, send `r`, `y`, then `q`: `qssi.attempt1.log` exists and the attempt count shows 2
- Run on Python 3.13 and 3.8.20.

## Decided after implementation
- **Show the vendor GKI mode and lunch targets in the header?** Yes. The second header line shows the QSSI and Vendor lunch targets and the GKI mode (user-provided plus its version, or not provided).
- **Add a "retry all failed" key?** No. Retries are one stage at a time with `r`.
