import os
import threading
import unittest

from orchestrator.scheduler import EXIT_FAILED, EXIT_INTERRUPTED, EXIT_OK, Scheduler
from orchestrator.stages import StageState
from tests.helpers import QuietDisplay, WorkspaceTestCase


class FakePrompt:
    """Answers retry prompts from a script of answers; records what was offered."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.offered = []

    def __call__(self, failed):
        self.offered.append([s.name for s in failed])
        return self.answers.pop(0) if self.answers else []


class RecordingDisplay(QuietDisplay):
    def __init__(self):
        super().__init__()
        self.stops = 0
        self.resumes = 0

    def stop(self, stages, interrupted=False):
        self.stops += 1

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
        self.assertEqual((display.stops, display.resumes), (2, 1))

        first = self.read("qssi.attempt1.log")
        self.assertIn("attempt=1 clean=0", first)
        self.assertIn("result   : FAILED", first)
        latest = self.read("qssi.log")
        self.assertIn("attempt  : 2 (clean before build)\n", latest)
        self.assertIn("  ICB_ATTEMPT=2\n", latest)
        self.assertIn("  ICB_CLEAN=1\n", latest)
        self.assertIn("result   : COMPLETED", latest)
        self.assertEqual(st["qssi"].log_path, os.path.join(self.run_dir, "qssi.log"))

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
        prompt = FakePrompt(["xbl"])
        sched, code = self.run_with(self.resolve(), prompt)
        # xbl still fails (the stub hook still applies); tz was never retried
        self.assertEqual(prompt.offered, [["tz", "xbl"], ["tz", "xbl"]])
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
        def prompt(failed):
            raise KeyboardInterrupt

        sched, code = self.run_with(self.flaky_cfg(), prompt)
        self.assertEqual(code, EXIT_FAILED)
        self.assertEqual(sched.statuses["qssi"].attempt, 1)


if __name__ == "__main__":
    unittest.main()
