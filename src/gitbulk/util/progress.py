"""Lightweight progress reporter for long-running gitbulk loops.

A 175-repo ``gitbulk report`` sequentially calls ``gh api repos/<slug>``
(via the ``github.reachable`` invariant) — each call is ~200-500ms and
they run back-to-back, so the user sees nothing for over a minute. The
progress reporter prints a one-line "N/total — <message>" indicator to
stderr so it's clear gitbulk is alive and making progress.

Behavior depends on the output stream:

  - **stderr is a TTY** (interactive): writes the indicator with a
    leading ``\\r`` so each update overwrites the previous one. Calling
    :meth:`Progress.done` clears the line so subsequent output starts
    fresh.
  - **stderr is not a TTY** (cron, piped, redirected): writes nothing.
    cron logs would otherwise accumulate one line per step, which is
    noise the operator doesn't want in their MAILTO.

Output uses sys.stderr — never stdout — so it doesn't interfere with
machine-readable result lines that handlers print on completion. The
``stream`` parameter exists for tests; production callers should leave
it at the default.
"""

from __future__ import annotations

import sys
from typing import IO


class Progress:
    """Stateful one-line progress indicator. Use as a context manager
    or call ``done()`` explicitly when finished."""

    def __init__(
        self,
        total: int,
        *,
        prefix: str = "",
        stream: IO[str] | None = None,
    ) -> None:
        self._total = total
        self._prefix = prefix
        self._stream = stream if stream is not None else sys.stderr
        self._enabled = self._stream.isatty() and total > 0
        self._last_len = 0

    def update(self, n: int, message: str = "") -> None:
        """Print "N/total — message" overwriting the previous line.

        ``n`` is 1-based (the count of items COMPLETED through this
        update). Caller decides whether to call before or after each
        item is processed; both work.
        """
        if not self._enabled:
            return
        if message:
            line = f"{self._prefix}{n}/{self._total} — {message}"
        else:
            line = f"{self._prefix}{n}/{self._total}"
        # Pad with spaces to overwrite the previous (longer) line.
        pad = max(0, self._last_len - len(line))
        self._stream.write(f"\r{line}{' ' * pad}")
        self._stream.flush()
        self._last_len = len(line)

    def done(self) -> None:
        """Clear the progress line so subsequent output starts clean."""
        if not self._enabled or self._last_len == 0:
            return
        self._stream.write("\r" + " " * self._last_len + "\r")
        self._stream.flush()
        self._last_len = 0

    def __enter__(self) -> "Progress":
        return self

    def __exit__(self, *exc_info) -> None:
        self.done()


__all__ = ["Progress"]
