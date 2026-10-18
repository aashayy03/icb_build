"""Log reading for the TUI: tails, an incremental line index and a viewer state machine.

Nothing here draws; the curses code in tui_display.py renders what these return.
"""

from __future__ import annotations

import curses
import os
from typing import List, Optional

from ..runner import clean_line

ESC = 27
BACKSPACE_KEYS = (curses.KEY_BACKSPACE, 127, 8)
ENTER_KEYS = (10, 13, curses.KEY_ENTER)


def _split(data: bytes) -> List[bytes]:
    raw = data.split(b"\n")
    if raw and raw[-1] == b"":
        raw.pop()
    return raw


def tail_lines(path: Optional[str], count: int, max_bytes: int = 64 * 1024) -> List[str]:
    """The last `count` lines of a log, cleaned for display. Reads at most max_bytes."""
    if not path or count <= 0:
        return []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
    except OSError:
        return []
    if size > max_bytes and b"\n" in data:
        data = data.split(b"\n", 1)[1]  # drop the partial first line
    return [clean_line(raw) for raw in _split(data)[-count:]]


class LogIndex:
    """Byte offset of every line start, extended incrementally as the log grows.

    A log replaced by a new file (a retry renames <stage>.log) is re-indexed from scratch.
    """

    CHUNK = 1 << 20

    def __init__(self, path: str) -> None:
        self.path = path
        self._reset()

    def _reset(self) -> None:
        self.offsets = [0]  # type: List[int]
        self.indexed = 0
        self.size = 0
        self.inode = None  # type: Optional[int]

    def refresh(self) -> None:
        try:
            st = os.stat(self.path)
        except OSError:
            self._reset()
            return
        if self.inode is not None and (st.st_ino != self.inode or st.st_size < self.indexed):
            self._reset()
        self.inode = st.st_ino
        self.size = st.st_size
        if st.st_size <= self.indexed:
            return
        with open(self.path, "rb") as fh:
            fh.seek(self.indexed)
            pos = self.indexed
            while pos < st.st_size:
                chunk = fh.read(min(self.CHUNK, st.st_size - pos))
                if not chunk:
                    break
                start = chunk.find(b"\n")
                while start >= 0:
                    self.offsets.append(pos + start + 1)
                    start = chunk.find(b"\n", start + 1)
                pos += len(chunk)
        self.indexed = pos

    def count(self) -> int:
        # The last offset is where the next line would start; at EOF it's not a line yet.
        n = len(self.offsets)
        if self.offsets[-1] >= self.size:
            n -= 1
        return max(0, n)

    def lines(self, start: int, n: int) -> List[str]:
        total = self.count()
        start = max(0, start)
        end = min(total, start + n)
        if start >= end:
            return []
        stop = self.offsets[end] if end < len(self.offsets) else self.size
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.offsets[start])
                data = fh.read(stop - self.offsets[start])
        except OSError:
            return []
        return [clean_line(raw) for raw in _split(data)][: end - start]

    def search(self, needle: str, start: int, backward: bool = False) -> Optional[int]:
        """Index of the first line at/after (or at/before) `start` containing needle."""
        needle = needle.lower()
        total = self.count()
        block = 2000
        if not backward:
            for base in range(max(0, start), total, block):
                for i, line in enumerate(self.lines(base, block)):
                    if needle in line.lower():
                        return base + i
        else:
            pos = min(start, total - 1)
            while pos >= 0:
                base = max(0, pos - block + 1)
                chunk = self.lines(base, pos - base + 1)
                for i in range(len(chunk) - 1, -1, -1):
                    if needle in chunk[i].lower():
                        return base + i
                pos = base - 1
        return None


class LogViewer:
    """State of the full-screen log viewer: scroll position, follow mode and search."""

    def __init__(self, title: str, path: str, follow: bool) -> None:
        self.title = title
        self.index = LogIndex(path)
        self.top = 0
        self.follow = follow
        self.pattern = ""
        self.search_input = None  # type: Optional[str]  # text being typed after '/'
        self.note = ""

    def _bottom(self, height: int) -> int:
        return max(0, self.index.count() - height)

    def refresh(self, height: int) -> None:
        self.index.refresh()
        if self.follow:
            self.top = self._bottom(height)
        self.top = max(0, min(self.top, self._bottom(height)))

    def visible(self, height: int) -> List[str]:
        return self.index.lines(self.top, height)

    def status(self, height: int) -> str:
        total = self.index.count()
        first = self.top + 1 if total else 0
        last = min(total, self.top + height)
        parts = ["%s  lines %d-%d/%d" % (self.title, first, last, total)]
        if self.follow:
            parts.append("FOLLOW")
        if self.pattern:
            parts.append("/" + self.pattern)
        if self.note:
            parts.append(self.note)
        return "  ".join(parts)

    def _find(self, start: int, backward: bool = False) -> None:
        if not self.pattern:
            self.note = "no search pattern (press /)"
            return
        found = self.index.search(self.pattern, start, backward)
        if found is None:
            self.note = "not found: " + self.pattern
        else:
            self.top = found
            self.follow = False
            self.note = "match at line %d" % (found + 1)

    def handle_key(self, key: int, height: int) -> bool:
        """Apply a key. Returns False when the viewer should close."""
        if self.search_input is not None:
            if key in ENTER_KEYS:
                self.pattern, self.search_input = self.search_input, None
                self._find(self.top)
            elif key == ESC:
                self.search_input = None
            elif key in BACKSPACE_KEYS:
                self.search_input = self.search_input[:-1]
            elif 32 <= key < 127:
                self.search_input += chr(key)
            return True

        self.note = ""
        page = max(1, height - 1)
        if key in (ord("q"), ESC):
            return False
        if key in (curses.KEY_UP, ord("k")):
            self.top -= 1
            self.follow = False
        elif key in (curses.KEY_DOWN, ord("j")):
            self.top += 1
        elif key in (curses.KEY_PPAGE, ord("b")):
            self.top -= page
            self.follow = False
        elif key in (curses.KEY_NPAGE, ord(" ")):
            self.top += page
        elif key in (curses.KEY_HOME, ord("g")):
            self.top = 0
            self.follow = False
        elif key in (curses.KEY_END, ord("G")):
            self.top = self._bottom(height)
        elif key == ord("f"):
            self.follow = not self.follow
        elif key == ord("/"):
            self.search_input = ""
        elif key == ord("n"):
            self._find(self.top + 1)
        elif key == ord("N"):
            self._find(self.top - 1, backward=True)
        self.top = max(0, min(self.top, self._bottom(height)))
        return True
