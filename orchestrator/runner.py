"""Run one stage script in its own process group and tee its output to a log file."""

from __future__ import annotations

import getpass
import os
import re
import signal
import socket
import subprocess
import threading
import time
from datetime import datetime
from typing import BinaryIO, Dict, List, Optional

RULE = "=" * 80

# After the script exits, wait this long for its output pipe to drain. A leftover
# grandchild (e.g. a build server daemon) can keep the pipe open indefinitely.
DRAIN_TIMEOUT = 5.0

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean_line(raw: bytes) -> str:
    """Turn a raw output line into something safe to show in one terminal cell."""
    text = raw.decode("utf-8", errors="replace")
    # Progress output rewrites the line with \r; keep the last non-empty segment.
    segments = [s for s in text.rstrip("\r\n").split("\r") if s.strip()]
    text = segments[-1] if segments else ""
    text = _ANSI_RE.sub("", text)
    text = _CTRL_RE.sub("", text).replace("\t", " ")
    return text.strip()


def fmt_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S %z")


def fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    total = int(seconds)
    return "%02d:%02d:%02d" % (total // 3600, (total % 3600) // 60, total % 60)


def describe_exit(code: Optional[int]) -> str:
    if code is None:
        return "-"
    if code < 0:
        try:
            return "%d (killed by %s)" % (code, signal.Signals(-code).name)
        except ValueError:
            return "%d (killed by signal %d)" % (code, -code)
    return str(code)


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "?"


def format_header(
    stage: str,
    run_id: str,
    start: datetime,
    command: List[str],
    cwd: str,
    passed_env: Dict[str, str],
    attempt: int = 1,
) -> str:
    lines = [
        RULE,
        "ICB BUILD  stage=%s  run=%s" % (stage, run_id),
        "start    : %s" % fmt_time(start),
        "host     : %s  (user: %s)" % (socket.gethostname(), _user()),
        "command  : %s" % " ".join(command),
        "cwd      : %s" % cwd,
        "attempt  : %d%s" % (attempt, " (clean before build)" if attempt > 1 else ""),
        "build    : %s" % ("incremental" if passed_env.get("ICB_INCREMENTAL") == "1" else "full"),
        "env      :",
    ]
    lines.extend("  %s=%s" % (k, passed_env[k]) for k in sorted(passed_env))
    lines.append(RULE)
    return "\n".join(lines) + "\n"


def format_footer(
    end: datetime,
    duration: Optional[float],
    exit_code: Optional[int],
    result: str,
    note: str = "",
) -> str:
    lines = [
        RULE,
        "end      : %s" % fmt_time(end),
        "duration : %s" % fmt_duration(duration),
        "exit code: %s" % describe_exit(exit_code),
        "result   : %s" % result,
    ]
    if note:
        lines.append("note     : %s" % note)
    lines.append(RULE)
    return "\n".join(lines) + "\n"


class StageProcess:
    """One running stage script. Owned by the scheduler's main thread.

    A reader thread copies the merged stdout/stderr into the log file and keeps
    the most recent non-empty line for the status display.
    """

    def __init__(
        self,
        name: str,
        command: List[str],
        cwd: str,
        passed_env: Dict[str, str],
        log_path: str,
        run_id: str,
        attempt: int = 1,
    ) -> None:
        self.name = name
        self.command = command
        self.cwd = cwd
        self.passed_env = passed_env
        self.log_path = log_path
        self.run_id = run_id
        self.attempt = attempt
        self.last_line = ""
        self.signal_sent = None  # type: Optional[str]
        self.start_error = None  # type: Optional[str]
        self._lock = threading.Lock()
        self._log = None  # type: Optional[BinaryIO]
        self._proc = None  # type: Optional[subprocess.Popen]
        self._reader = None  # type: Optional[threading.Thread]
        self._exit_seen_at = None  # type: Optional[float]

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc is not None else None

    def start(self, start_wall: datetime) -> bool:
        """Write the log header and launch the script. Returns False if it could not start."""
        self._log = open(self.log_path, "ab")
        self._write(format_header(
            self.name, self.run_id, start_wall, self.command, self.cwd, self.passed_env,
            self.attempt,
        ).encode("utf-8"))

        env = os.environ.copy()
        env.update(self.passed_env)
        try:
            self._proc = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # own process group: killpg reaches make/ninja/etc.
            )
        except OSError as exc:
            self.start_error = "failed to start: %s" % exc
            self._write(("%s\n" % self.start_error).encode("utf-8"))
            return False

        self._reader = threading.Thread(
            target=self._pump, name="log-%s" % self.name, daemon=True
        )
        self._reader.start()
        return True

    def _write(self, data: bytes) -> None:
        with self._lock:
            if self._log is not None:
                self._log.write(data)
                self._log.flush()

    def _pump(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stream = self._proc.stdout
        try:
            for raw in iter(stream.readline, b""):
                self._write(raw)
                text = clean_line(raw)
                if text:
                    self.last_line = text
        finally:
            stream.close()

    def poll(self) -> Optional[int]:
        """Exit code once the script has exited and its output is drained, else None."""
        if self._proc is None:
            return None
        code = self._proc.poll()
        if code is None:
            return None
        if self._exit_seen_at is None:
            self._exit_seen_at = time.monotonic()
        if self._reader is not None and self._reader.is_alive():
            if time.monotonic() - self._exit_seen_at < DRAIN_TIMEOUT:
                return None
        return code

    def send_signal(self, sig: int) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is not None and not self._group_alive():
            return
        try:
            os.killpg(self._proc.pid, sig)
            self.signal_sent = signal.Signals(sig).name
        except (ProcessLookupError, PermissionError):
            pass

    def _group_alive(self) -> bool:
        if self._proc is None:
            return False
        try:
            os.killpg(self._proc.pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def finish(
        self,
        end_wall: datetime,
        duration: Optional[float],
        exit_code: Optional[int],
        result: str,
        note: str = "",
    ) -> None:
        """Write the footer and close the log. Late output from leftovers is dropped."""
        footer = format_footer(end_wall, duration, exit_code, result, note)
        with self._lock:
            if self._log is not None:
                self._log.write(footer.encode("utf-8"))
                self._log.close()
                self._log = None
