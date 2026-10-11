import os
import shutil
import threading
import unittest

from orchestrator.scheduler import EXIT_FAILED, EXIT_INTERRUPTED, EXIT_OK, Scheduler
from orchestrator.stages import StageState
from tests.helpers import QuietDisplay, WorkspaceTestCase

PARALLEL = ["tz", "xbl", "qssi", "vendor"]


class SchedulerTests(WorkspaceTestCase):
    def run_scheduler(self, cfg, **kwargs):
        kwargs.setdefault("poll_interval", 0.05)
        sched = Scheduler(cfg, "test-run", self.run_dir, QuietDisplay(), **kwargs)
        return sched, sched.run()

    def log_text(self, stage):
        with open(os.path.join(self.run_dir, stage + ".log")) as fh:
            return fh.read()

    def has_log(self, stage):
        return os.path.exists(os.path.join(self.run_dir, stage + ".log"))

    def states(self, sched):
        return {n: s.state for n, s in sched.statuses.items()}

    def test_happy_path_runs_in_order(self):
        sched, code = self.run_scheduler(self.resolve())
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(all(s is StageState.COMPLETED for s in self.states(sched).values()))
        st = sched.statuses
        # the four standalone builds overlap in time
        self.assertLess(max(st[n].started_at for n in PARALLEL),
                        min(st[n].ended_at for n in PARALLEL))
        # super only after all four, meta only after super
        self.assertGreaterEqual(st["super"].started_at, max(st[n].ended_at for n in PARALLEL))
        self.assertGreaterEqual(st["meta"].started_at, st["super"].ended_at)

    def test_failure_lets_others_finish_and_blocks_super_meta(self):
        os.environ["ICB_STUB_FAIL"] = "qssi"
        sched, code = self.run_scheduler(self.resolve())
        self.assertEqual(code, EXIT_FAILED)
        states = self.states(sched)
        self.assertEqual(states["qssi"], StageState.FAILED)
        for name in ("tz", "xbl", "vendor"):
            self.assertEqual(states[name], StageState.COMPLETED)
        self.assertEqual(states["super"], StageState.BLOCKED)
        self.assertEqual(states["meta"], StageState.BLOCKED)
        self.assertEqual(sched.statuses["super"].note, "blocked by qssi")
        self.assertFalse(self.has_log("super"))
        self.assertFalse(self.has_log("meta"))
        log = self.log_text("qssi")
        self.assertIn("exit code: 1\n", log)
        self.assertIn("result   : FAILED\n", log)

    def test_skip_counts_as_done(self):
        sched, code = self.run_scheduler(self.resolve(skip=["tz", "xbl"]))
        self.assertEqual(code, EXIT_OK)
        states = self.states(sched)
        self.assertEqual(states["tz"], StageState.SKIPPED)
        self.assertEqual(states["super"], StageState.COMPLETED)
        self.assertEqual(states["meta"], StageState.COMPLETED)
        self.assertFalse(self.has_log("tz"))

    def test_only_meta(self):
        sched, code = self.run_scheduler(self.resolve(only=["meta"]))
        self.assertEqual(code, EXIT_OK)
        ran = [n for n, s in self.states(sched).items() if s is not StageState.SKIPPED]
        self.assertEqual(ran, ["meta"])
        self.assertEqual(sorted(os.listdir(self.run_dir)), ["meta.log"])

    def test_log_header_and_footer(self):
        cfg = self.resolve(only=["qssi"], overrides={"stages": {"qssi": {"env": {"EXTRA": "1"}}}})
        self.run_scheduler(cfg)
        log = self.log_text("qssi")
        self.assertTrue(log.startswith("=" * 80 + "\nICB BUILD  stage=qssi  run=test-run\n"))
        self.assertIn("command  : bash %s\n" % cfg.stages["qssi"].script, log)
        self.assertIn("cwd      : %s\n" % cfg.stages["qssi"].dir, log)
        self.assertIn("  QSSI_JOBS=7\n", log)
        self.assertIn("  QSSI_LUNCH_TARGET=qssi-userdebug\n", log)
        self.assertIn("  EXTRA=1\n", log)
        self.assertNotIn("ICB_STUB_SLEEP", log.split("=" * 80)[1])  # only passed vars in header
        self.assertIn("  ICB_STUB=1\n", log)
        self.assertIn("+ lunch qssi-userdebug", log)  # script output between
        self.assertTrue(log.rstrip().endswith("=" * 80))
        self.assertIn("exit code: 0\nresult   : COMPLETED\n", log)

    def test_start_failure_is_reported(self):
        cfg = self.resolve(only=["tz"])
        shutil.rmtree(cfg.stages["tz"].dir)  # vanishes after validation
        sched, code = self.run_scheduler(cfg)
        self.assertEqual(code, EXIT_FAILED)
        self.assertEqual(sched.statuses["tz"].state, StageState.FAILED)
        log = self.log_text("tz")
        self.assertIn("failed to start", log)
        self.assertIn("result   : FAILED", log)


class CancelTests(WorkspaceTestCase):
    stub_sleep = "30"

    def test_interrupt_cancels_running_and_blocks_pending(self):
        cfg = self.resolve()
        sched = Scheduler(cfg, "test-run", self.run_dir, QuietDisplay(),
                          poll_interval=0.05, term_grace=5)
        threading.Timer(0.5, sched.request_cancel).start()
        code = sched.run()
        self.assertEqual(code, EXIT_INTERRUPTED)
        for name in PARALLEL:
            self.assertEqual(sched.statuses[name].state, StageState.CANCELLED)
        self.assertEqual(sched.statuses["super"].state, StageState.BLOCKED)
        self.assertEqual(sched.statuses["meta"].state, StageState.BLOCKED)
        with open(os.path.join(self.run_dir, "qssi.log")) as fh:
            log = fh.read()
        self.assertIn("result   : CANCELLED", log)
        self.assertIn("killed by SIGTERM", log)
        self.assert_groups_gone(sched)

    def test_term_ignoring_stage_is_force_killed(self):
        script = self.write_script(
            "stubborn.sh", "trap '' TERM\necho started\nsleep 30 & wait; sleep 30 & wait\n"
        )
        cfg = self.resolve(only=["tz"], overrides={"stages": {"tz": {"script": script}}})
        sched = Scheduler(cfg, "test-run", self.run_dir, QuietDisplay(),
                          poll_interval=0.05, term_grace=0.5)
        threading.Timer(0.5, sched.request_cancel).start()
        self.assertEqual(sched.run(), EXIT_INTERRUPTED)
        self.assertEqual(sched.statuses["tz"].state, StageState.CANCELLED)
        self.assertIn("SIGKILL", sched.statuses["tz"].note)
        self.assert_groups_gone(sched)

    def assert_groups_gone(self, sched):
        for name, proc in sched._procs.items():
            with self.assertRaises(ProcessLookupError, msg="%s left processes" % name):
                os.killpg(proc.pid, 0)


if __name__ == "__main__":
    unittest.main()
