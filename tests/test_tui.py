"""TUI tests: pure log helpers, key handling with a fake controller, and pty smoke tests."""

import curses
import json
import os
import pty
import select
import shutil
import signal
import sys
import tempfile
import time
import unittest

from orchestrator.config import TOOL_ROOT
from orchestrator.display.logview import LogIndex, LogViewer, tail_lines
from orchestrator.display.tui_display import TuiDisplay
from orchestrator.stages import StageState, StageStatus
from tests.helpers import make_workspace


def write(path, text, mode="w"):
    with open(path, mode) as fh:
        fh.write(text)


class LogHelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="icb_tui_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "x.log")

    def test_tail_lines(self):
        write(self.path, "".join("line %d\n" % i for i in range(100)))
        self.assertEqual(tail_lines(self.path, 3), ["line 97", "line 98", "line 99"])
        self.assertEqual(tail_lines(self.path, 3, max_bytes=20), ["line 98", "line 99"])
        self.assertEqual(tail_lines(os.path.join(self.tmp, "missing.log"), 3), [])
        self.assertEqual(tail_lines(None, 3), [])

    def test_tail_cleans_control_sequences(self):
        write(self.path, "plain\n\x1b[32mgreen\x1b[0m\nprogress 10%\rprogress 90%\n")
        self.assertEqual(tail_lines(self.path, 3), ["plain", "green", "progress 90%"])

    def test_index_grows_incrementally_and_handles_partial_line(self):
        write(self.path, "a\nb\n")
        idx = LogIndex(self.path)
        idx.refresh()
        self.assertEqual(idx.count(), 2)
        write(self.path, "c-partial", "a")
        idx.refresh()
        self.assertEqual(idx.count(), 3)
        self.assertEqual(idx.lines(1, 5), ["b", "c-partial"])
        write(self.path, "-done\nd\n", "a")
        idx.refresh()
        self.assertEqual(idx.lines(0, 10), ["a", "b", "c-partial-done", "d"])

    def test_index_resets_when_file_is_replaced(self):
        write(self.path, "".join("old %d\n" % i for i in range(50)))
        idx = LogIndex(self.path)
        idx.refresh()
        os.replace(self.path, self.path + ".attempt1")  # what a retry does
        write(self.path, "new 0\n")
        idx.refresh()
        self.assertEqual(idx.lines(0, 10), ["new 0"])

    def test_index_search(self):
        write(self.path, "".join("line %d%s\n" % (i, " ERROR" if i in (5, 4000) else "")
                                 for i in range(5000)))
        idx = LogIndex(self.path)
        idx.refresh()
        self.assertEqual(idx.search("error", 0), 5)
        self.assertEqual(idx.search("error", 6), 4000)
        self.assertIsNone(idx.search("error", 4001))
        self.assertEqual(idx.search("error", 3999, backward=True), 5)
        self.assertEqual(idx.search("ERROR", 4999, backward=True), 4000)

    def test_viewer_scroll_follow_and_search(self):
        write(self.path, "".join("line %d\n" % i for i in range(100)))
        v = LogViewer("x.log", self.path, follow=True)
        v.refresh(10)
        self.assertEqual(v.top, 90)  # following: shows the end
        self.assertTrue(v.handle_key(curses.KEY_UP, 10))
        self.assertFalse(v.follow)
        self.assertEqual(v.top, 89)
        v.handle_key(ord("g"), 10)
        self.assertEqual(v.visible(2), ["line 0", "line 1"])
        v.handle_key(curses.KEY_NPAGE, 10)
        self.assertEqual(v.top, 9)
        v.handle_key(ord("G"), 10)
        self.assertEqual(v.top, 90)
        v.handle_key(ord("f"), 10)
        self.assertTrue(v.follow)

        v.handle_key(ord("g"), 10)
        v.handle_key(ord("/"), 10)
        for ch in "line 4x":
            v.handle_key(ord(ch), 10)
        v.handle_key(curses.KEY_BACKSPACE, 10)  # -> "line 4"
        v.handle_key(10, 10)
        self.assertEqual(v.pattern, "line 4")
        self.assertEqual(v.top, 4)
        v.handle_key(ord("n"), 10)
        self.assertEqual(v.top, 40)
        v.handle_key(ord("N"), 10)
        self.assertEqual(v.top, 4)
        self.assertIn("/line 4", v.status(10))
        self.assertFalse(v.handle_key(ord("q"), 10))


class FakeController:
    def __init__(self, stages):
        self.stages = {s.name: s for s in stages}
        self.interrupted = False
        self.retried = []
        self.cancels = 0

    def can_retry(self, name):
        return not self.interrupted and self.stages[name].state is StageState.FAILED

    def retry(self, name):
        self.retried.append(name)
        self.stages[name].attempt += 1
        self.stages[name].state = StageState.PENDING

    def request_cancel(self):
        self.cancels += 1
        self.interrupted = True


def tui_keys_footer():
    from orchestrator.display.tui_display import MAIN_KEYS
    return MAIN_KEYS


class TuiKeyTests(unittest.TestCase):
    """Key handling without a terminal (drawing is skipped while curses is inactive)."""

    def make(self, states):
        stages = [StageStatus(n, state=s) for n, s in zip(
            ["tz", "xbl", "qssi", "vendor", "super", "meta"], states)]
        tui = TuiDisplay("run", "/logs")
        tui.stages = stages
        ctrl = FakeController(stages)
        tui.attach(ctrl)
        return tui, ctrl, stages

    DONE_WITH_FAILURE = [StageState.COMPLETED, StageState.COMPLETED, StageState.FAILED,
                         StageState.COMPLETED, StageState.BLOCKED, StageState.BLOCKED]

    def test_retry_asks_then_retries_selected_failed_stage(self):
        tui, ctrl, _ = self.make(self.DONE_WITH_FAILURE)
        tui.handle_key(curses.KEY_DOWN)
        tui.handle_key(ord("j"))
        self.assertEqual(tui.selected, 2)
        tui.handle_key(ord("r"))
        self.assertEqual(tui.confirm[0], "Clean and retry qssi? (y/n)")
        tui.handle_key(ord("y"))
        self.assertEqual(ctrl.retried, ["qssi"])
        self.assertIn("queued for retry (attempt 2", tui.msg)

    def test_no_incremental_key(self):
        # incremental builds are chosen with --incremental on the command line, not in the TUI
        tui, ctrl, _ = self.make(self.DONE_WITH_FAILURE)
        tui.selected = 2
        tui.handle_key(ord("i"))
        self.assertIsNone(tui.confirm)
        self.assertEqual(ctrl.retried, [])
        self.assertNotIn(" i ", tui_keys_footer())

    def test_retry_declined_or_not_allowed(self):
        tui, ctrl, _ = self.make(self.DONE_WITH_FAILURE)
        tui.selected = 2
        tui.handle_key(ord("r"))
        tui.handle_key(ord("n"))
        self.assertEqual(ctrl.retried, [])
        tui.selected = 0
        tui.handle_key(ord("r"))
        self.assertIsNone(tui.confirm)
        self.assertIn("only FAILED stages can be retried", tui.msg)

    def test_quit_when_finished_and_while_running(self):
        tui, ctrl, _ = self.make(self.DONE_WITH_FAILURE)
        self.assertTrue(tui.holds_open())
        tui.handle_key(ord("q"))
        self.assertFalse(tui.holds_open())
        self.assertEqual(ctrl.cancels, 0)

        running = [StageState.RUNNING] * 4 + [StageState.PENDING] * 2
        tui, ctrl, _ = self.make(running)
        tui.handle_key(ord("q"))
        self.assertIn("Cancel them and quit?", tui.confirm[0])
        tui.handle_key(ord("y"))
        self.assertEqual(ctrl.cancels, 1)
        self.assertFalse(tui.holds_open())

    def test_cancel(self):
        tui, ctrl, _ = self.make([StageState.RUNNING] * 4 + [StageState.PENDING] * 2)
        tui.handle_key(ord("c"))
        tui.handle_key(ord("y"))
        self.assertEqual(ctrl.cancels, 1)
        tui.handle_key(ord("c"))  # already cancelling: no second (force-kill) request
        self.assertEqual(ctrl.cancels, 1)
        self.assertIn("already cancelling", tui.msg)
        self.assertTrue(tui.holds_open())  # stays open after cancel until q

    def test_header_shows_lunch_targets_and_gki_mode(self):
        from types import SimpleNamespace as NS
        tui, ctrl, _ = self.make(self.DONE_WITH_FAILURE)
        vendor = NS(lunch_target="icb8155-user", gki=True,
                    gki_version="android14-6.1-2026-06_r6@16037590")
        ctrl.cfg = NS(stub=False, incremental=False,
                      stages={"qssi": NS(lunch_target="icbssi-user"), "vendor": vendor})
        self.assertEqual(
            tui.config_line(),
            " lunch: qssi icbssi-user, vendor icb8155-user   "
            "GKI: user-provided android14-6.1-2026-06_r6@16037590   build: full")
        vendor.gki = False
        ctrl.cfg.incremental = True
        self.assertTrue(tui.config_line().endswith("GKI: not provided   build: incremental"))

    def test_viewer_and_help(self):
        tmp = tempfile.mkdtemp(prefix="icb_tui_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        tui, _, stages = self.make(self.DONE_WITH_FAILURE)
        tui.handle_key(10)
        self.assertIsNone(tui.viewer)
        self.assertIn("no log yet for tz", tui.msg)
        stages[0].log_path = os.path.join(tmp, "tz.log")
        write(stages[0].log_path, "hello\n")
        tui.handle_key(10)
        self.assertEqual(tui.viewer.title, "tz.log")
        tui.handle_key(ord("q"))
        self.assertIsNone(tui.viewer)
        tui.handle_key(ord("?"))
        self.assertTrue(tui.show_help)
        tui.handle_key(ord("x"))
        self.assertFalse(tui.show_help)


class PtyTestCase(unittest.TestCase):
    """Run icb_build.py --stub --ui <ui> in a pseudo-terminal and drive it with keys."""

    ui = "tui"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="icb_tui_pty_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = make_workspace(self.tmp)
        self.logs = os.path.join(self.tmp, "logs")
        self.pid = None
        self.out = bytearray()

    def tearDown(self):
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGKILL)
                os.waitpid(self.pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass

    def spawn(self, *extra, **env_extra):
        env = dict(os.environ, TERM="xterm", ICB_STUB_SLEEP="0.05")
        env.pop("ICB_STUB_FAIL", None)
        env.update(env_extra)
        pid, fd = pty.fork()
        if pid == 0:  # child
            os.chdir(self.tmp)
            os.execve(sys.executable, [sys.executable, os.path.join(TOOL_ROOT, "icb_build.py"),
                                   "-r", self.root, "--stub", "--log-dir", self.logs,
                                   "--ui", self.ui] + list(extra), env)
        self.pid, self.fd = pid, fd

    def pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.05)
            if r:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:
                    return
                if not data:
                    return
                self.out.extend(data)

    def orchestrator_log(self):
        try:
            with open(os.path.join(self.logs, "latest", "orchestrator.log")) as fh:
                return fh.read()
        except OSError:
            return ""

    def wait_log(self, text, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            self.pump(0.1)
            if text in self.orchestrator_log():
                return
        self.fail("timed out waiting for %r in orchestrator.log" % text)

    def keys(self, data, settle=0.3):
        os.write(self.fd, data)
        self.pump(settle)

    def finish(self, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            self.pump(0.1)
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.pid = None
                self.pump(0.2)
                return status >> 8
        self.fail("TUI did not exit")

    def text(self):
        return self.out.decode("utf-8", "replace")


class TuiPtyTests(PtyTestCase):
    def test_success_then_quit(self):
        self.spawn()
        self.wait_log("stage meta: COMPLETED")
        self.pump(0.5)  # the TUI stays open after the run
        self.assertIsNotNone(self.pid)
        self.keys(b"q")
        self.assertEqual(self.finish(), 0)
        self.assertIn("Result: SUCCESS", self.text())  # summary printed after curses exits

    def test_retry_from_tui(self):
        flaky = os.path.join(self.tmp, "flaky.sh")
        write(flaky, '#!/usr/bin/env bash\n[ "$ICB_ATTEMPT" -ge 2 ] || exit 2\necho ok\n')
        cfg = os.path.join(self.tmp, "cfg.json")
        write(cfg, json.dumps({"stages": {"qssi": {"script": flaky}}}))
        self.spawn("-c", cfg)
        self.wait_log("stage meta: BLOCKED")
        self.keys(b"jj")  # select qssi
        self.keys(b"r")
        self.keys(b"y")
        self.wait_log("stage meta: COMPLETED")
        self.keys(b"q")
        self.assertEqual(self.finish(), 0)
        run_dir = os.path.realpath(os.path.join(self.logs, "latest"))
        self.assertTrue(os.path.exists(os.path.join(run_dir, "qssi.attempt1.log")))
        self.assertIn("COMPLETED (attempt 2)", self.text())

    def test_incremental_flag_with_tui(self):
        self.spawn("--incremental")
        self.wait_log("stage meta: COMPLETED")
        self.keys(b"q")
        self.assertEqual(self.finish(), 0)
        run_dir = os.path.realpath(os.path.join(self.logs, "latest"))
        with open(os.path.join(run_dir, "vendor.log")) as fh:
            log = fh.read()
        self.assertIn("build    : incremental", log)
        self.assertIn("skipping kernel steps", log)

    def test_quit_while_running_cancels(self):
        self.spawn(ICB_STUB_SLEEP="30")
        self.wait_log("stage vendor: RUNNING")
        self.keys(b"q")
        self.keys(b"y")
        self.assertEqual(self.finish(), 130)
        self.assertIn("Result: INTERRUPTED", self.text())
        self.assertIn("stage qssi: CANCELLED", self.orchestrator_log())

    def test_ctrl_c_cancels_and_stays_open(self):
        self.spawn(ICB_STUB_SLEEP="30")
        self.wait_log("stage vendor: RUNNING")
        self.keys(b"\x03")  # Ctrl+C through the terminal
        self.wait_log("stage qssi: CANCELLED")
        self.pump(0.5)
        self.assertIsNotNone(self.pid)  # still open for browsing logs
        self.keys(b"q")
        self.assertEqual(self.finish(), 130)

    def test_tui_requires_a_terminal(self):
        import subprocess
        proc = subprocess.run(
            [sys.executable, os.path.join(TOOL_ROOT, "icb_build.py"), "-r", self.root, "--stub",
             "--log-dir", self.logs, "--ui", "tui"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, cwd=self.tmp,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--ui tui needs an interactive terminal", proc.stderr)


if __name__ == "__main__":
    unittest.main()
