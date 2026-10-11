"""Run each stage script directly and check the commands it would run."""

import os
import subprocess
import unittest

from tests.helpers import WorkspaceTestCase

GKI = "android14-6.1-2026-06_r6@16037590"


class ScriptTests(WorkspaceTestCase):
    stub_sleep = "0"

    def run_stage(self, stage, cfg=None, extra_env=None):
        cfg = cfg or self.resolve()
        env = os.environ.copy()
        env.update(cfg.stage_env(stage, "test-run", self.run_dir))
        env.update(extra_env or {})
        proc = subprocess.run(
            ["bash", cfg.stages[stage].script],
            cwd=cfg.stages[stage].dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            timeout=30,
        )
        return proc.returncode, proc.stdout

    def commands(self, output):
        """The '+ ...' command lines, without the timestamp/stage prefix."""
        return [line.split("] + ", 1)[1] for line in output.splitlines() if "] + " in line]

    def test_tz(self):
        code, out = self.run_stage("tz")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.commands(out), ["python build_all.py -b TZ.XF.5.0 CHIPSET=sm8150"])
        self.assertIn("stub mode: nothing was built", out)

    def test_tz_env_override_from_config(self):
        cfg = self.resolve(overrides={"stages": {"tz": {"env": {"TZ_CHIPSET": "sm8155"}}}})
        code, out = self.run_stage("tz", cfg)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.commands(out), ["python build_all.py -b TZ.XF.5.0 CHIPSET=sm8155"])

    def test_xbl(self):
        code, out = self.run_stage("xbl")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.commands(out), [
            "source /opt/qcom-build-env/xbl-env.sh (if present)",
            "python ./buildex.py --variant AU -r RELEASE -t SDM855Pkg,QcomToolsPkg",
        ])

    def test_qssi(self):
        code, out = self.run_stage("qssi")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.commands(out), [
            "source build/envsetup.sh",
            "lunch qssi-userdebug",
            "bash build.sh -j7 dist --qssi_only",
        ])

    def test_vendor_with_user_gki(self):
        cfg = self.resolve(overrides={"stages": {"vendor": {"gki": True, "gki_version": GKI}}})
        code, out = self.run_stage("vendor", cfg)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.commands(out), [
            "source /opt/qcom-build-env/vendor-env.sh (if present)",
            "source build/envsetup.sh",
            "lunch board-userdebug",
            "./vendor/alpine/kernel/alap-linux/scripts/prebuilt_kernel_setup.sh gki-fetch -c " + GKI,
            "env ALAP_GKI_FRAGMENT=0 RECOMPILE_KERNEL=1 RECOMPILE_ABL=1 PREBUILT_ARCHIVE=1 "
            "./kernel_platform/build/android/prepare_vendor.sh gen3auto-alap gki",
            "bash build.sh -j9 dist --target_only",
        ])

    def test_vendor_without_user_gki(self):
        code, out = self.run_stage("vendor")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.commands(out), [
            "source /opt/qcom-build-env/vendor-env.sh (if present)",
            "source build/envsetup.sh",
            "lunch board-userdebug",
            "env RECOMPILE_KERNEL=1 RECOMPILE_ABL=1 PREBUILT_ARCHIVE=1 "
            "./kernel_platform/build/android/prepare_vendor.sh gen3auto-alap gki",
            "bash build.sh -j9 dist --target_only",
        ])
        self.assertIn("skipping gki-fetch", out)

    def test_super_defaults(self):
        cfg = self.resolve()
        code, out = self.run_stage("super", cfg)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.commands(out), [
            "source build/envsetup.sh",
            "lunch board-userdebug",
            "python vendor/qcom/opensource/core-utils/build/build_image_standalone.py "
            "--image super --qssi_build_path %s/ --target_build_path ./ --merged_build_path ./ "
            "--target_lunch msmnile_au --skip_qiifa" % cfg.stages["qssi"].dir,
        ])

    def test_super_optional_flags(self):
        cfg = self.resolve(overrides={"stages": {"super": {
            "target_lunch": "other_au", "output_ota": True, "no_tmp": True
        }}})
        code, out = self.run_stage("super", cfg)
        self.assertEqual(code, 0, out)
        self.assertTrue(self.commands(out)[-1].endswith(
            "--target_lunch other_au --skip_qiifa --output_ota --no_tmp"
        ))

    def test_meta(self):
        code, out = self.run_stage("meta")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.commands(out), ["python build.py --flavors=8155"])

    def test_stub_fail_hook(self):
        code, out = self.run_stage("meta", extra_env={"ICB_STUB_FAIL": "xbl,meta"})
        self.assertEqual(code, 1)
        self.assertIn("STUB: simulated failure", out)

    # -- real mode (no --stub) in a fake workspace: commands really run and fail --

    def test_real_mode_missing_envsetup_fails_clearly(self):
        code, out = self.run_stage("qssi", self.resolve(stub=False))
        self.assertNotEqual(code, 0)
        self.assertIn("ERROR: source build/envsetup.sh failed", out)

    def test_real_mode_skips_missing_env_script(self):
        cfg = self.resolve(stub=False, overrides={"stages": {"xbl": {"env": {
            "XBL_ENV_SCRIPT": os.path.join(self.tmp, "no-such-env.sh")
        }}}})
        code, out = self.run_stage("xbl", cfg)
        self.assertIn("no-such-env.sh not present, skipping", out)
        self.assertNotEqual(code, 0)  # buildex.py doesn't exist in the fake tree


if __name__ == "__main__":
    unittest.main()
