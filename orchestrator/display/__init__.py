"""Status display selection: rich if installed, else built-in ANSI, plain when not a TTY.

The interactive TUI is never picked automatically; it needs --ui tui.
"""

from __future__ import annotations

import os
import sys
from typing import Optional, TextIO

from ..config import ConfigError
from .base import Display


def _rich_available() -> bool:
    try:
        import rich  # noqa: F401
    except ImportError:
        return False
    return True


def resolve_mode(mode: str, stream: TextIO) -> str:
    if mode != "auto":
        return mode
    is_tty = hasattr(stream, "isatty") and stream.isatty()
    if not is_tty or os.environ.get("TERM") == "dumb":
        return "plain"
    return "rich" if _rich_available() else "ansi"


def select_display(
    mode: str,
    run_id: str,
    run_dir: str,
    status_interval: float,
    stream: Optional[TextIO] = None,
) -> Display:
    stream = stream if stream is not None else sys.stdout
    mode = resolve_mode(mode, stream)
    args = (run_id, run_dir, status_interval, stream)
    if mode == "rich":
        try:
            from .rich_display import RichDisplay
        except ImportError:
            raise ConfigError(
                "--ui rich needs the 'rich' package: run 'pip install rich' or use --ui ansi"
            )
        return RichDisplay(*args)
    if mode == "ansi":
        from .ansi_display import AnsiDisplay

        return AnsiDisplay(*args)
    if mode == "plain":
        from .plain_display import PlainDisplay

        return PlainDisplay(*args)
    if mode == "tui":
        if not (hasattr(stream, "isatty") and stream.isatty() and sys.stdin.isatty()):
            raise ConfigError("--ui tui needs an interactive terminal (stdin and stdout)")
        try:
            from .tui_display import TuiDisplay
        except ImportError as exc:
            raise ConfigError("--ui tui is unavailable: %s" % exc)
        return TuiDisplay(*args)
    raise ConfigError("unknown ui mode %r" % mode)
