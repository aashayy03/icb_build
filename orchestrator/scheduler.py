"""Dependency-driven scheduler: start ready stages, propagate failures, handle Ctrl+C."""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections import OrderedDict
from datetime import datetime
from typing import Dict, List, Optional

from .config import Config
from .display.base import Display
from .runner import StageProcess, describe_exit, fmt_duration
from .stages import DEPENDENCIES, STAGE_ORDER, StageState, StageStatus

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INTERRUPTED = 130

log = logging.getLogger("icb_build")


def _now_wall() -> datetime:
    return datetime.now().astimezone()


class Scheduler:
    def __init__(
        self,
        cfg: Config,
        run_id: str,
        run_dir: str,
        display: Display,
        poll_interval: float = 0.5,
        term_grace: float = 10.0,
    ) -> None:
        self.cfg = cfg
        self.run_id = run_id
        self.run_dir = run_dir
        self.display = display
        self.poll_interval = poll_interval
        self.term_grace = term_grace
        self.statuses = OrderedDict(
            (name, StageStatus(name)) for name in STAGE_ORDER
        )  # type: OrderedDict[str, StageStatus]
        for name, status in self.statuses.items():
            if name not in cfg.selected:
                status.state = StageState.SKIPPED
                status.note = "not selected"
        self._procs = {}  # type: Dict[str, StageProcess]
        self._interrupts = 0
        self._interrupt_lock = threading.Lock()
        self._cancel_started_at = None  # type: Optional[float]
        self._force_killed = False

    # -- public -----------------------------------------------------------

    def request_cancel(self) -> None:
        """First call: graceful stop (SIGTERM, then SIGKILL after grace). Second: SIGKILL now."""
        with self._interrupt_lock:
            self._interrupts += 1

    @property
    def interrupted(self) -> bool:
        return self._cancel_started_at is not None

    def snapshot(self) -> List[StageStatus]:
        return list(self.statuses.values())

    def run(self) -> int:
        log.info("run %s started; stages selected: %s", self.run_id, ", ".join(self.cfg.selected))
        for name, status in self.statuses.items():
            if status.state is StageState.SKIPPED:
                log.info("stage %s: SKIPPED (not selected)", name)

        restore = self._install_signal_handlers()
        try:
            self.display.start(self.snapshot())
            if self.cfg.stub:
                msg = "STUB MODE: build commands are logged, not run (ICB_STUB=1)"
                self.display.message(msg)
                log.warning(msg)
            while True:
                self._handle_interrupts()
                self._reap()
                if not self.interrupted:
                    self._propagate_blocked()
                    self._start_ready()
                self.display.update(self.snapshot())
                if all(s.state.is_terminal for s in self.statuses.values()):
                    break
                time.sleep(self.poll_interval)
        finally:
            restore()
            self._kill_leftovers()
            self.display.stop(self.snapshot(), self.interrupted)

        code = self.exit_code()
        log.info("run %s finished with exit code %d", self.run_id, code)
        return code

    def exit_code(self) -> int:
        if self.interrupted:
            return EXIT_INTERRUPTED
        bad = (StageState.FAILED, StageState.BLOCKED, StageState.CANCELLED)
        if any(s.state in bad for s in self.statuses.values()):
            return EXIT_FAILED
        return EXIT_OK

    # -- internals --------------------------------------------------------

    def _install_signal_handlers(self):
        if threading.current_thread() is not threading.main_thread():
            return lambda: None

        def handler(signum, frame):
            self.request_cancel()

        previous = {
            sig: signal.signal(sig, handler) for sig in (signal.SIGINT, signal.SIGTERM)
        }

        def restore():
            for sig, old in previous.items():
                signal.signal(sig, old)

        return restore

    def _running(self) -> List[str]:
        return [n for n, s in self.statuses.items() if s.state is StageState.RUNNING]

    def _handle_interrupts(self) -> None:
        with self._interrupt_lock:
            count = self._interrupts
        now = time.monotonic()

        if count >= 1 and self._cancel_started_at is None:
            self._cancel_started_at = now
            running = self._running()
            msg = "Interrupt received: stopping %s (Ctrl+C again to force kill)" % (
                ", ".join(running) if running else "nothing running"
            )
            self.display.message(msg)
            log.warning(msg)
            for name, status in self.statuses.items():
                if status.state is StageState.PENDING:
                    status.state = StageState.BLOCKED
                    status.note = "not started: run interrupted"
                    log.info("stage %s: BLOCKED (run interrupted)", name)
            for name in running:
                self._procs[name].send_signal(signal.SIGTERM)
                log.info("stage %s: sent SIGTERM to process group", name)

        if (
            self._cancel_started_at is not None
            and not self._force_killed
            and (count >= 2 or now - self._cancel_started_at >= self.term_grace)
        ):
            self._force_killed = True
            running = self._running()
            if running:
                reason = "second interrupt" if count >= 2 else "grace period expired"
                msg = "Force killing %s (%s)" % (", ".join(running), reason)
                self.display.message(msg)
                log.warning(msg)
                for name in running:
                    self._procs[name].send_signal(signal.SIGKILL)

    def _reap(self) -> None:
        for name in self._running():
            status = self.statuses[name]
            proc = self._procs[name]
            status.last_line = proc.last_line
            code = proc.poll()
            if code is None:
                continue
            status.ended_at = time.monotonic()
            status.end_wall = _now_wall()
            status.exit_code = code
            note = ""
            if code == 0:
                status.state = StageState.COMPLETED
            elif proc.signal_sent:
                status.state = StageState.CANCELLED
                note = "stopped by user (%s)" % proc.signal_sent
            else:
                status.state = StageState.FAILED
                note = "script exited with %s" % describe_exit(code)
            status.note = note
            proc.finish(status.end_wall, status.elapsed(), code, status.state.value, note)
            level = logging.INFO if status.state is StageState.COMPLETED else logging.WARNING
            log.log(
                level,
                "stage %s: %s exit=%s duration=%s",
                name, status.state.value, describe_exit(code), fmt_duration(status.elapsed()),
            )

    def _propagate_blocked(self) -> None:
        # STAGE_ORDER is topological, so one pass also blocks transitive dependents.
        for name in STAGE_ORDER:
            status = self.statuses[name]
            if status.state is not StageState.PENDING:
                continue
            culprits = [d for d in DEPENDENCIES[name] if self.statuses[d].state.blocks_dependents]
            if culprits:
                status.state = StageState.BLOCKED
                status.note = "blocked by %s" % ", ".join(culprits)
                log.info("stage %s: BLOCKED (%s)", name, status.note)

    def _start_ready(self) -> None:
        for name in STAGE_ORDER:
            status = self.statuses[name]
            if status.state is not StageState.PENDING:
                continue
            if not all(self.statuses[d].state.satisfies_dependents for d in DEPENDENCIES[name]):
                continue
            self._start(name)

    def _start(self, name: str) -> None:
        stage = self.cfg.stages[name]
        status = self.statuses[name]
        log_path = os.path.join(self.run_dir, name + ".log")
        proc = StageProcess(
            name=name,
            command=stage.command(),
            cwd=stage.dir,
            passed_env=self.cfg.stage_env(name, self.run_id, self.run_dir),
            log_path=log_path,
            run_id=self.run_id,
        )
        status.log_path = log_path
        status.started_at = time.monotonic()
        status.start_wall = _now_wall()
        self._procs[name] = proc
        if proc.start(status.start_wall):
            status.state = StageState.RUNNING
            log.info("stage %s: RUNNING pid=%s cwd=%s", name, proc.pid, stage.dir)
            return
        status.ended_at = time.monotonic()
        status.end_wall = _now_wall()
        status.state = StageState.FAILED
        status.note = proc.start_error or "failed to start"
        proc.finish(status.end_wall, status.elapsed(), None, status.state.value, status.note)
        log.error("stage %s: FAILED (%s)", name, status.note)

    def _kill_leftovers(self) -> None:
        """Only reached with stages still running if the loop exited abnormally."""
        for name in self._running():
            proc = self._procs[name]
            proc.send_signal(signal.SIGKILL)
            status = self.statuses[name]
            status.state = StageState.CANCELLED
            status.ended_at = time.monotonic()
            status.end_wall = _now_wall()
            status.note = "killed: orchestrator exited unexpectedly"
            proc.finish(status.end_wall, status.elapsed(), None, status.state.value, status.note)
            log.error("stage %s: killed because the orchestrator loop exited", name)
