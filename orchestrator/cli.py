"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional, TextIO

from . import __version__
from .config import MAX_JOBS, Config, ConfigError, parse_bool, resolve
from .display import resolve_mode, select_display
from .runner import describe_exit
from .scheduler import RetryPrompt, Scheduler
from .stages import DEPENDENCIES, STAGE_ORDER, StageStatus

EXIT_USAGE = 2


def _bool_arg(value: str) -> bool:
    try:
        return parse_bool(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected true or false, got %r" % value)


def _stage_list(value: str) -> List[str]:
    names = [v.strip() for v in value.split(",") if v.strip()]
    if not names:
        raise argparse.ArgumentTypeError("expected a comma-separated list of stages")
    return names


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="icb_build.py",
        description="Build the ICB QCOM AOSP image: TZ, XBL, QSSI and Vendor in parallel, "
        "then Super, then Meta.",
        epilog="Settings precedence: built-in defaults < config file < command line.",
    )
    p.add_argument("-r", "--root-dir", required=True,
                   help="workspace root (the directory containing snapdragon-auto-gen3-la/)")
    p.add_argument("-c", "--config",
                   help="YAML or JSON config file (default: ./icb_build.yaml|.yml|.json if present)")
    p.add_argument("--qssi-lunch", metavar="TARGET", help="QSSI lunch target")
    p.add_argument("--qssi-jobs", metavar="N", type=int, help="QSSI -j (1..%d)" % MAX_JOBS)
    p.add_argument("--vendor-lunch", metavar="TARGET", help="Vendor lunch target")
    p.add_argument("--vendor-jobs", metavar="N", type=int, help="Vendor -j (1..%d)" % MAX_JOBS)
    p.add_argument("--gki", metavar="true|false", type=_bool_arg,
                   help="whether the GKI is provided by the user")
    p.add_argument("--gki-version", metavar="STR",
                   help="GKI version, e.g. android14-6.1-2026-06_r6@16037590")
    sel = p.add_mutually_exclusive_group()
    sel.add_argument("--only", metavar="STAGES", type=_stage_list,
                     help="run only these stages (comma-separated: %s)" % ",".join(STAGE_ORDER))
    sel.add_argument("--skip", metavar="STAGES", type=_stage_list,
                     help="run all stages except these (comma-separated)")
    p.add_argument("--dry-run", action="store_true",
                   help="print the resolved config and per-stage commands, run nothing")
    p.add_argument("--stub", action="store_true",
                   help="run the pipeline with every build command logged but not run "
                        "(ICB_STUB=1), to check orchestration without building")
    p.add_argument("--log-dir", metavar="PATH", help="directory for run logs")
    p.add_argument("--ui", choices=("auto", "rich", "ansi", "plain", "tui"),
                   help="status display mode (tui: interactive, see README)")
    p.add_argument("--version", action="version", version="%(prog)s " + __version__)
    return p


def overrides_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    """Only flags the user actually passed become overrides."""
    stage_flags = {
        ("qssi", "lunch_target"): args.qssi_lunch,
        ("qssi", "jobs"): args.qssi_jobs,
        ("vendor", "lunch_target"): args.vendor_lunch,
        ("vendor", "jobs"): args.vendor_jobs,
        ("vendor", "gki"): args.gki,
        ("vendor", "gki_version"): args.gki_version,
    }
    stages = {}  # type: Dict[str, Dict[str, Any]]
    for (stage, key), value in stage_flags.items():
        if value is not None:
            stages.setdefault(stage, {})[key] = value
    overrides = {}  # type: Dict[str, Any]
    if stages:
        overrides["stages"] = stages
    if args.log_dir is not None:
        # A relative --log-dir is relative to where the command is run.
        overrides["log_dir"] = os.path.abspath(os.path.expanduser(args.log_dir))
    if args.ui is not None:
        overrides["ui"] = args.ui
    return overrides


def new_run_id(log_dir: str) -> str:
    base = time.strftime("%Y%m%d-%H%M%S")
    run_id, n = base, 1
    while os.path.exists(os.path.join(log_dir, run_id)):
        run_id = "%s-%d" % (base, n)
        n += 1
    return run_id


def prepare_run_dir(log_dir: str, run_id: str) -> str:
    run_dir = os.path.join(log_dir, run_id)
    os.makedirs(run_dir)
    latest = os.path.join(log_dir, "latest")
    tmp = os.path.join(log_dir, ".latest.%d" % os.getpid())
    try:
        os.symlink(run_id, tmp)  # relative target: survives moving the log dir
        os.replace(tmp, latest)
    except OSError as exc:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        sys.stderr.write("warning: could not update %s: %s\n" % (latest, exc))
    return run_dir


def setup_run_logger(run_dir: str) -> logging.Handler:
    handler = logging.FileHandler(os.path.join(run_dir, "orchestrator.log"), encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    logger = logging.getLogger("icb_build")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return handler


def print_dry_run(cfg: Config, out=None) -> None:
    out = out or sys.stdout
    w = out.write
    w("Config file: %s\n" % (cfg.config_file or "(none: defaults + command line)"))
    w("Display:     %s (resolved: %s)\n" % (cfg.ui, resolve_mode(cfg.ui, sys.stdout)))
    if cfg.stub:
        w("Stub mode:   on (scripts log their commands instead of running them)\n")
    w("\nResolved configuration:\n")
    w(json.dumps(cfg.to_dict(), indent=2) + "\n")
    w("\nStage plan:\n")
    for name in STAGE_ORDER:
        stage = cfg.stages[name]
        deps = ", ".join(DEPENDENCIES[name]) or "-"
        if name not in cfg.selected:
            w("  %-7s SKIP (not selected)\n" % name)
            continue
        w("  %-7s RUN   after: %s\n" % (name, deps))
        w("          cwd:     %s\n" % stage.dir)
        w("          command: %s\n" % " ".join(stage.command()))
        env = cfg.stage_env(name, "<run-id>", os.path.join(cfg.log_dir, "<run-id>"))
        w("          env:\n")
        for key in sorted(env):
            w("            %s=%s\n" % (key, env[key]))


def make_retry_prompt(stdin: TextIO, stdout: TextIO) -> RetryPrompt:
    """Ask [y/N] for each failed stage. Ctrl+C skips the remaining questions."""

    def prompt(failed: List[StageStatus]) -> List[str]:
        chosen = []  # type: List[str]
        for status in failed:
            stdout.write("\n%s FAILED (attempt %d, exit %s), log: %s\n" % (
                status.name, status.attempt, describe_exit(status.exit_code),
                status.log_path or "-"))
            stdout.write("Clean and retry %s? [y/N] " % status.name)
            stdout.flush()
            try:
                answer = stdin.readline()
            except KeyboardInterrupt:
                stdout.write("\n")
                break
            if not answer:  # EOF
                stdout.write("\n")
                break
            if answer.strip().lower() in ("y", "yes"):
                chosen.append(status.name)
        stdout.flush()
        return chosen

    return prompt


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if not sys.platform.startswith("linux"):
        sys.stderr.write("error: icb_build only supports Linux (detected %s)\n" % sys.platform)
        return EXIT_USAGE

    try:
        cfg = resolve(
            root_dir=args.root_dir,
            config_path=args.config,
            overrides=overrides_from_args(args),
            only=args.only,
            skip=args.skip,
            dry_run=args.dry_run,
            stub=args.stub,
        )
    except ConfigError as exc:
        for line in str(exc).splitlines():
            sys.stderr.write("error: %s\n" % line)
        return EXIT_USAGE

    for warning in cfg.warnings:
        sys.stderr.write("warning: %s\n" % warning)

    if args.dry_run:
        print_dry_run(cfg)
        return 0

    try:
        os.makedirs(cfg.log_dir, exist_ok=True)
        run_id = new_run_id(cfg.log_dir)
        display = select_display(cfg.ui, run_id, os.path.join(cfg.log_dir, run_id),
                                 cfg.status_interval)
        run_dir = prepare_run_dir(cfg.log_dir, run_id)
    except ConfigError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return EXIT_USAGE
    except OSError as exc:
        sys.stderr.write("error: cannot create log directory: %s\n" % exc)
        return EXIT_USAGE

    with open(os.path.join(run_dir, "resolved_config.json"), "w", encoding="utf-8") as fh:
        data = cfg.to_dict()
        data["run_id"] = run_id
        data["tool_version"] = __version__
        data["python"] = sys.version.split()[0]
        json.dump(data, fh, indent=2)
        fh.write("\n")

    handler = setup_run_logger(run_dir)
    try:
        logging.getLogger("icb_build").info(
            "icb_build %s, python %s, config file: %s",
            __version__, sys.version.split()[0], cfg.config_file or "(none)",
        )
        logging.getLogger("icb_build").info(
            "resolved config: %s", json.dumps(cfg.to_dict(), sort_keys=True)
        )
        retry_prompt = None
        if sys.stdin.isatty() and sys.stdout.isatty():
            retry_prompt = make_retry_prompt(sys.stdin, sys.stdout)
        return Scheduler(cfg, run_id, run_dir, display, retry_prompt=retry_prompt).run()
    finally:
        logging.getLogger("icb_build").removeHandler(handler)
        handler.close()
