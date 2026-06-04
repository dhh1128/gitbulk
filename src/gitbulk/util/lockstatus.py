"""Interactive lock-status indicator (node ``rsclk7nq`` UX).

When stderr is a TTY, show a live one-line notice while a gitbulk run is
BLOCKED waiting on a lock another run holds — with a countdown to the
timeout — so a user running two commands at once can see one waiting on the
other. Silent on uncontended acquires and under cron / pipes.

Folds into an active :class:`gitbulk.util.progress.Progress` bar (so a
``repo_lock`` wait mid-apply shares the bar's line instead of fighting it);
otherwise it owns a single ``\\r`` line on stderr. Output goes to stderr only,
never stdout, so machine-readable result lines are never corrupted.
"""

from __future__ import annotations

import math
import sys
from typing import IO

from gitbulk.util import progress
from gitbulk.util.style import Style


class TtyLockStatusReporter:
    """Lock-status reporter for the ``locks`` module (duck-typed callbacks).

    Engages only while an acquisition is actually blocked: ``waiting`` is
    called each poll tick, then exactly one of ``acquired`` (with the seconds
    waited) or ``gave_up`` (on timeout). An uncontended acquire arrives as
    ``acquired(waited=0)`` and renders nothing.
    """

    def __init__(self, stream: IO[str] | None = None) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._style = Style(self._stream)
        try:
            self._enabled = bool(self._stream.isatty())
        except (AttributeError, ValueError):
            self._enabled = False
        self._own_len = 0     # width of a self-owned line currently on screen
        self._folded = False  # last notice was folded into a Progress bar

    # ── reporter callbacks ──────────────────────────────────────────────────

    def waiting(self, *, label, holder, remaining, elapsed) -> None:
        if not self._enabled:
            return
        msg = self._format(label, holder, remaining)
        bar = progress.active_progress()
        if bar is not None:
            bar.set_wait_suffix(msg)
            self._folded = True
        else:
            self._write_own(msg)

    def acquired(self, *, label, waited) -> None:
        # Uncontended (waited == 0) showed nothing; only clear if we waited.
        if self._enabled and waited > 0:
            self._clear()

    def gave_up(self, *, label) -> None:
        if self._enabled:
            self._clear()

    # ── rendering ───────────────────────────────────────────────────────────

    def _format(self, label, holder, remaining) -> str:
        glyph = "⏳" if self._style.unicode else "[waiting]"
        secs = max(0, math.ceil(remaining))
        msg = (
            f"{glyph} waiting on {label} lock"
            f"{self._holder_phrase(holder)} — {secs}s left"
        )
        return self._style.warn(msg)

    @staticmethod
    def _holder_phrase(holder) -> str:
        if not holder:
            return ""
        pid = holder.get("pid")
        sub = holder.get("subcommand") or "?"
        alive = holder.get("alive")
        live = " running" if alive is True else " stale" if alive is False else ""
        return f" — held by {sub} (pid {pid}{live})"

    def _write_own(self, msg: str) -> None:
        pad = max(0, self._own_len - len(msg))
        self._stream.write(f"\r{msg}{' ' * pad}")
        self._stream.flush()
        self._own_len = len(msg)

    def _clear(self) -> None:
        if self._folded:
            bar = progress.active_progress()
            if bar is not None:
                bar.clear_wait_suffix()
            self._folded = False
        if self._own_len:
            self._stream.write("\r" + " " * self._own_len + "\r")
            self._stream.flush()
            self._own_len = 0


__all__ = ["TtyLockStatusReporter"]
