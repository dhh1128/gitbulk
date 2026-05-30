"""Terminal styling for gitbulk: semantic color + status glyphs.

gitbulk is run interactively *and* unattended from cron, often with its
output piped into another command or captured into a MAILTO log. Color
and glyphs help a human scan a 150-repo run for the one thing that needs
attention, but they are noise — or outright corruption — in a pipe or a
log file. This module resolves those concerns into two **independent
capability gates**, computed per output stream:

  - **color** — whether ANSI SGR escapes are emitted at all.
  - **unicode** — whether status markers render as glyphs (``✓ ⚠ ✗``)
    or as ASCII fallbacks (``[ok] [!] [x]``).

They are independent because the two failure modes are independent: a
``dumb`` terminal or a redirected file may handle UTF-8 fine but want no
ANSI, and vice-versa.

Design rules worth preserving:

  - **Semantic, not decorative.** Every style maps to a *meaning*
    (clean/attention/error), never "make the header blue". Disabling
    color therefore only ever hides *emphasis*, never *information*.
  - **Emphasis glyphs are color-gated.** The ``✓/⚠/✗`` marker on a
    summary line appears only when color is on, so with color off the
    line is byte-identical to the pre-color era and any downstream
    parser that reads gitbulk's stdout keeps working unchanged.

Color-enablement precedence (highest first):

  1. ``NO_COLOR`` present (any value) → **off** — the no-color.org
     standard. This deliberately outranks ``FORCE_COLOR``: ``NO_COLOR``
     is an accessibility/environment opt-out and is treated as the
     strongest signal. ``FORCE_COLOR``'s job is to beat the isatty
     check (e.g. when piping into ``less -R``), not to override an
     explicit opt-out.
  2. ``FORCE_COLOR`` / ``CLICOLOR_FORCE`` truthy → **on**.
  3. ``TERM == "dumb"`` → **off**.
  4. ``stream.isatty()`` → on if a TTY, else off.

There is intentionally no ``--color`` CLI flag: ``NO_COLOR=1 gitbulk …``
and ``FORCE_COLOR=1 gitbulk … | tee log`` cover the cases without adding
a global option to thread through every argparse subparser.
"""

from __future__ import annotations

import os
import sys
from typing import IO, Mapping

#: ANSI Select-Graphic-Rendition codes, by friendly name.
_SGR: dict[str, str] = {
    "reset": "0",
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
}

#: Outcome category → (color name, unicode glyph, ASCII fallback glyph).
#: The categories are the three states a gitbulk run resolves to; the
#: glyph is the emphasis marker prepended to a summary line.
_OUTCOME: dict[str, tuple[str, str, str]] = {
    "ok": ("green", "✓", "[ok]"),
    "attention": ("yellow", "⚠", "[!]"),
    "error": ("red", "✗", "[x]"),
}

#: gitbulk exit codes → outcome category. The integer literals mirror the
#: authoritative ``EXIT_*`` constants in :mod:`gitbulk.cli` (and the copies
#: each command module keeps); they are duplicated here — rather than
#: imported — to keep this low-level module free of a cli import, which
#: would create a cycle (cli → commands → style → cli). The drift guard is
#: ``test_style.test_exit_code_map_matches_cli_constants``.
_OUTCOME_BY_EXIT_CODE: dict[int, str] = {
    0: "ok",         # EXIT_OK
    1: "error",      # EXIT_STRUCTURAL_FAILURE
    2: "attention",  # EXIT_ATTENTION_NEEDED
    3: "attention",  # EXIT_INVARIANT_SKIPPED
    4: "attention",  # EXIT_OVERRIDES_APPLIED
    99: "error",     # EXIT_NOT_IMPLEMENTED
}


def outcome_for_exit_code(code: int) -> str:
    """Classify a gitbulk exit code as ``"ok"``, ``"attention"`` or
    ``"error"``. Unknown codes are treated as ``"error"`` — fail loud,
    never silently paint an unrecognized outcome as success."""
    return _OUTCOME_BY_EXIT_CODE.get(code, "error")


def _color_enabled(stream: IO[str], env: Mapping[str, str]) -> bool:
    """Resolve whether ANSI color should be emitted on ``stream``.

    See the module docstring for the precedence rationale.
    """
    if env.get("NO_COLOR") is not None:
        return False
    if env.get("FORCE_COLOR") or env.get("CLICOLOR_FORCE"):
        return True
    if env.get("TERM") == "dumb":
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        # AttributeError: stream has no isatty(); ValueError: I/O
        # operation on a closed file. Either way, don't emit escapes.
        return False


def _unicode_enabled(stream: IO[str]) -> bool:
    """Whether ``stream`` can render Unicode glyphs, inferred from its
    declared encoding. A non-UTF encoding (or none) falls back to ASCII."""
    enc = (getattr(stream, "encoding", None) or "").lower()
    return "utf" in enc


class Style:
    """Resolved styling capability for a single output stream.

    Capability rarely changes within a process, so construct one of these
    per stream and reuse it. ``enabled`` gates color; ``unicode`` gates
    glyph rendering. Both are plain attributes so callers can branch on
    them when composing richer output.
    """

    def __init__(
        self,
        stream: IO[str] | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        resolved_stream = stream if stream is not None else sys.stdout
        resolved_env = env if env is not None else os.environ
        self.enabled: bool = _color_enabled(resolved_stream, resolved_env)
        self.unicode: bool = _unicode_enabled(resolved_stream)

    def paint(self, text: str, *names: str) -> str:
        """Wrap ``text`` in the named SGR styles (e.g. ``"red"``,
        ``"bold"``), or return it unchanged when color is disabled or no
        styles were given. Unknown style names raise ``KeyError`` so
        typos surface in tests rather than as broken escape sequences."""
        if not self.enabled or not names:
            return text
        codes = ";".join(_SGR[name] for name in names)
        return f"\033[{codes}m{text}\033[{_SGR['reset']}m"

    def glyph(self, outcome: str) -> str:
        """The colored status marker for an outcome category, or ``""``
        when color is disabled (emphasis glyphs are color-gated)."""
        if not self.enabled:
            return ""
        color, unicode_glyph, ascii_glyph = _OUTCOME[outcome]
        marker = unicode_glyph if self.unicode else ascii_glyph
        return self.paint(marker, color)

    def summary(self, text: str, outcome: str) -> str:
        """Compose a final summary line as ``"<glyph> text"``, or just
        ``text`` when color is disabled (so piped output is unchanged)."""
        marker = self.glyph(outcome)
        return f"{marker} {text}" if marker else text

    def error(self, text: str) -> str:
        """Paint a whole error message red (full-line, by convention)."""
        return self.paint(text, "red")

    def warn(self, text: str) -> str:
        """Paint a whole warning message yellow."""
        return self.paint(text, "yellow")


def summary_line(text: str, exit_code: int) -> str:
    """Format a command's final stdout summary line, coloring its outcome
    glyph from ``exit_code``. Resolves capability against the *live*
    ``sys.stdout`` at call time so output capture/redirection is honored."""
    return Style(sys.stdout).summary(text, outcome_for_exit_code(exit_code))


def error_line(text: str) -> str:
    """Format an error/abort message for stderr, painted red when stderr
    supports color. Resolves against the live ``sys.stderr`` at call time."""
    return Style(sys.stderr).error(text)


__all__ = [
    "Style",
    "outcome_for_exit_code",
    "summary_line",
    "error_line",
]
