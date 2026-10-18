"""Configuration: built-in defaults < config file (YAML/JSON) < CLI overrides."""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .stages import DEPENDENCIES, STAGE_ORDER

TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_ROOT = "snapdragon-auto-gen3-la"

# Hard cap on -j for qssi and vendor: the build server is shared.
MAX_JOBS = 60

UI_MODES = ("auto", "rich", "ansi", "plain", "tui")
DEFAULT_CONFIG_NAMES = ("icb_build.yaml", "icb_build.yml", "icb_build.json")

TOP_LEVEL_KEYS = {"log_dir", "status_interval", "ui", "stages"}
COMMON_STAGE_KEYS = {"path", "script", "env"}
EXTRA_STAGE_KEYS = {
    "qssi": {"lunch_target", "jobs"},
    "vendor": {"lunch_target", "jobs", "gki", "gki_version"},
    "super": {"target_lunch", "output_ota", "no_tmp"},
}

# A stage's lunch target must be set when any of these stages will run
# (super lunches the vendor target before merging).
LUNCH_NEEDED_BY = {"qssi": ["qssi"], "vendor": ["vendor", "super"]}

# Which CLI flag sets a config key; used to make error messages actionable.
CLI_FLAGS = {
    ("qssi", "jobs"): "--qssi-jobs",
    ("qssi", "lunch_target"): "--qssi-lunch",
    ("vendor", "jobs"): "--vendor-jobs",
    ("vendor", "lunch_target"): "--vendor-lunch",
    ("vendor", "gki"): "--gki",
    ("vendor", "gki_version"): "--gki-version",
}

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TRUE = {"true", "1", "yes", "y", "on"}
_FALSE = {"false", "0", "no", "n", "off"}


class ConfigError(Exception):
    """Invalid configuration or arguments. The CLI exits with code 2."""


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
    raise ValueError("expected true/false, got %r" % (value,))


def default_jobs() -> int:
    return max(1, min(MAX_JOBS, os.cpu_count() or 1))


def builtin_defaults() -> Dict[str, Any]:
    jobs = default_jobs()
    return {
        "log_dir": "logs",
        "status_interval": 30,
        "ui": "auto",
        "stages": {
            "tz": {"path": SOURCE_ROOT + "/tz_8155/trustzone_images/build/ms"},
            "xbl": {"path": SOURCE_ROOT + "/boot_8155/boot_images/QcomPkg"},
            "qssi": {
                "path": SOURCE_ROOT + "/apps_qssi/LINUX/android",
                "lunch_target": "icbssi-userdebug",
                "jobs": jobs,
            },
            "vendor": {
                "path": SOURCE_ROOT + "/apps_vendor/LINUX/android",
                "lunch_target": "icb8155-userdebug",
                "jobs": jobs,
                "gki": False,
                "gki_version": "",
            },
            "super": {
                "path": SOURCE_ROOT + "/apps_vendor/LINUX/android",
                "target_lunch": "msmnile_au",
                "output_ota": False,
                "no_tmp": False,
            },
            "meta": {"path": SOURCE_ROOT + "/common/build"},
        },
    }


def find_default_config(cwd: str) -> Optional[str]:
    for name in DEFAULT_CONFIG_NAMES:
        candidate = os.path.join(cwd, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def load_config_file(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        raise ConfigError("config file not found: %s" % path)
    ext = os.path.splitext(path)[1].lower()
    with open(path, encoding="utf-8") as fh:
        text = fh.read()

    if ext in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            raise ConfigError(
                "%s is YAML, which needs PyYAML: run 'pip install pyyaml' "
                "or use a .json config instead" % path
            )
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ConfigError("invalid YAML in %s: %s" % (path, exc))
    elif ext == ".json":
        try:
            data = json.loads(text) if text.strip() else None
        except ValueError as exc:
            raise ConfigError("invalid JSON in %s: %s" % (path, exc))
    else:
        raise ConfigError(
            "unsupported config file type %r (use .yaml, .yml or .json): %s" % (ext, path)
        )

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError("%s: top level must be a mapping" % path)
    return data


def check_file_keys(data: Dict[str, Any], source: str) -> None:
    """Reject unknown keys so typos don't silently fall back to defaults."""
    errors = []  # type: List[str]
    if "root_dir" in data:
        errors.append(
            "%s: 'root_dir' cannot be set in the config file; pass it with -r/--root-dir" % source
        )
    for key in sorted(set(data) - TOP_LEVEL_KEYS - {"root_dir"}):
        errors.append("%s: unknown key %r" % (source, key))

    stages = data.get("stages", {})
    if stages is None:
        stages = {}
    if not isinstance(stages, dict):
        errors.append("%s: 'stages' must be a mapping" % source)
        stages = {}
    for name, body in stages.items():
        if name not in DEPENDENCIES:
            errors.append(
                "%s: unknown stage %r (valid: %s)" % (source, name, ", ".join(STAGE_ORDER))
            )
            continue
        if body is None:
            continue
        if not isinstance(body, dict):
            errors.append("%s: stages.%s must be a mapping" % (source, name))
            continue
        allowed = COMMON_STAGE_KEYS | EXTRA_STAGE_KEYS.get(name, set())
        for key in sorted(set(body) - allowed):
            errors.append(
                "%s: unknown key stages.%s.%s (allowed: %s)"
                % (source, name, key, ", ".join(sorted(allowed)))
            )
    if errors:
        raise ConfigError("\n".join(errors))


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        elif value is not None:
            result[key] = copy.deepcopy(value)
    return result


def select_stages(only: Optional[List[str]], skip: Optional[List[str]]) -> List[str]:
    if only and skip:
        raise ConfigError("--only and --skip cannot be used together")
    for flag, names in (("--only", only), ("--skip", skip)):
        unknown = [n for n in (names or []) if n not in DEPENDENCIES]
        if unknown:
            raise ConfigError(
                "%s: unknown stage(s) %s (valid: %s)"
                % (flag, ", ".join(unknown), ", ".join(STAGE_ORDER))
            )
    if only:
        return [n for n in STAGE_ORDER if n in only]
    if skip:
        return [n for n in STAGE_ORDER if n not in skip]
    return list(STAGE_ORDER)


@dataclass
class StageConfig:
    name: str
    path: str  # as configured, relative to root_dir
    dir: str  # absolute working directory
    script: str  # absolute path to the .sh file
    env: Dict[str, str] = field(default_factory=dict)
    lunch_target: str = ""
    jobs: Optional[int] = None
    gki: Optional[bool] = None
    gki_version: str = ""
    target_lunch: str = ""  # super: --target_lunch of build_image_standalone.py
    output_ota: bool = False
    no_tmp: bool = False

    def command(self) -> List[str]:
        return ["bash", self.script]


@dataclass
class Config:
    root_dir: str
    config_file: Optional[str]
    log_dir: str
    status_interval: float
    ui: str
    stages: Dict[str, StageConfig]
    selected: List[str]
    warnings: List[str] = field(default_factory=list)
    stub: bool = False  # --stub: scripts log commands instead of running them

    def stage_env(
        self, name: str, run_id: str, run_dir: str, attempt: int = 1
    ) -> Dict[str, str]:
        """Variables passed to a stage's script (and printed in its log header)."""
        stage = self.stages[name]
        qssi, vendor = self.stages["qssi"], self.stages["vendor"]
        env = {
            "ICB_ROOT_DIR": self.root_dir,
            "ICB_STAGE": name,
            "ICB_STAGE_DIR": stage.dir,
            "ICB_LOG_DIR": run_dir,
            "ICB_RUN_ID": run_id,
            # Always passed, so an ICB_STUB exported in the user's shell can't leak in.
            "ICB_STUB": "1" if self.stub else "0",
            # A retry (attempt > 1) asks the script to clean the stage's output first.
            "ICB_ATTEMPT": str(attempt),
            "ICB_CLEAN": "1" if attempt > 1 else "0",
        }
        qssi_vars = {
            "QSSI_LUNCH_TARGET": qssi.lunch_target,
            "QSSI_JOBS": str(qssi.jobs),
        }
        vendor_vars = {
            "VENDOR_LUNCH_TARGET": vendor.lunch_target,
            "VENDOR_JOBS": str(vendor.jobs),
            "GKI_ENABLED": "true" if vendor.gki else "false",
            "GKI_VERSION": vendor.gki_version,
        }
        if name == "qssi":
            env.update(qssi_vars)
        elif name == "vendor":
            env.update(vendor_vars)
        elif name in ("super", "meta"):
            env.update(qssi_vars)
            env.update(vendor_vars)
            env["QSSI_DIR"] = qssi.dir
            env["VENDOR_DIR"] = vendor.dir
        if name == "super":
            env["SUPER_TARGET_LUNCH"] = stage.target_lunch
            env["SUPER_OUTPUT_OTA"] = "true" if stage.output_ota else "false"
            env["SUPER_NO_TMP"] = "true" if stage.no_tmp else "false"
        env.update(stage.env)
        return env

    def to_dict(self) -> Dict[str, Any]:
        stages = {}
        for name in STAGE_ORDER:
            st = self.stages[name]
            item = {
                "path": st.path,
                "dir": st.dir,
                "script": st.script,
                "env": dict(st.env),
            }  # type: Dict[str, Any]
            if name in ("qssi", "vendor"):
                item["lunch_target"] = st.lunch_target
                item["jobs"] = st.jobs
            if name == "vendor":
                item["gki"] = st.gki
                item["gki_version"] = st.gki_version
            if name == "super":
                item["target_lunch"] = st.target_lunch
                item["output_ota"] = st.output_ota
                item["no_tmp"] = st.no_tmp
            stages[name] = item
        return {
            "root_dir": self.root_dir,
            "config_file": self.config_file,
            "log_dir": self.log_dir,
            "status_interval": self.status_interval,
            "ui": self.ui,
            "selected": list(self.selected),
            "stub": self.stub,
            "stages": stages,
        }


def _where(stage: str, key: str) -> str:
    flag = CLI_FLAGS.get((stage, key))
    loc = "stages.%s.%s" % (stage, key)
    return "%s / %s" % (flag, loc) if flag else loc


def resolve(
    root_dir: str,
    config_path: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    only: Optional[List[str]] = None,
    skip: Optional[List[str]] = None,
    dry_run: bool = False,
    stub: bool = False,
    cwd: Optional[str] = None,
) -> Config:
    """Merge all config layers, validate them and return a typed Config.

    All validation problems are collected and raised together as one ConfigError.
    """
    cwd = cwd or os.getcwd()
    if config_path is None:
        config_path = find_default_config(cwd)
    file_data = {}  # type: Dict[str, Any]
    if config_path:
        config_path = os.path.abspath(os.path.expanduser(config_path))
        file_data = load_config_file(config_path)
        check_file_keys(file_data, config_path)

    merged = deep_merge(builtin_defaults(), file_data)
    merged = deep_merge(merged, overrides or {})
    selected = select_stages(only, skip)

    errors = []  # type: List[str]
    warnings = []  # type: List[str]

    # root dir: CLI only, resolved to an absolute real path.
    root = os.path.realpath(os.path.expanduser(root_dir))
    if not os.path.isdir(root):
        errors.append("root dir does not exist or is not a directory: %s" % root)
    elif not os.path.isdir(os.path.join(root, SOURCE_ROOT)):
        errors.append(
            "root dir %s does not contain %s/ -- pass the directory that contains it "
            "(not %s itself)" % (root, SOURCE_ROOT, SOURCE_ROOT)
        )

    ui = merged.get("ui")
    if ui not in UI_MODES:
        errors.append("ui must be one of %s, got %r" % (", ".join(UI_MODES), ui))

    interval = merged.get("status_interval")
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval <= 0:
        errors.append("status_interval must be a positive number, got %r" % (interval,))
        interval = 30

    log_dir = merged.get("log_dir")
    if not isinstance(log_dir, str) or not log_dir.strip():
        errors.append("log_dir must be a non-empty string, got %r" % (log_dir,))
        log_dir = "logs"
    log_dir = os.path.expanduser(log_dir)
    if not os.path.isabs(log_dir):
        log_dir = os.path.join(TOOL_ROOT, log_dir)
    log_dir = os.path.normpath(log_dir)

    stages = {}  # type: Dict[str, StageConfig]
    for name in STAGE_ORDER:
        raw = merged["stages"].get(name) or {}
        path = raw.get("path")
        if not isinstance(path, str) or not path.strip():
            errors.append("stages.%s.path must be a non-empty string" % name)
            path = "."
        stage_dir = os.path.normpath(os.path.join(root, path))

        script = raw.get("script") or os.path.join("scripts", name + ".sh")
        if not isinstance(script, str):
            errors.append("stages.%s.script must be a string" % name)
            script = os.path.join("scripts", name + ".sh")
        script = os.path.expanduser(script)
        if not os.path.isabs(script):
            script = os.path.join(TOOL_ROOT, script)
        script = os.path.normpath(script)

        env = {}  # type: Dict[str, str]
        raw_env = raw.get("env") or {}
        if not isinstance(raw_env, dict):
            errors.append("stages.%s.env must be a mapping" % name)
            raw_env = {}
        for key, value in raw_env.items():
            if not isinstance(key, str) or not _ENV_NAME_RE.match(key):
                errors.append("stages.%s.env: invalid variable name %r" % (name, key))
                continue
            if isinstance(value, bool):
                env[key] = "true" if value else "false"
            elif isinstance(value, (str, int, float)):
                env[key] = str(value)
            elif value is None:
                env[key] = ""
            else:
                errors.append("stages.%s.env.%s must be a scalar value" % (name, key))

        st = StageConfig(name=name, path=path, dir=stage_dir, script=script, env=env)

        if name in ("qssi", "vendor"):
            lunch = raw.get("lunch_target", "")
            if lunch is None:
                lunch = ""
            if not isinstance(lunch, str):
                errors.append("%s must be a string" % _where(name, "lunch_target"))
                lunch = ""
            st.lunch_target = lunch.strip()
            if set(LUNCH_NEEDED_BY[name]) & set(selected) and not st.lunch_target:
                errors.append(
                    "%s lunch target is not set (%s), needed by: %s"
                    % (name, _where(name, "lunch_target"),
                       ", ".join(n for n in LUNCH_NEEDED_BY[name] if n in selected))
                )

            jobs = raw.get("jobs")
            if isinstance(jobs, bool) or not isinstance(jobs, int):
                errors.append(
                    "%s must be an integer, got %r" % (_where(name, "jobs"), jobs)
                )
            elif not 1 <= jobs <= MAX_JOBS:
                errors.append(
                    "%s = %d is out of range: must be 1..%d (shared build server limit)"
                    % (_where(name, "jobs"), jobs, MAX_JOBS)
                )
            else:
                st.jobs = jobs

        if name == "vendor":
            try:
                st.gki = parse_bool(raw.get("gki", False))
            except ValueError as exc:
                errors.append("%s: %s" % (_where(name, "gki"), exc))
                st.gki = False
            version = raw.get("gki_version", "")
            if version is None:
                version = ""
            if not isinstance(version, str):
                errors.append("%s must be a string" % _where(name, "gki_version"))
                version = ""
            st.gki_version = version.strip()
            if st.gki and not st.gki_version:
                errors.append(
                    "gki is true but no GKI version is given (%s)" % _where(name, "gki_version")
                )

        if name == "super":
            target = raw.get("target_lunch", "")
            if not isinstance(target, str) or not target.strip():
                errors.append("stages.super.target_lunch must be a non-empty string")
                target = ""
            st.target_lunch = target.strip()
            for key in ("output_ota", "no_tmp"):
                try:
                    setattr(st, key, parse_bool(raw.get(key, False)))
                except ValueError as exc:
                    errors.append("stages.super.%s: %s" % (key, exc))

        if name in selected:
            if not os.path.isfile(script):
                errors.append("stage %s: script not found: %s" % (name, script))
            if os.path.isdir(root) and not os.path.isdir(stage_dir):
                msg = "stage %s: working directory does not exist: %s" % (name, stage_dir)
                (warnings if dry_run else errors).append(msg)

        stages[name] = st

    if errors:
        raise ConfigError("\n".join(errors))

    return Config(
        root_dir=root,
        config_file=config_path,
        log_dir=log_dir,
        status_interval=float(interval),
        ui=ui,
        stages=stages,
        selected=selected,
        warnings=warnings,
        stub=stub,
    )
