"""Standard-library live table that redraws in place using ANSI escape codes."""

from __future__ import annotations

import os
import shutil
import time
from typing import List

from ..runner import fmt_duration
from ..stages import StageState, StageStatus
from .base import STATE_SGR, Display

HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
CLEAR_LINE = "\x1b[2K"
CLEAR_DOWN = "\x1b[J"

NAME_W = 8
STATE_W = 10
ELAPSED_W = 9


class AnsiDisplay(Display):
    def start(self, stages: List[StageStatus]) -> None:
        self._drawn = 0
        self._paused = False
        self._color = "NO_COLOR" not in os.environ
        self.stream.write(HIDE_CURSOR)
        self._draw(stages)

    def update(self, stages: List[StageStatus]) -> None:
        self._draw(stages)

    def message(self, text: str) -> None:
        self._erase()
        self._print(text)
        self._drawn = 0

    def pause(self, stages: List[StageStatus]) -> None:
        self._draw(stages)
        self._drawn = 0  # keep the table on screen above the question
        self._paused = True
        self.stream.write(SHOW_CURSOR)
        self.stream.flush()

    def stop(self, stages: List[StageStatus], interrupted: bool = False) -> None:
        if not self._paused:  # a paused table is already on screen
            self._draw(stages)
        self._drawn = 0  # the final table stays on screen; never erase it later
        self.stream.write(SHOW_CURSOR)
        self.stream.flush()
        self.print_summary(stages, interrupted)

    # -- drawing ------------------------------------------------------------

    def _erase(self) -> None:
        if self._drawn:
            # Cursor to the start of the first table line, then clear to end of screen.
            self.stream.write("\x1b[%dF%s" % (self._drawn, CLEAR_DOWN))

    def _paint(self, state: StageState, text: str) -> str:
        if not self._color:
            return text
        return "\x1b[%sm%s\x1b[0m" % (STATE_SGR[state], text)

    def _draw(self, stages: List[StageStatus]) -> None:
        # Truncate every line to the terminal width: a wrapped line would break
        # the line count used to move the cursor back up.
        width = max(40, shutil.get_terminal_size((100, 24)).columns) - 1
        now = time.monotonic()
        lines = [
            ("ICB build  run %s  elapsed %s  logs %s"
             % (self.run_id, self.run_elapsed(), self.run_dir))[:width],
            ("%-*s %-*s %-*s %s" % (NAME_W, "STAGE", STATE_W, "STATE", ELAPSED_W, "ELAPSED",
                                    "LAST OUTPUT"))[:width],
        ]
        fixed = NAME_W + STATE_W + ELAPSED_W + 3
        for s in stages:
            if s.state in (StageState.SKIPPED, StageState.BLOCKED):
                detail = s.note
            else:
                detail = s.last_line or s.note
            room = max(0, width - fixed)
            if len(detail) > room:
                detail = detail[: max(0, room - 3)] + "..." if room > 3 else detail[:room]
            name = ("%-*s" % (NAME_W, s.name))[:NAME_W]
            state = self._paint(s.state, "%-*s" % (STATE_W, s.state.value))
            elapsed = "%-*s" % (ELAPSED_W, fmt_duration(s.elapsed(now)))
            lines.append("%s %s %s %s" % (name, state, elapsed, detail))

        self._erase()
        self.stream.write("".join(CLEAR_LINE + line + "\n" for line in lines))
        self.stream.flush()
        self._drawn = len(lines)
