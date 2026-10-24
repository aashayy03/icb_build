"""Display interface and helpers shared by all status displays."""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import List, Optional, TextIO

from ..runner import fmt_duration
from ..stages import StageState, StageStatus

# ANSI SGR colour codes per state (used by the ansi display).
STATE_SGR = {
    StageState.PENDING: "2",  # dim
    StageState.RUNNING: "33",  # yellow
    StageState.COMPLETED: "32",  # green
    StageState.FAILED: "31;1",  # bold red
    StageState.SKIPPED: "2",
    StageState.BLOCKED: "2",
    StageState.CANCELLED: "35",  # magenta
}

# rich style names per state (used by the rich display).
STATE_STYLE = {
    StageState.PENDING: "dim",
    StageState.RUNNING: "yellow",
    StageState.COMPLETED: "green",
    StageState.FAILED: "bold red",
    StageState.SKIPPED: "dim",
    StageState.BLOCKED: "dim",
    StageState.CANCELLED: "magenta",
}


def stage_detail(status: StageStatus) -> str:
    """What the summary shows next to a stage: its log, or why it didn't run."""
    if status.state in (StageState.SKIPPED, StageState.BLOCKED):
        return status.note
    if status.log_path:
        return status.log_path + ("  (%s)" % status.note if status.note else "")
    return status.note


def result_label(status: StageStatus) -> str:
    """State for the summary, with the attempt number once a stage was retried."""
    if status.attempt > 1:
        return "%s (attempt %d)" % (status.state.value, status.attempt)
    return status.state.value


def retry_message(stages: List[StageStatus]) -> str:
    """'Retrying: ...' for stages queued for another attempt, or "" if there are none."""
    retried = ["%s (attempt %d)" % (s.name, s.attempt)
               for s in stages if s.state is StageState.PENDING and s.attempt > 1]
    return "Retrying: %s" % ", ".join(retried) if retried else ""


def overall_result(stages: List[StageStatus], interrupted: bool) -> str:
    if interrupted or any(s.state is StageState.CANCELLED for s in stages):
        return "INTERRUPTED"
    failed = sum(1 for s in stages if s.state is StageState.FAILED)
    blocked = sum(1 for s in stages if s.state is StageState.BLOCKED)
    if failed or blocked:
        return "FAILED (%d failed, %d blocked)" % (failed, blocked)
    return "SUCCESS"


class Display:
    """Receives stage snapshots from the scheduler (always on the main thread)."""

    # True for an interactive display that offers retries itself (the TUI):
    # the scheduler then never asks its [y/N] retry question.
    handles_retry = False

    def __init__(
        self,
        run_id: str,
        run_dir: str,
        status_interval: float = 30.0,
        stream: Optional[TextIO] = None,
    ) -> None:
        self.run_id = run_id
        self.run_dir = run_dir
        self.status_interval = status_interval
        self.stream = stream if stream is not None else sys.stdout
        self.t0 = time.monotonic()
        self.controller = None  # the Scheduler, set by attach()

    def attach(self, controller) -> None:
        """Called by the scheduler so interactive displays can cancel or retry."""
        self.controller = controller

    def wait(self, seconds: float) -> None:
        """Pause between scheduler polls. Interactive displays read keys here."""
        time.sleep(seconds)

    def holds_open(self) -> bool:
        """True keeps the run open after every stage finished (until the user quits)."""
        return False

    def run_elapsed(self) -> str:
        return fmt_duration(time.monotonic() - self.t0)

    def start(self, stages: List[StageStatus]) -> None:
        pass

    def update(self, stages: List[StageStatus]) -> None:
        pass

    def message(self, text: str) -> None:
        self._print(text)

    def stop(self, stages: List[StageStatus], interrupted: bool = False) -> None:
        self.print_summary(stages, interrupted)

    def pause(self, stages: List[StageStatus]) -> None:
        """Stop live output, leaving the current state on screen, for the retry question.

        Followed by resume(), or by stop() if the run ended meanwhile.
        """

    def resume(self, stages: List[StageStatus]) -> None:
        """Restart live output after pause()."""
        msg = retry_message(stages)
        if msg:
            self.message(msg)
        self.start(stages)

    def print_summary(self, stages: List[StageStatus], interrupted: bool = False) -> None:
        rows = [("STAGE", "RESULT", "DURATION", "DETAILS")]
        for s in stages:
            rows.append((s.name, result_label(s), fmt_duration(s.elapsed()), stage_detail(s)))
        w0 = max(len(r[0]) for r in rows)
        w1 = max(len(r[1]) for r in rows)
        w2 = max(len(r[2]) for r in rows)
        out = ["", "Summary (run %s, total %s)" % (self.run_id, self.run_elapsed())]
        for r in rows:
            out.append("  %-*s  %-*s  %-*s  %s" % (w0, r[0], w1, r[1], w2, r[2], r[3]))
        out.append("Result: %s" % overall_result(stages, interrupted))
        out.append("Logs:   %s" % self.run_dir)
        self._print("\n".join(out))

    def _print(self, text: str) -> None:
        self.stream.write(text + "\n")
        self.stream.flush()


class GuardedDisplay:
    """Wraps the scheduler's display so a dead output never takes the build down.

    If writing fails (a closed pipe such as `| head`, a terminal that went away),
    the display is switched off, stdout is pointed at /dev/null, and the build
    continues; orchestrator.log and the stage logs still record everything.
    """

    def __init__(self, inner: Display) -> None:
        self.inner = inner
        self.dead = False

    @property
    def handles_retry(self) -> bool:
        return self.inner.handles_retry and not self.dead

    def attach(self, controller) -> None:
        self.inner.attach(controller)

    def _call(self, name: str, *args) -> None:
        if self.dead:
            return
        try:
            getattr(self.inner, name)(*args)
        except OSError as exc:
            self._die(exc)

    def _die(self, exc: OSError) -> None:
        self.dead = True
        logging.getLogger("icb_build").warning(
            "display output failed (%s); continuing without terminal output", exc)
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, self.inner.stream.fileno())
            os.close(devnull)
        except (OSError, AttributeError, ValueError):
            pass

    def start(self, stages: List[StageStatus]) -> None:
        self._call("start", stages)

    def update(self, stages: List[StageStatus]) -> None:
        self._call("update", stages)

    def message(self, text: str) -> None:
        self._call("message", text)

    def stop(self, stages: List[StageStatus], interrupted: bool = False) -> None:
        self._call("stop", stages, interrupted)

    def pause(self, stages: List[StageStatus]) -> None:
        self._call("pause", stages)

    def resume(self, stages: List[StageStatus]) -> None:
        self._call("resume", stages)

    def wait(self, seconds: float) -> None:
        if self.dead:
            time.sleep(seconds)
        else:
            self._call("wait", seconds)

    def holds_open(self) -> bool:
        # With no terminal left, nobody can press q: let the run end when stages finish.
        return not self.dead and self.inner.holds_open()
