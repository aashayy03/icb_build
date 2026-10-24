import json
import os
import threading
import time
import unittest

from orchestrator.scheduler import EXIT_FAILED, EXIT_INTERRUPTED, EXIT_OK, Scheduler
from orchestrator.stages import StageState
from tests.helpers import QuietDisplay, WorkspaceTestCase
from tests.test_tui import PtyTestCase, write

try:
    import rich  # noqa: F401
    HAVE_RICH = True
except ImportError:
    HAVE_RICH = False


class FakePrompt:
    """Answers retry prompts from a script of answers; records what was offered."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.offered = []
        self.running = []  # the `running` list passed with each question

    def __call__(self, failed, running, idle):
        self.offered.append([s.name for s in failed])
        self.running.append(list(running))
        return self.answers.pop(0) if self.answers else []


class RecordingDisplay(QuietDisplay):
    def __init__(self):
        super().__init__()
        self.stops = 0
        self.pauses = 0
        self.resumes = 0

    def stop(self, stages, interrupted=False):
        self.stops += 1

    def pause(self, stages):
        self.pauses += 1

    def resume(self, stages):
        self.resumes += 1



class RetryTests(WorkspaceTestCase):
    def flaky_cfg(self, passes_on_attempt=2, **kwargs):
        """qssi uses a script that fails until ICB_ATTEMPT reaches passes_on_attempt."""
        script = self.write_script("flaky_qssi.sh", (
            'echo "attempt=$ICB_ATTEMPT clean=$ICB_CLEAN"\n'
            '[ "$ICB_ATTEMPT" -ge %d ] || { echo "simulated failure"; exit 2; }\n'
            'echo "qssi ok"\n' % passes_on_attempt
        ))
        return self.resolve(overrides={"stages": {"qssi": {"script": script}}}, **kwargs)

    def slow_vendor_cfg(self, seconds=1.5, **kwargs):
        """Flaky qssi (fails on attempt 1) next to a vendor that builds for `seconds`."""
        flaky = self.write_script("flaky_qssi.sh", (
            '[ "$ICB_ATTEMPT" -ge 2 ] || { echo "simulated failure"; exit 2; }\n'))
        slow = self.write_script("slow_vendor.sh", 'sleep %s\necho "vendor ok"\n' % seconds)
        kwargs.setdefault("only", ["qssi", "vendor", "super", "meta"])
        return self.resolve(overrides={"stages": {"qssi": {"script": flaky},
                                                  "vendor": {"script": slow}}}, **kwargs)

    def run_with(self, cfg, prompt, display=None):
        sched = Scheduler(cfg, "test-run", self.run_dir, display or RecordingDisplay(),
                          poll_interval=0.05, retry_prompt=prompt)
        return sched, sched.run()

    def read(self, name):
        with open(os.path.join(self.run_dir, name)) as fh:
            return fh.read()

    def test_retry_passes_and_unblocks_dependents(self):
        prompt = FakePrompt(["qssi"])
        display = RecordingDisplay()
        sched, code = self.run_with(self.flaky_cfg(), prompt, display)

        self.assertEqual(code, EXIT_OK)
        self.assertEqual(prompt.offered, [["qssi"]])
        st = sched.statuses
        self.assertEqual(st["qssi"].state, StageState.COMPLETED)
        self.assertEqual(st["qssi"].attempt, 2)
        self.assertEqual(st["super"].state, StageState.COMPLETED)
        self.assertEqual(st["meta"].state, StageState.COMPLETED)
        self.assertGreaterEqual(st["super"].started_at, st["qssi"].ended_at)
        self.assertEqual((display.pauses, display.resumes, display.stops), (1, 1, 1))

        first = self.read("qssi.attempt1.log")
        self.assertIn("attempt=1 clean=0", first)
        self.assertIn("result   : FAILED", first)
        latest = self.read("qssi.log")
        self.assertIn("attempt  : 2 (clean before build)\n", latest)
        self.assertIn("  ICB_ATTEMPT=2\n", latest)
        self.assertIn("  ICB_CLEAN=1\n", latest)
        self.assertIn("result   : COMPLETED", latest)
        self.assertEqual(st["qssi"].log_path, os.path.join(self.run_dir, "qssi.log"))

    def test_retry_in_incremental_run_is_full_clean_build(self):
        os.environ["ICB_STUB_FAIL"] = "vendor"
        prompt = FakePrompt(["vendor"])
        sched, _ = self.run_with(self.resolve(incremental=True), prompt)
        self.assertEqual(sched.statuses["vendor"].attempt, 2)
        first = self.read("vendor.attempt1.log")
        self.assertIn("build    : incremental", first)
        self.assertIn("skipping kernel steps", first)
        self.assertNotIn("prepare_vendor.sh gen3auto-alap", first)
        retry = self.read("vendor.log")
        self.assertIn("build    : full", retry)
        self.assertIn("  ICB_INCREMENTAL=0\n", retry)
        self.assertIn("+ rm -rf -- ", retry)  # clean step
        self.assertIn("prepare_vendor.sh gen3auto-alap gki", retry)  # kernel steps back

    def test_asked_while_another_stage_is_still_running(self):
        vendor_state = []

        def prompt(failed, running, idle):
            vendor_state.append(sched.statuses["vendor"].state)
            return ["qssi"]

        sched = Scheduler(self.slow_vendor_cfg(), "test-run", self.run_dir, RecordingDisplay(),
                          poll_interval=0.05, retry_prompt=prompt)
        self.assertEqual(sched.run(), EXIT_OK)
        self.assertEqual(vendor_state, [StageState.RUNNING])
        st = sched.statuses
        # the retry started while vendor was still building, and super waited for both
        self.assertLess(st["qssi"].started_at, st["vendor"].ended_at)
        self.assertEqual(st["qssi"].attempt, 2)
        self.assertGreaterEqual(st["super"].started_at, st["vendor"].ended_at)
        self.assertEqual(st["meta"].state, StageState.COMPLETED)

    def test_running_stages_passed_to_prompt(self):
        prompt = FakePrompt([])
        self.run_with(self.slow_vendor_cfg(), prompt)
        self.assertEqual((prompt.offered, prompt.running), ([["qssi"]], [["vendor"]]))

    def test_no_is_final(self):
        prompt = FakePrompt([])
        display = RecordingDisplay()
        sched, code = self.run_with(self.slow_vendor_cfg(), prompt, display)
        self.assertEqual(code, EXIT_FAILED)
        self.assertEqual(prompt.offered, [["qssi"]])  # not asked again at the end
        self.assertEqual(sched.statuses["vendor"].state, StageState.COMPLETED)
        self.assertEqual(sched.statuses["super"].state, StageState.BLOCKED)
        self.assertEqual((display.pauses, display.resumes, display.stops), (1, 1, 1))

    def test_no_resume_when_declined_and_run_is_over(self):
        prompt = FakePrompt([])
        display = RecordingDisplay()
        self.run_with(self.flaky_cfg(only=["qssi"]), prompt, display)
        self.assertEqual((display.pauses, display.resumes, display.stops), (1, 0, 1))

    def test_stage_finishing_during_question_keeps_its_duration(self):
        def slow_answer(failed, running, idle):
            end = time.monotonic() + 1.5
            while time.monotonic() < end:  # the user takes a while to answer
                self.assertFalse(idle())
                time.sleep(0.05)
            return []

        sched, _ = self.run_with(self.slow_vendor_cfg(seconds=0.3), slow_answer)
        vendor = sched.statuses["vendor"]
        self.assertEqual(vendor.state, StageState.COMPLETED)
        self.assertLess(vendor.elapsed(), 1.0)
        with open(os.path.join(self.run_dir, "vendor.log")) as fh:
            self.assertIn("result   : COMPLETED", fh.read())

    def test_ctrl_c_during_question_cancels_running_stages(self):
        def prompt(failed, running, idle):
            sched.request_cancel()  # what the SIGINT handler does
            self.assertTrue(idle())
            return ["qssi"]  # an answer typed before Ctrl+C is ignored

        sched = Scheduler(self.slow_vendor_cfg(seconds=30), "test-run", self.run_dir,
                          RecordingDisplay(), poll_interval=0.05, term_grace=5,
                          retry_prompt=prompt)
        self.assertEqual(sched.run(), EXIT_INTERRUPTED)
        self.assertEqual(sched.statuses["qssi"].attempt, 1)
        self.assertEqual(sched.statuses["vendor"].state, StageState.CANCELLED)

    def test_ctrl_c_during_question_with_nothing_running_only_stops_asking(self):
        def prompt(failed, running, idle):
            sched.request_cancel()
            self.assertTrue(idle())
            return []

        sched = Scheduler(self.flaky_cfg(only=["qssi"]), "test-run", self.run_dir,
                          RecordingDisplay(), poll_interval=0.05, retry_prompt=prompt)
        self.assertEqual(sched.run(), EXIT_FAILED)  # not 130: nothing was cancelled
        self.assertEqual(sched.statuses["qssi"].state, StageState.FAILED)

    def test_declining_keeps_failure(self):
        prompt = FakePrompt([])
        sched, code = self.run_with(self.flaky_cfg(), prompt)
        self.assertEqual(code, EXIT_FAILED)
        self.assertEqual(prompt.offered, [["qssi"]])
        self.assertEqual(sched.statuses["qssi"].state, StageState.FAILED)
        self.assertEqual(sched.statuses["super"].state, StageState.BLOCKED)
        self.assertEqual(sched.statuses["meta"].state, StageState.BLOCKED)
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "qssi.attempt1.log")))

    def test_repeated_failure_prompts_again(self):
        prompt = FakePrompt(["qssi"], ["qssi"])
        sched, code = self.run_with(self.flaky_cfg(passes_on_attempt=3), prompt)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(prompt.offered, [["qssi"], ["qssi"]])
        self.assertEqual(sched.statuses["qssi"].attempt, 3)
        for name in ("qssi.attempt1.log", "qssi.attempt2.log", "qssi.log"):
            self.assertTrue(os.path.exists(os.path.join(self.run_dir, name)), name)
        self.assertIn("attempt  : 3 (clean before build)", self.read("qssi.log"))

    def test_only_chosen_stages_retried(self):
        os.environ["ICB_STUB_FAIL"] = "tz,xbl"
        offered = []

        def prompt(failed, running, idle):
            offered.extend((s.name, s.attempt) for s in failed)
            return ["xbl"] if ("xbl", 1) in [(s.name, s.attempt) for s in failed] else []

        sched, code = self.run_with(self.resolve(), prompt)
        # xbl still fails (the stub hook still applies); tz was declined once, never re-asked
        self.assertEqual(sorted(offered), [("tz", 1), ("xbl", 1), ("xbl", 2)])
        self.assertEqual(sched.statuses["tz"].attempt, 1)
        self.assertEqual(sched.statuses["xbl"].attempt, 2)
        self.assertEqual(code, EXIT_FAILED)
        self.assertEqual(sched.statuses["super"].note, "blocked by tz, xbl")

    def test_super_stays_blocked_while_another_dependency_failed(self):
        os.environ["ICB_STUB_FAIL"] = "tz"
        cfg = self.flaky_cfg()
        sched = Scheduler(cfg, "test-run", self.run_dir, RecordingDisplay(), poll_interval=0.05)
        sched.run()
        sched.retry("qssi")
        self.assertEqual(sched.statuses["qssi"].state, StageState.PENDING)
        self.assertEqual(sched.statuses["super"].state, StageState.BLOCKED)
        self.assertEqual(sched.statuses["super"].note, "blocked by tz")

    def test_no_prompt_when_not_interactive(self):
        sched, code = self.run_with(self.flaky_cfg(), None)
        self.assertEqual(code, EXIT_FAILED)
        self.assertEqual(sched.statuses["qssi"].attempt, 1)

    def test_no_prompt_after_interrupt(self):
        os.environ["ICB_STUB_SLEEP"] = "30"
        prompt = FakePrompt(["qssi"])
        sched = Scheduler(self.resolve(), "test-run", self.run_dir, RecordingDisplay(),
                          poll_interval=0.05, term_grace=5, retry_prompt=prompt)
        threading.Timer(0.5, sched.request_cancel).start()
        self.assertEqual(sched.run(), EXIT_INTERRUPTED)
        self.assertEqual(prompt.offered, [])
        self.assertFalse(sched.can_retry("qssi"))

    def test_retry_refused_for_non_failed_stage(self):
        sched, _ = self.run_with(self.resolve(only=["tz"]), None)
        self.assertFalse(sched.can_retry("tz"))  # COMPLETED
        self.assertFalse(sched.can_retry("meta"))  # SKIPPED
        with self.assertRaises(ValueError):
            sched.retry("tz")

    def test_prompt_interrupt_means_no(self):
        def prompt(failed, running, idle):
            raise KeyboardInterrupt

        sched, code = self.run_with(self.flaky_cfg(), prompt)
        self.assertEqual(code, EXIT_FAILED)
        self.assertEqual(sched.statuses["qssi"].attempt, 1)


class LivePromptPtyTests(PtyTestCase):
    """The [y/N] question shows up while another stage is still building."""

    ui = "ansi"

    def spawn_slow_qssi(self, **env):
        slow = os.path.join(self.tmp, "slow_qssi.sh")
        write(slow, '#!/usr/bin/env bash\nsleep 4\necho "qssi ok"\n')
        cfg = os.path.join(self.tmp, "cfg.json")
        write(cfg, json.dumps({"stages": {"qssi": {"script": slow}}}))
        self.spawn("-c", cfg, ICB_STUB_FAIL="vendor", **env)

    def wait_log_count(self, text, count, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            self.pump(0.1)
            if self.orchestrator_log().count(text) >= count:
                return
        self.fail("timed out waiting for %d x %r in orchestrator.log" % (count, text))

    def test_retry_while_qssi_still_running(self):
        self.spawn_slow_qssi()
        self.wait_log("asking whether to retry: vendor (still running: qssi")
        self.pump(0.3)
        self.assertIn("still running: qssi", self.text())
        self.assertIn("Clean and retry vendor? [y/N]", self.text())
        self.assertNotIn("stage qssi: COMPLETED", self.orchestrator_log())
        self.keys(b"y\n")
        self.wait_log("stage vendor: RUNNING attempt=2")
        self.assertNotIn("stage qssi: COMPLETED", self.orchestrator_log())
        self.wait_log_count("asking whether to retry: vendor", 2)  # stub still fails it
        self.keys(b"n\n")
        self.assertEqual(self.finish(), 1)
        self.assertIn("FAILED (attempt 2)", self.text())

    def test_ctrl_c_at_question_cancels_qssi(self):
        self.spawn_slow_qssi()
        self.wait_log("asking whether to retry: vendor")
        self.keys(b"\x03")
        self.assertEqual(self.finish(), 130)
        self.assertIn("stage qssi: CANCELLED", self.orchestrator_log())

    def test_keys_typed_before_the_question_are_ignored(self):
        self.spawn_slow_qssi(ICB_STUB_SLEEP="0.3")  # vendor takes ~2 s to fail
        self.wait_log("stage vendor: RUNNING")
        self.keys(b"y\n", settle=0)
        self.wait_log("asking whether to retry: vendor")
        self.pump(1.0)
        self.assertNotIn("retry chosen", self.orchestrator_log())
        self.keys(b"n\n")
        self.wait_log("retry chosen: none")
        self.assertEqual(self.finish(), 1)


@unittest.skipUnless(HAVE_RICH, "rich is not installed")
class LivePromptRichPtyTests(LivePromptPtyTests):
    ui = "rich"


if __name__ == "__main__":
    unittest.main()
