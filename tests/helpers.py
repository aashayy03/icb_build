"""Shared test helpers: fake workspaces and a quiet display."""

import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from orchestrator.config import builtin_defaults, resolve
from orchestrator.display.base import Display

LUNCH = {
    "stages": {
        "qssi": {"lunch_target": "qssi-userdebug", "jobs": 7},
        "vendor": {"lunch_target": "board-userdebug", "jobs": 9},
    }
}


def make_workspace(parent):
    """Create <parent>/ws with every default stage directory. Returns the root dir."""
    root = os.path.join(parent, "ws")
    for stage in builtin_defaults()["stages"].values():
        os.makedirs(os.path.join(root, stage["path"]), exist_ok=True)
    return root


class QuietDisplay(Display):
    def __init__(self):
        super().__init__("test-run", "/nonexistent", 30.0, io.StringIO())
        self.messages = []

    def message(self, text):
        self.messages.append(text)


class WorkspaceTestCase(unittest.TestCase):
    """Gives each test a fake workspace, a run dir and fast stub scripts."""

    stub_sleep = "0.05"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="icb_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = make_workspace(self.tmp)
        self.run_dir = os.path.join(self.tmp, "logs", "run")
        os.makedirs(self.run_dir)
        patcher = mock.patch.dict(os.environ, {"ICB_STUB_SLEEP": self.stub_sleep})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("ICB_STUB_FAIL", None)

    def resolve(self, overrides=None, **kwargs):
        merged = {"stages": {k: dict(v) for k, v in LUNCH["stages"].items()}}
        for name, body in ((overrides or {}).get("stages") or {}).items():
            merged["stages"].setdefault(name, {}).update(body)
        for key, value in (overrides or {}).items():
            if key != "stages":
                merged[key] = value
        kwargs.setdefault("cwd", self.tmp)  # never pick up a stray ./icb_build.yaml
        kwargs.setdefault("stub", True)  # scripts log commands instead of building
        return resolve(self.root, overrides=merged, **kwargs)

    def write_script(self, name, body):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            fh.write("#!/usr/bin/env bash\n" + body)
        return path
