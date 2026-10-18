"""Stage definitions, dependency graph and runtime status."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

# Order matters: it is the display order and a valid topological order.
STAGE_ORDER = ["tz", "xbl", "qssi", "vendor", "super", "meta"]

# For ICB, super waits for all four standalone builds (not only qssi + vendor).
DEPENDENCIES = {
    "tz": [],
    "xbl": [],
    "qssi": [],
    "vendor": [],
    "super": ["tz", "xbl", "qssi", "vendor"],
    "meta": ["super"],
}  # type: Dict[str, List[str]]


# How a FAILED stage is re-run (Scheduler.retry): a clean full build (r / the
# end-of-run prompt), or an incremental rebuild of qssi/vendor (TUI `i` key).
REBUILD_CLEAN = "clean"
REBUILD_INCREMENTAL = "incremental"


class StageState(Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self not in (StageState.PENDING, StageState.RUNNING)

    @property
    def satisfies_dependents(self) -> bool:
        # SKIPPED counts as done: a skipped stage's earlier output is assumed reusable.
        return self in (StageState.COMPLETED, StageState.SKIPPED)

    @property
    def blocks_dependents(self) -> bool:
        return self in (StageState.FAILED, StageState.BLOCKED, StageState.CANCELLED)


@dataclass
class StageStatus:
    name: str
    state: StageState = StageState.PENDING
    started_at: Optional[float] = None  # time.monotonic()
    ended_at: Optional[float] = None
    start_wall: Optional[datetime] = None
    end_wall: Optional[datetime] = None
    exit_code: Optional[int] = None
    last_line: str = ""
    log_path: Optional[str] = None
    note: str = ""
    attempt: int = 1  # incremented by Scheduler.retry()
    rebuild: str = ""  # how the current attempt re-runs: "", REBUILD_CLEAN or REBUILD_INCREMENTAL

    def elapsed(self, now: Optional[float] = None) -> Optional[float]:
        if self.started_at is None:
            return None
        end = self.ended_at
        if end is None:
            end = time.monotonic() if now is None else now
        return max(0.0, end - self.started_at)


def _validate_graph() -> None:
    seen = set()
    for name in STAGE_ORDER:
        for dep in DEPENDENCIES[name]:
            if dep not in seen:
                raise RuntimeError(
                    "stage %r depends on %r, which is not earlier in STAGE_ORDER" % (name, dep)
                )
        seen.add(name)
    if set(DEPENDENCIES) != seen:
        raise RuntimeError("DEPENDENCIES and STAGE_ORDER list different stages")


_validate_graph()
