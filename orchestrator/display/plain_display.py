"""Plain status lines for CI and piped output: no cursor movement, no colours."""

from __future__ import annotations

import time
from typing import List, Optional, Tuple

from ..stages import StageStatus
from .base import Display, retry_message


class PlainDisplay(Display):
    """Prints one status line on every state change and every status_interval seconds."""

    def start(self, stages: List[StageStatus]) -> None:
        self._last_sig = None  # type: Optional[Tuple[str, ...]]
        self._last_emit = 0.0
        self._print("ICB build run %s  logs: %s" % (self.run_id, self.run_dir))
        self.update(stages)

    def update(self, stages: List[StageStatus]) -> None:
        sig = tuple(s.state.value for s in stages)
        now = time.monotonic()
        if sig != self._last_sig or now - self._last_emit >= self.status_interval:
            self._last_sig = sig
            self._last_emit = now
            self._print(
                "[%s] %s" % (self.run_elapsed(), " ".join("%s=%s" % (s.name, s.state.value) for s in stages))
            )

    def resume(self, stages: List[StageStatus]) -> None:
        msg = retry_message(stages)
        if msg:
            self.message(msg)
        self._last_sig = None
        self.update(stages)

    def message(self, text: str) -> None:
        self._print("[%s] %s" % (self.run_elapsed(), text))
