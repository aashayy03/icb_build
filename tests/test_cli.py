import contextlib
import io
import json
import os
import unittest

from orchestrator.cli import main, make_retry_prompt
from orchestrator.stages import StageState, StageStatus
from tests.helpers import WorkspaceTestCase


class CliTests(WorkspaceTestCase):
    def base_args(self):
        return ["-r", self.root, "--qssi-lunch", "qssi-userdebug",
                "--vendor-lunch", "board-userdebug", "--log-dir", os.path.join(self.tmp, "logs"),
                "--stub"]

    def call(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        cwd = os.getcwd()
        os.chdir(self.tmp)  # keep any ./icb_build.yaml in the repo out of the way
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = main(list(argv))
                except SystemExit as exc:
                    code = exc.code
        finally:
            os.chdir(cwd)
        return code, out.getvalue(), err.getvalue()

    def test_dry_run(self):
        code, out, err = self.call(*self.base_args(), "--qssi-jobs", "12", "--gki", "true",
                                   "--gki-version", "android14-6.1-2026-06_r6@16037590",
                                   "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("QSSI_JOBS=12", out)
        self.assertIn("GKI_ENABLED=true", out)
        self.assertIn("GKI_VERSION=android14-6.1-2026-06_r6@16037590", out)
        self.assertIn("super   RUN   after: tz, xbl, qssi, vendor", out)
        self.assertIn("Stub mode:   on", out)
        self.assertIn("ICB_STUB=1", out)
        self.assertEqual(os.listdir(os.path.join(self.tmp, "logs")), ["run"])  # no new run dir

    def test_dry_run_incremental_with_selection(self):
        code, out, err = self.call(*self.base_args(), "--incremental", "--skip", "tz,xbl",
                                   "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("Build mode:  incremental", out)
        self.assertIn("tz      SKIP (not selected)", out)
        self.assertIn("vendor  RUN", out)
        self.assertIn("ICB_INCREMENTAL=1", out)
        self.assertIn('"incremental": true', out)

    def test_incremental_run_only_builds_selected_stages(self):
        code, out, err = self.call(*self.base_args(), "--ui", "plain", "--incremental",
                                   "--only", "qssi,vendor")
        self.assertEqual(code, 0, out + err)
        self.assertIn("INCREMENTAL BUILD", out)
        run_dir = os.path.realpath(os.path.join(self.tmp, "logs", "latest"))
        logs = sorted(f for f in os.listdir(run_dir) if f.endswith(".log"))
        self.assertEqual(logs, ["orchestrator.log", "qssi.log", "vendor.log"])

    def test_incremental_warning_when_not_applicable(self):
        code, _, err = self.call(*self.base_args(), "--incremental", "--only", "tz", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("warning: --incremental only affects qssi and vendor", err)

    def test_closed_output_pipe_does_not_kill_build(self):
        import subprocess
        import sys
        from orchestrator.config import TOOL_ROOT
        proc = subprocess.Popen(
            [sys.executable, os.path.join(TOOL_ROOT, "icb_build.py")] + self.base_args()
            + ["--ui", "plain"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.tmp,
            env=dict(os.environ, ICB_STUB_SLEEP="0.1"),
        )
        proc.stdout.readline()
        proc.stdout.close()  # like `| head -1`
        _, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, err)
        self.assertNotIn(b"Traceback", err)
        run_dir = os.path.realpath(os.path.join(self.tmp, "logs", "latest"))
        with open(os.path.join(run_dir, "meta.log")) as fh:
            self.assertIn("result   : COMPLETED", fh.read())
        with open(os.path.join(run_dir, "orchestrator.log")) as fh:
            self.assertIn("continuing without terminal output", fh.read())

    def test_jobs_over_limit_exits_2(self):
        code, _, err = self.call(*self.base_args(), "--vendor-jobs", "61")
        self.assertEqual(code, 2)
        self.assertIn("--vendor-jobs / stages.vendor.jobs = 61 is out of range", err)

    def test_root_dir_is_required(self):
        code, _, err = self.call("--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("--root-dir", err)

    def test_only_and_skip_are_exclusive(self):
        code, _, _ = self.call(*self.base_args(), "--only", "tz", "--skip", "xbl")
        self.assertEqual(code, 2)

    def test_bad_gki_value(self):
        code, _, err = self.call(*self.base_args(), "--gki", "maybe")
        self.assertEqual(code, 2)
        self.assertIn("expected true or false", err)

    def test_full_run_writes_logs(self):
        code, out, err = self.call(*self.base_args(), "--ui", "plain")
        self.assertEqual(code, 0, out + err)
        logs = os.path.join(self.tmp, "logs")
        latest = os.path.join(logs, "latest")
        self.assertTrue(os.path.islink(latest))
        run_dir = os.path.realpath(latest)
        files = sorted(os.listdir(run_dir))
        self.assertEqual(files, ["meta.log", "orchestrator.log", "qssi.log",
                                 "resolved_config.json", "super.log", "tz.log",
                                 "vendor.log", "xbl.log"])
        with open(os.path.join(run_dir, "resolved_config.json")) as fh:
            data = json.load(fh)
        self.assertEqual(data["run_id"], os.path.basename(run_dir))
        self.assertEqual(data["stages"]["qssi"]["lunch_target"], "qssi-userdebug")
        with open(os.path.join(run_dir, "orchestrator.log")) as fh:
            olog = fh.read()
        self.assertIn("stage super: COMPLETED", olog)
        self.assertIn("finished with exit code 0", olog)
        self.assertIn("Result: SUCCESS", out)

        # a second run gets its own folder and moves the latest link
        code, _, _ = self.call(*self.base_args(), "--ui", "plain", "--only", "tz")
        self.assertEqual(code, 0)
        self.assertNotEqual(os.path.realpath(latest), run_dir)
        self.assertTrue(os.path.isdir(run_dir))


class RetryPromptTests(unittest.TestCase):
    def failed(self, *names):
        return [StageStatus(n, state=StageState.FAILED, exit_code=2,
                            log_path="/logs/%s.log" % n) for n in names]

    def ask(self, answers, *names):
        out = io.StringIO()
        chosen = make_retry_prompt(io.StringIO(answers), out)(self.failed(*names))
        return chosen, out.getvalue()

    def test_yes_and_no(self):
        chosen, out = self.ask("y\n\n", "tz", "qssi")
        self.assertEqual(chosen, ["tz"])
        self.assertIn("qssi FAILED (attempt 1, exit 2), log: /logs/qssi.log", out)
        self.assertIn("Clean and retry qssi? [y/N] ", out)

    def test_yes_variants(self):
        self.assertEqual(self.ask("YES\n", "vendor")[0], ["vendor"])
        self.assertEqual(self.ask(" Y \n", "vendor")[0], ["vendor"])
        self.assertEqual(self.ask("no\n", "vendor")[0], [])
        self.assertEqual(self.ask("sure\n", "vendor")[0], [])

    def test_eof_skips_remaining(self):
        self.assertEqual(self.ask("y\n", "tz", "xbl", "qssi")[0], ["tz"])
        self.assertEqual(self.ask("", "tz")[0], [])


if __name__ == "__main__":
    unittest.main()
