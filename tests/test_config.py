import json
import os
import sys
import unittest
from unittest import mock

from orchestrator.config import (
    MAX_JOBS,
    SOURCE_ROOT,
    TOOL_ROOT,
    ConfigError,
    default_jobs,
    parse_bool,
    select_stages,
)
from tests.helpers import WorkspaceTestCase

try:
    import yaml  # noqa: F401

    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


class ConfigTests(WorkspaceTestCase):
    def write_json(self, data, name="cfg.json"):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            json.dump(data, fh)
        return path

    def write_text(self, text, name):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def assertConfigError(self, fragment, **kwargs):
        with self.assertRaises(ConfigError) as ctx:
            self.resolve(**kwargs)
        self.assertIn(fragment, str(ctx.exception))

    # -- defaults and precedence ---------------------------------------------

    def test_builtin_defaults(self):
        cfg = self.resolve(overrides={"stages": {"qssi": {"jobs": None}}})
        self.assertEqual(cfg.root_dir, os.path.realpath(self.root))
        self.assertEqual(
            cfg.stages["super"].dir,
            os.path.join(cfg.root_dir, SOURCE_ROOT, "apps_vendor", "LINUX", "android"),
        )
        self.assertEqual(cfg.stages["meta"].path, SOURCE_ROOT + "/common/build")
        self.assertEqual(cfg.log_dir, os.path.join(TOOL_ROOT, "logs"))
        self.assertEqual(cfg.stages["tz"].script, os.path.join(TOOL_ROOT, "scripts", "tz.sh"))
        self.assertEqual(cfg.selected, ["tz", "xbl", "qssi", "vendor", "super", "meta"])
        self.assertLessEqual(default_jobs(), MAX_JOBS)
        self.assertEqual(cfg.stages["super"].target_lunch, "msmnile_au")
        self.assertFalse(cfg.stages["super"].output_ota)
        self.assertFalse(cfg.stages["super"].no_tmp)

    def test_default_lunch_targets(self):
        cfg = self.resolve(overrides={"stages": {
            "qssi": {"lunch_target": None}, "vendor": {"lunch_target": None}
        }})
        self.assertEqual(cfg.stages["qssi"].lunch_target, "icbssi-userdebug")
        self.assertEqual(cfg.stages["vendor"].lunch_target, "icb8155-userdebug")

    def test_super_options(self):
        path = self.write_json({"stages": {"super": {
            "target_lunch": "other_au", "output_ota": True, "no_tmp": "yes"
        }}})
        cfg = self.resolve(config_path=path)
        env = cfg.stage_env("super", "RID", "/logs/RID")
        self.assertEqual(env["SUPER_TARGET_LUNCH"], "other_au")
        self.assertEqual(env["SUPER_OUTPUT_OTA"], "true")
        self.assertEqual(env["SUPER_NO_TMP"], "true")
        self.assertNotIn("SUPER_TARGET_LUNCH", cfg.stage_env("meta", "RID", "/logs/RID"))
        self.assertConfigError("stages.super.output_ota", overrides={
            "stages": {"super": {"output_ota": "sometimes"}}
        })

    def test_incremental_env(self):
        full = self.resolve()
        self.assertFalse(full.incremental)
        self.assertEqual(full.stage_env("vendor", "R", "/l")["ICB_INCREMENTAL"], "0")
        inc = self.resolve(incremental=True)
        self.assertTrue(inc.to_dict()["incremental"])
        for stage in ("qssi", "vendor"):
            self.assertEqual(inc.stage_env(stage, "R", "/l")["ICB_INCREMENTAL"], "1")
        for stage in ("tz", "xbl", "super", "meta"):  # their build doesn't change
            self.assertEqual(inc.stage_env(stage, "R", "/l")["ICB_INCREMENTAL"], "0")
        # a retry is always a full clean build
        retry = inc.stage_env("vendor", "R", "/l", attempt=2)
        self.assertEqual((retry["ICB_INCREMENTAL"], retry["ICB_CLEAN"]), ("0", "1"))

    def test_incremental_respects_stage_selection(self):
        cfg = self.resolve(incremental=True, only=["qssi", "vendor"])
        self.assertEqual(cfg.selected, ["qssi", "vendor"])
        self.assertEqual(cfg.warnings, [])
        cfg = self.resolve(incremental=True, skip=["tz", "xbl"])
        self.assertEqual(cfg.selected, ["qssi", "vendor", "super", "meta"])

    def test_incremental_without_qssi_or_vendor_warns(self):
        cfg = self.resolve(incremental=True, only=["tz", "meta"])
        self.assertTrue(any("--incremental only affects qssi and vendor" in w
                            for w in cfg.warnings))
        self.assertFalse(any("--incremental" in w for w in self.resolve(only=["tz"]).warnings))

    def test_incremental_still_validates_gki(self):
        # a retry inside an incremental run is a full build that uses the GKI settings
        self.assertConfigError("gki is true but no GKI version", incremental=True,
                               overrides={"stages": {"vendor": {"gki": True}}})

    def test_stub_flag_is_always_passed(self):
        self.assertEqual(self.resolve(stub=False).stage_env("tz", "R", "/l")["ICB_STUB"], "0")
        self.assertEqual(self.resolve(stub=True).stage_env("tz", "R", "/l")["ICB_STUB"], "1")

    def test_json_file_then_cli_precedence(self):
        path = self.write_json({"stages": {"qssi": {"jobs": 10}, "vendor": {"jobs": 11}}})
        # helper always sets jobs via "CLI" overrides; drop them to see the file value
        cfg = self.resolve(config_path=path, overrides={
            "stages": {"qssi": {"jobs": None}, "vendor": {"jobs": 30}}
        })
        self.assertEqual(cfg.stages["qssi"].jobs, 10)  # from file
        self.assertEqual(cfg.stages["vendor"].jobs, 30)  # CLI beats file
        self.assertEqual(cfg.config_file, path)

    @unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
    def test_yaml_file_then_cli_precedence(self):
        path = self.write_text(
            "ui: plain\nstages:\n  qssi:\n    jobs: 12\n  vendor:\n    gki: yes\n"
            "    gki_version: android14-6.1-2026-06_r6@16037590\n",
            "cfg.yaml",
        )
        cfg = self.resolve(config_path=path, overrides={
            "ui": "ansi", "stages": {"qssi": {"jobs": None}}
        })
        self.assertEqual(cfg.stages["qssi"].jobs, 12)
        self.assertTrue(cfg.stages["vendor"].gki)
        self.assertEqual(cfg.ui, "ansi")

    def test_default_config_file_is_found_in_cwd(self):
        self.write_json({"status_interval": 5}, name="icb_build.json")
        cfg = self.resolve()
        self.assertEqual(cfg.status_interval, 5.0)

    def test_yaml_without_pyyaml_gives_clear_error(self):
        path = self.write_text("ui: plain\n", "cfg.yaml")
        with mock.patch.dict(sys.modules, {"yaml": None}):
            self.assertConfigError("pip install pyyaml", config_path=path)

    def test_unsupported_extension(self):
        path = self.write_text("x", "cfg.toml")
        self.assertConfigError("unsupported config file type", config_path=path)

    # -- jobs limit -------------------------------------------------------------

    def test_jobs_above_limit_from_cli_rejected(self):
        self.assertConfigError(
            "--qssi-jobs / stages.qssi.jobs = 61 is out of range",
            overrides={"stages": {"qssi": {"jobs": 61}}},
        )

    def test_jobs_above_limit_from_file_rejected(self):
        path = self.write_json({"stages": {"vendor": {"jobs": 64}}})
        self.assertConfigError(
            "stages.vendor.jobs = 64 is out of range: must be 1..60",
            config_path=path, overrides={"stages": {"vendor": {"jobs": None}}},
        )

    def test_jobs_limit_is_inclusive(self):
        cfg = self.resolve(overrides={"stages": {"qssi": {"jobs": 60}, "vendor": {"jobs": 1}}})
        self.assertEqual((cfg.stages["qssi"].jobs, cfg.stages["vendor"].jobs), (60, 1))

    def test_jobs_must_be_positive_integer(self):
        for bad in (0, -4, "12", True, 2.5):
            with self.subTest(jobs=bad):
                self.assertConfigError("qssi.jobs", overrides={"stages": {"qssi": {"jobs": bad}}})

    # -- other validation ------------------------------------------------------

    def test_root_dir_in_config_rejected(self):
        path = self.write_json({"root_dir": "/mnt/ws"})
        self.assertConfigError("'root_dir' cannot be set in the config file", config_path=path)

    def test_root_without_source_tree_rejected(self):
        self.root = os.path.join(self.root, SOURCE_ROOT)  # common mistake: one level too deep
        self.assertConfigError("does not contain " + SOURCE_ROOT)

    def test_missing_root_rejected(self):
        self.root = os.path.join(self.tmp, "nope")
        self.assertConfigError("root dir does not exist")

    def test_root_is_resolved_through_symlinks(self):
        link = os.path.join(self.tmp, "link")
        os.symlink(self.root, link)
        self.root = link
        self.assertEqual(self.resolve().root_dir, os.path.realpath(os.path.join(self.tmp, "ws")))

    def test_gki_true_requires_version(self):
        self.assertConfigError(
            "gki is true but no GKI version", overrides={"stages": {"vendor": {"gki": True}}}
        )

    def test_unknown_keys_rejected(self):
        path = self.write_json({"stages": {"qssi": {"job": 4}}, "logdir": "x"})
        with self.assertRaises(ConfigError) as ctx:
            self.resolve(config_path=path)
        self.assertIn("unknown key 'logdir'", str(ctx.exception))
        self.assertIn("unknown key stages.qssi.job", str(ctx.exception))

    def test_lunch_target_required_only_when_needed(self):
        self.assertConfigError(
            "qssi lunch target is not set",
            overrides={"stages": {"qssi": {"lunch_target": ""}}},
        )
        cfg = self.resolve(overrides={"stages": {"qssi": {"lunch_target": ""}}}, only=["tz"])
        self.assertEqual(cfg.selected, ["tz"])
        # super lunches the vendor target, so --only super still needs it
        self.assertConfigError(
            "vendor lunch target is not set",
            overrides={"stages": {"vendor": {"lunch_target": ""}}}, only=["super"],
        )

    def test_missing_stage_dir_is_error_but_warning_in_dry_run(self):
        os.rmdir(os.path.join(self.root, SOURCE_ROOT, "common", "build"))
        self.assertConfigError("stage meta: working directory does not exist")
        cfg = self.resolve(dry_run=True)
        self.assertTrue(any("stage meta" in w for w in cfg.warnings))
        self.resolve(skip=["meta"])  # not selected: not checked

    def test_select_stages(self):
        self.assertEqual(select_stages(["meta", "tz"], None), ["tz", "meta"])
        self.assertEqual(select_stages(None, ["tz", "xbl"]), ["qssi", "vendor", "super", "meta"])
        with self.assertRaises(ConfigError):
            select_stages(["kernel"], None)
        with self.assertRaises(ConfigError):
            select_stages(["tz"], ["xbl"])

    def test_parse_bool(self):
        for value in (True, "true", "YES", "1", "on"):
            self.assertTrue(parse_bool(value))
        for value in (False, "false", "No", "0", "off"):
            self.assertFalse(parse_bool(value))
        with self.assertRaises(ValueError):
            parse_bool("maybe")

    # -- env passed to scripts -------------------------------------------------

    def test_stage_env(self):
        cfg = self.resolve(overrides={"stages": {
            "vendor": {"gki": True, "gki_version": "android14-6.1-2026-06_r6@16037590"},
            "meta": {"env": {"META_FLAVOR": "asic", "DEBUG": True}},
        }})
        qssi = cfg.stage_env("qssi", "RID", "/logs/RID")
        self.assertEqual(qssi["QSSI_JOBS"], "7")
        self.assertEqual(qssi["ICB_STAGE"], "qssi")
        self.assertEqual(qssi["ICB_LOG_DIR"], "/logs/RID")
        self.assertNotIn("VENDOR_JOBS", qssi)

        vendor = cfg.stage_env("vendor", "RID", "/logs/RID")
        self.assertEqual(vendor["GKI_ENABLED"], "true")
        self.assertEqual(vendor["GKI_VERSION"], "android14-6.1-2026-06_r6@16037590")
        self.assertNotIn("QSSI_JOBS", vendor)

        meta = cfg.stage_env("meta", "RID", "/logs/RID")
        self.assertEqual(meta["QSSI_DIR"], cfg.stages["qssi"].dir)
        self.assertEqual(meta["VENDOR_DIR"], cfg.stages["vendor"].dir)
        self.assertEqual(meta["META_FLAVOR"], "asic")
        self.assertEqual(meta["DEBUG"], "true")

        tz = cfg.stage_env("tz", "RID", "/logs/RID")
        self.assertEqual(
            sorted(tz),
            ["ICB_ATTEMPT", "ICB_CLEAN", "ICB_INCREMENTAL", "ICB_LOG_DIR", "ICB_ROOT_DIR",
             "ICB_RUN_ID", "ICB_STAGE", "ICB_STAGE_DIR", "ICB_STUB"],
        )
        self.assertEqual((tz["ICB_ATTEMPT"], tz["ICB_CLEAN"]), ("1", "0"))
        retry = cfg.stage_env("tz", "RID", "/logs/RID", attempt=3)
        self.assertEqual((retry["ICB_ATTEMPT"], retry["ICB_CLEAN"]), ("3", "1"))


if __name__ == "__main__":
    unittest.main()
