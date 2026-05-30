"""Tests for :mod:`gitbulk.util.style`.

The styling module has two independent capability gates — ANSI *color*
and Unicode *glyph* rendering — resolved per output stream. These tests
pin the resolution precedence (the de-facto ``NO_COLOR`` / ``FORCE_COLOR``
contract) and the "emphasis glyphs drop with color so piped output stays
byte-identical" guarantee that protects downstream parsers.
"""

from __future__ import annotations

import sys

import pytest

from gitbulk.util import style as st
from gitbulk.util.style import (
    Style,
    error_line,
    outcome_for_exit_code,
    summary_line,
)


class _FakeStream:
    """Stand-in stream that lets a test dictate ``isatty()`` and
    ``encoding`` without touching a real terminal."""

    def __init__(self, *, tty: bool = False, encoding: str | None = "utf-8",
                 isatty_raises: bool = False) -> None:
        self._tty = tty
        self.encoding = encoding
        self._isatty_raises = isatty_raises

    def isatty(self) -> bool:
        if self._isatty_raises:
            raise ValueError("I/O operation on closed file")
        return self._tty


# --------------------------------------------------------------------------
# Color-enablement precedence
# --------------------------------------------------------------------------

@pytest.mark.parametrize("no_color_value", ["", "0", "1", "anything"])
def test_no_color_disables_regardless_of_value(no_color_value):
    """The NO_COLOR standard: presence disables color whatever the value."""
    s = Style(_FakeStream(tty=True), env={"NO_COLOR": no_color_value})
    assert s.enabled is False


def test_no_color_wins_over_force_color():
    """Deliberate precedence decision: NO_COLOR (an accessibility opt-out)
    outranks FORCE_COLOR. Documented in the module; this test guards it."""
    s = Style(_FakeStream(tty=False), env={"NO_COLOR": "", "FORCE_COLOR": "1"})
    assert s.enabled is False


def test_force_color_enables_when_piped():
    s = Style(_FakeStream(tty=False), env={"FORCE_COLOR": "1"})
    assert s.enabled is True


def test_clicolor_force_enables_when_piped():
    s = Style(_FakeStream(tty=False), env={"CLICOLOR_FORCE": "1"})
    assert s.enabled is True


def test_empty_force_color_does_not_force():
    """An empty/falsy FORCE_COLOR must not force; resolution falls through
    to the isatty check."""
    s = Style(_FakeStream(tty=True), env={"FORCE_COLOR": ""})
    assert s.enabled is True
    s2 = Style(_FakeStream(tty=False), env={"FORCE_COLOR": ""})
    assert s2.enabled is False


def test_term_dumb_disables():
    s = Style(_FakeStream(tty=True), env={"TERM": "dumb"})
    assert s.enabled is False


def test_tty_enables_plain_terminal():
    s = Style(_FakeStream(tty=True), env={"TERM": "xterm-256color"})
    assert s.enabled is True


def test_non_tty_disables():
    s = Style(_FakeStream(tty=False), env={})
    assert s.enabled is False


def test_isatty_raising_is_treated_as_non_tty():
    """A closed stream raises ValueError on isatty(); treat as no-color
    rather than crashing the run."""
    s = Style(_FakeStream(isatty_raises=True), env={})
    assert s.enabled is False


# --------------------------------------------------------------------------
# Unicode-enablement (independent gate)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("encoding,expected", [
    ("utf-8", True),
    ("UTF-8", True),
    ("ascii", False),
    ("latin-1", False),
    (None, False),
])
def test_unicode_gate_follows_encoding(encoding, expected):
    s = Style(_FakeStream(tty=True, encoding=encoding), env={"FORCE_COLOR": "1"})
    assert s.unicode is expected


# --------------------------------------------------------------------------
# paint()
# --------------------------------------------------------------------------

def test_paint_disabled_returns_plain():
    s = Style(_FakeStream(tty=False), env={})
    assert s.paint("hello", "red") == "hello"


def test_paint_enabled_wraps_in_ansi():
    s = Style(_FakeStream(tty=True), env={"FORCE_COLOR": "1"})
    out = s.paint("hello", "red")
    assert out == "\033[31mhello\033[0m"


def test_paint_combines_multiple_styles():
    s = Style(_FakeStream(tty=True), env={"FORCE_COLOR": "1"})
    out = s.paint("hi", "bold", "green")
    assert out == "\033[1;32mhi\033[0m"


def test_paint_no_names_returns_plain_even_when_enabled():
    s = Style(_FakeStream(tty=True), env={"FORCE_COLOR": "1"})
    assert s.paint("hi") == "hi"


def test_paint_unknown_style_raises():
    """Typos in style names fail loudly so they're caught in tests, not
    silently emitted as broken escape sequences."""
    s = Style(_FakeStream(tty=True), env={"FORCE_COLOR": "1"})
    with pytest.raises(KeyError):
        s.paint("hi", "chartreuse")


# --------------------------------------------------------------------------
# glyph() and summary()
# --------------------------------------------------------------------------

def test_glyph_empty_when_color_disabled():
    """Emphasis glyphs are color-gated: with color off there is no glyph
    at all, so piped/redirected output is byte-identical to pre-color."""
    s = Style(_FakeStream(tty=False), env={})
    assert s.glyph("ok") == ""


def test_glyph_unicode_when_capable():
    s = Style(_FakeStream(tty=True, encoding="utf-8"), env={"FORCE_COLOR": "1"})
    assert s.glyph("ok") == "\033[32m✓\033[0m"
    assert s.glyph("attention") == "\033[33m⚠\033[0m"
    assert s.glyph("error") == "\033[31m✗\033[0m"


def test_glyph_ascii_fallback_when_not_unicode():
    s = Style(_FakeStream(tty=True, encoding="ascii"), env={"FORCE_COLOR": "1"})
    assert s.glyph("ok") == "\033[32m[ok]\033[0m"
    assert s.glyph("attention") == "\033[33m[!]\033[0m"
    assert s.glyph("error") == "\033[31m[x]\033[0m"


def test_summary_prepends_glyph_when_enabled():
    s = Style(_FakeStream(tty=True, encoding="utf-8"), env={"FORCE_COLOR": "1"})
    out = s.summary("gitbulk report: all clear", "ok")
    assert out == "\033[32m✓\033[0m gitbulk report: all clear"


def test_summary_plain_when_disabled():
    s = Style(_FakeStream(tty=False), env={})
    assert s.summary("gitbulk report: all clear", "ok") == "gitbulk report: all clear"


# --------------------------------------------------------------------------
# error()/warn()
# --------------------------------------------------------------------------

def test_error_paints_red_when_enabled():
    s = Style(_FakeStream(tty=True), env={"FORCE_COLOR": "1"})
    assert s.error("boom") == "\033[31mboom\033[0m"


def test_warn_paints_yellow_when_enabled():
    s = Style(_FakeStream(tty=True), env={"FORCE_COLOR": "1"})
    assert s.warn("careful") == "\033[33mcareful\033[0m"


def test_error_and_warn_plain_when_disabled():
    s = Style(_FakeStream(tty=False), env={})
    assert s.error("boom") == "boom"
    assert s.warn("careful") == "careful"


# --------------------------------------------------------------------------
# outcome_for_exit_code()
# --------------------------------------------------------------------------

@pytest.mark.parametrize("code,outcome", [
    (0, "ok"),          # EXIT_OK
    (1, "error"),       # EXIT_STRUCTURAL_FAILURE
    (2, "attention"),   # EXIT_ATTENTION_NEEDED
    (3, "attention"),   # EXIT_INVARIANT_SKIPPED
    (4, "attention"),   # EXIT_OVERRIDES_APPLIED
    (99, "error"),      # EXIT_NOT_IMPLEMENTED
])
def test_outcome_for_known_exit_codes(code, outcome):
    assert outcome_for_exit_code(code) == outcome


def test_outcome_for_unknown_exit_code_is_error():
    """An unmapped code is treated as an error — fail loud, never silently
    paint an unknown outcome as success."""
    assert outcome_for_exit_code(7) == "error"


def test_exit_code_map_matches_cli_constants():
    """Guard the duplicated integer literals against drift from the
    authoritative EXIT_* constants in cli.py."""
    from gitbulk import cli
    assert outcome_for_exit_code(cli.EXIT_OK) == "ok"
    assert outcome_for_exit_code(cli.EXIT_STRUCTURAL_FAILURE) == "error"
    assert outcome_for_exit_code(cli.EXIT_ATTENTION_NEEDED) == "attention"
    assert outcome_for_exit_code(cli.EXIT_INVARIANT_SKIPPED) == "attention"
    assert outcome_for_exit_code(cli.EXIT_OVERRIDES_APPLIED) == "attention"
    assert outcome_for_exit_code(cli.EXIT_NOT_IMPLEMENTED) == "error"


# --------------------------------------------------------------------------
# Module-level convenience wrappers (resolve sys.stdout/sys.stderr at call
# time so test capture and redirection are respected).
# --------------------------------------------------------------------------

def test_summary_line_uses_stdout(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setattr(sys, "stdout", _FakeStream(tty=True, encoding="utf-8"))
    out = summary_line("gitbulk report: ok", 0)
    assert out == "\033[32m✓\033[0m gitbulk report: ok"


def test_summary_line_plain_when_not_tty(monkeypatch):
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys, "stdout", _FakeStream(tty=False))
    assert summary_line("gitbulk report: ok", 0) == "gitbulk report: ok"


def test_error_line_uses_stderr(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setattr(sys, "stderr", _FakeStream(tty=True))
    assert error_line("kaboom") == "\033[31mkaboom\033[0m"


def test_default_stream_and_env_paths(monkeypatch):
    """Cover the ``stream is None``/``env is None`` defaulting branches:
    a bare ``Style()`` resolves against sys.stdout and os.environ."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR_FORCE", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setattr(sys, "stdout", _FakeStream(tty=True, encoding="utf-8"))
    s = Style()
    assert s.enabled is True
    assert s.unicode is True


def test_all_exports_present():
    for name in st.__all__:
        assert hasattr(st, name)
