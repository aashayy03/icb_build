"""Interactive curses TUI (--ui tui): stage table, live log pane, log viewer, retry.

Runs on the scheduler's main thread: keys are read in wait() between polls, and
actions call the scheduler directly (request_cancel, can_retry, retry).
"""

from __future__ import annotations

import curses
import locale
import logging
import os
import time
from typing import Callable, Dict, List, Optional, Tuple

from ..runner import fmt_duration
from ..stages import StageState, StageStatus
from .base import Display, overall_result
from .logview import ENTER_KEYS, ESC, LogViewer, tail_lines

log = logging.getLogger("icb_build")

MIN_W, MIN_H = 60, 15
MESSAGE_SECONDS = 8.0
KEY_POLL = 0.1  # seconds per getch while waiting

# Rows 0-1: header and run settings, row 2: column titles, then one row per stage.
TABLE_TOP = 3
# Stage table columns: marker+name, state, attempt, elapsed, then last output.
COL_STATE, COL_ATT, COL_ELAPSED, COL_DETAIL = 11, 22, 27, 37

MAIN_KEYS = "Up/Down select  Enter view log  r retry  c cancel run  q quit  ? help"
VIEWER_KEYS = "Up/Down PgUp/PgDn scroll  g/G top/bottom  f follow  / search  n/N next/prev  q back"
HELP_LINES = [
    "ICB build TUI - keys",
    "",
    "Main screen",
    "  Up/Down, k/j    select a stage",
    "  Enter           open the selected stage's log",
    "  r               clean and retry the selected FAILED stage",
    "  c               cancel the run (stops running stages)",
    "  q               quit (asks first if stages are still running)",
    "  Ctrl+C          cancel the run; press again to force kill",
    "",
    "Log viewer",
    "  Up/Down, PgUp/PgDn, g/G   scroll, top, bottom",
    "  f               follow the end of the log on/off",
    "  /  n  N         search, next match, previous match",
    "  q / Esc         back to the stage table",
    "",
    "Press any key to close this help.",
]


class TuiDisplay(Display):
    handles_retry = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.scr = None  # curses window while active
        self.stages = []  # type: List[StageStatus]
        self.selected = 0
        self.viewer = None  # type: Optional[LogViewer]
        self.show_help = False
        self.confirm = None  # type: Optional[Tuple[str, Callable[[], None]]]
        self.msg = ""
        self.msg_at = 0.0
        self.quit_requested = False
        self._attr = {}  # type: Dict[StageState, int]

    # -- lifecycle (called by the scheduler) ------------------------------

    def start(self, stages: List[StageStatus]) -> None:
        self.stages = stages
        locale.setlocale(locale.LC_ALL, "")
        os.environ.setdefault("ESCDELAY", "25")  # snappy Esc (set_escdelay is 3.9+)
        self.scr = curses.initscr()
        try:
            curses.noecho()
            curses.cbreak()  # not raw(): Ctrl+C still reaches the scheduler's handler
            self.scr.keypad(True)
            try:
                curses.curs_set(0)
            except curses.error:
                pass
            self._init_colors()
        except Exception:
            self._teardown()
            raise
        self._draw()

    def stop(self, stages: List[StageStatus], interrupted: bool = False) -> None:
        self.stages = stages
        self._teardown()
        self.print_summary(stages, interrupted)

    def resume(self, stages: List[StageStatus]) -> None:
        self.update(stages)  # retries happen in place; never stopped in between

    def update(self, stages: List[StageStatus]) -> None:
        self.stages = stages
        self._draw()

    def message(self, text: str) -> None:
        self._say(text)
        self._draw()

    def holds_open(self) -> bool:
        return not self.quit_requested

    def wait(self, seconds: float) -> None:
        if self.scr is None:
            time.sleep(seconds)
            return
        deadline = time.monotonic() + seconds
        while not self.quit_requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.scr.timeout(max(1, int(min(remaining, KEY_POLL) * 1000)))
            key = self.scr.getch()
            if key == -1:
                continue
            self.handle_key(key)
            self._draw()

    def _teardown(self) -> None:
        if self.scr is None:
            return
        try:
            self.scr.keypad(False)
            curses.nocbreak()
            curses.echo()
            curses.curs_set(1)
        except curses.error:
            pass
        curses.endwin()
        self.scr = None

    # -- keys ---------------------------------------------------------------

    def handle_key(self, key: int) -> None:
        if key == curses.KEY_RESIZE:
            try:
                curses.update_lines_cols()
            except curses.error:
                pass
            return
        if self.confirm is not None:
            _, action = self.confirm
            self.confirm = None
            if key in (ord("y"), ord("Y")):
                action()
            else:
                self._say("cancelled")
            return
        if self.show_help:
            self.show_help = False
            return
        if self.viewer is not None:
            if not self.viewer.handle_key(key, self._viewer_height()):
                self.viewer = None
            return

        if key in (curses.KEY_UP, ord("k")):
            self.selected = max(0, self.selected - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.selected = min(len(self.stages) - 1, self.selected + 1)
        elif key in ENTER_KEYS:
            self._open_viewer()
        elif key == ord("r"):
            self._ask_retry()
        elif key == ord("c"):
            self._ask_cancel()
        elif key == ord("q"):
            self._ask_quit()
        elif key == ord("?"):
            self.show_help = True

    def _current(self) -> Optional[StageStatus]:
        if not self.stages:
            return None
        self.selected = max(0, min(self.selected, len(self.stages) - 1))
        return self.stages[self.selected]

    def _active(self) -> bool:
        return any(not s.state.is_terminal for s in self.stages)

    def _open_viewer(self) -> None:
        s = self._current()
        if s is None:
            return
        if not s.log_path or not os.path.exists(s.log_path):
            self._say("no log yet for %s" % s.name)
            return
        title = os.path.basename(s.log_path)
        if s.attempt > 1:
            title += " (attempt %d)" % s.attempt
        self.viewer = LogViewer(title, s.log_path, follow=s.state is StageState.RUNNING)

    def _ask_retry(self) -> None:
        s, ctrl = self._current(), self.controller
        if s is None or ctrl is None:
            return
        if ctrl.can_retry(s.name):
            self.confirm = ("Clean and retry %s? (y/n)" % s.name, lambda: self._do_retry(s.name))
        elif ctrl.interrupted:
            self._say("the run was cancelled: retry is not available")
        else:
            self._say("%s is %s: only FAILED stages can be retried" % (s.name, s.state.value))

    def _do_retry(self, name: str) -> None:
        try:
            self.controller.retry(name)
        except (ValueError, OSError) as exc:
            self._say("retry failed: %s" % exc)
            return
        attempt = next(s.attempt for s in self.stages if s.name == name)
        self._say("%s queued for retry (attempt %d, clean before build)" % (name, attempt))

    def _ask_cancel(self) -> None:
        ctrl = self.controller
        if ctrl is None:
            return
        if ctrl.interrupted:
            self._say("already cancelling (Ctrl+C forces a kill)")
        elif not self._active():
            self._say("nothing to cancel: the run has finished")
        else:
            self.confirm = ("Cancel the run? Running stages will be stopped. (y/n)",
                            ctrl.request_cancel)

    def _ask_quit(self) -> None:
        ctrl = self.controller
        if not self._active() or ctrl is None:
            self.quit_requested = True
        elif ctrl.interrupted:
            self.quit_requested = True  # cancellation already under way; leave when it ends
            self._say("quitting once the stages have stopped")
        else:
            def cancel_and_quit() -> None:
                ctrl.request_cancel()
                self.quit_requested = True
            self.confirm = ("Stages are still running. Cancel them and quit? (y/n)",
                            cancel_and_quit)

    def _say(self, text: str) -> None:
        self.msg = text
        self.msg_at = time.monotonic()

    # -- drawing -----------------------------------------------------------

    def _init_colors(self) -> None:
        self._attr = {
            StageState.PENDING: curses.A_DIM,
            StageState.SKIPPED: curses.A_DIM,
            StageState.BLOCKED: curses.A_DIM,
            StageState.RUNNING: curses.A_BOLD,
            StageState.COMPLETED: 0,
            StageState.FAILED: curses.A_BOLD,
            StageState.CANCELLED: 0,
        }
        if "NO_COLOR" in os.environ or not curses.has_colors():
            return
        try:
            curses.start_color()
            try:
                curses.use_default_colors()
                bg = -1
            except curses.error:
                bg = curses.COLOR_BLACK
            for pair, fg in ((1, curses.COLOR_GREEN), (2, curses.COLOR_YELLOW),
                             (3, curses.COLOR_RED), (4, curses.COLOR_MAGENTA)):
                curses.init_pair(pair, fg, bg)
        except curses.error:
            return
        self._attr[StageState.COMPLETED] = curses.color_pair(1)
        self._attr[StageState.RUNNING] = curses.color_pair(2) | curses.A_BOLD
        self._attr[StageState.FAILED] = curses.color_pair(3) | curses.A_BOLD
        self._attr[StageState.CANCELLED] = curses.color_pair(4)

    def _viewer_height(self) -> int:
        if self.scr is None:
            return 20
        return max(1, self.scr.getmaxyx()[0] - 2)

    def _put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w - 1:
            return
        text = text[: w - 1 - x]  # never touch the last column (scrolls on some terminals)
        try:
            self.scr.addstr(y, x, text, attr)
        except curses.error:
            pass
        except UnicodeEncodeError:
            try:
                self.scr.addstr(y, x, text.encode("ascii", "replace").decode("ascii"), attr)
            except curses.error:
                pass

    def _draw(self) -> None:
        if self.scr is None:
            return
        try:
            self.scr.erase()
            h, w = self.scr.getmaxyx()
            if h < MIN_H or w < MIN_W:
                self._put(0, 0, "Too small: need %dx%d, have %dx%d" % (MIN_W, MIN_H, w, h))
                self._put(1, 0, "Resize. q: quit  Ctrl+C: cancel")
            elif self.viewer is not None:
                self._draw_viewer(h, w)
            else:
                self._draw_main(h, w)
            if self.show_help:
                self._draw_help(h, w)
            self.scr.refresh()
        except curses.error:
            pass
        except Exception:  # a drawing bug must never take the build down with it
            log.exception("TUI drawing failed")

    def _stub(self) -> bool:
        cfg = getattr(self.controller, "cfg", None)
        return bool(getattr(cfg, "stub", False))

    def config_line(self) -> str:
        """Second header line: lunch targets and the vendor GKI mode of this run."""
        cfg = getattr(self.controller, "cfg", None)
        if cfg is None:
            return ""
        qssi, vendor = cfg.stages["qssi"], cfg.stages["vendor"]
        if vendor.gki:
            gki = "user-provided %s" % vendor.gki_version
        else:
            gki = "not provided"
        build = "incremental" if getattr(cfg, "incremental", False) else "full"
        return " lunch: qssi %s, vendor %s   GKI: %s   build: %s" % (
            qssi.lunch_target, vendor.lunch_target, gki, build)

    def _draw_main(self, h: int, w: int) -> None:
        header = " ICB build  run %s  elapsed %s%s  logs %s" % (
            self.run_id, self.run_elapsed(), "  [STUB MODE]" if self._stub() else "", self.run_dir)
        self._put(0, 0, header.ljust(w), curses.A_REVERSE)
        self._put(1, 0, self.config_line().ljust(w), curses.A_REVERSE)
        self._put(2, 0, "  %-8s %-10s %-4s %-9s %s" % ("STAGE", "STATE", "ATT", "ELAPSED",
                                                       "LAST OUTPUT"), curses.A_BOLD)
        current = self._current()
        for i, s in enumerate(self.stages):
            y = TABLE_TOP + i
            chosen = s is current
            bold = curses.A_BOLD if chosen else 0
            self._put(y, 0, "%s %-8s" % (">" if chosen else " ", s.name), bold)
            self._put(y, COL_STATE, "%-10s" % s.state.value, self._attr.get(s.state, 0) | bold)
            att = str(s.attempt) if s.started_at is not None or s.attempt > 1 else "-"
            self._put(y, COL_ATT, "%-4s" % att, bold)
            self._put(y, COL_ELAPSED, "%-9s" % fmt_duration(s.elapsed()), bold)
            if s.state in (StageState.SKIPPED, StageState.BLOCKED):
                detail = s.note
            else:
                detail = s.last_line or s.note
            self._put(y, COL_DETAIL, detail, 0 if chosen else curses.A_DIM)

        sep = TABLE_TOP + len(self.stages)
        pane_top, pane_bottom = sep + 1, h - 3  # inclusive
        if current is not None:
            title = os.path.basename(current.log_path) if current.log_path else current.name
            if current.attempt > 1:
                title += " (attempt %d)" % current.attempt
            self._put(sep, 0, ("-- %s " % title).ljust(w, "-"), curses.A_DIM)
            height = pane_bottom - pane_top + 1
            if current.log_path:
                lines = tail_lines(current.log_path, height)
            else:
                lines = [current.note or "(no log yet: stage has not started)"]
            for j, line in enumerate(lines[:height]):
                self._put(pane_top + j, 0, line)

        self._put(h - 2, 0, self._message_line(), self._message_attr())
        self._put(h - 1, 0, MAIN_KEYS.ljust(w), curses.A_REVERSE)

    def _message_line(self) -> str:
        if self.confirm is not None:
            return self.confirm[0]
        if self.msg and time.monotonic() - self.msg_at < MESSAGE_SECONDS:
            return self.msg
        if self.stages and not self._active():
            interrupted = bool(getattr(self.controller, "interrupted", False))
            result = overall_result(self.stages, interrupted)
            can_retry = self.controller is not None and any(
                self.controller.can_retry(s.name) for s in self.stages)
            if can_retry:
                return ("Run finished: %s. Select a FAILED stage and press r to retry, "
                        "or q to quit." % result)
            return "Run finished: %s. Press q to quit." % result
        return ""

    def _message_attr(self) -> int:
        if self.confirm is not None:
            return curses.A_REVERSE | curses.A_BOLD
        return curses.A_BOLD

    def _draw_viewer(self, h: int, w: int) -> None:
        viewer = self.viewer
        assert viewer is not None
        height = h - 2
        viewer.refresh(height)
        self._put(0, 0, (" " + viewer.status(height)).ljust(w), curses.A_REVERSE)
        for i, line in enumerate(viewer.visible(height)):
            self._put(1 + i, 0, line)
        if viewer.search_input is not None:
            self._put(h - 1, 0, ("/" + viewer.search_input).ljust(w), curses.A_REVERSE)
        else:
            self._put(h - 1, 0, VIEWER_KEYS.ljust(w), curses.A_REVERSE)

    def _draw_help(self, h: int, w: int) -> None:
        width = min(w - 4, max(len(line) for line in HELP_LINES) + 4)
        top = max(0, (h - len(HELP_LINES) - 2) // 2)
        left = max(0, (w - width) // 2)
        self._put(top, left, "+" + "-" * (width - 2) + "+")
        for i, line in enumerate(HELP_LINES):
            self._put(top + 1 + i, left, "| " + line.ljust(width - 4)[: width - 4] + " |")
        self._put(top + 1 + len(HELP_LINES), left, "+" + "-" * (width - 2) + "+")
