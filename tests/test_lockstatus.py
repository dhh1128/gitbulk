"""Tests for :mod:`gitbulk.util.lockstatus` (node rsclk7nq UX)."""

from __future__ import annotations

import io

import pytest

from gitbulk.util.lockstatus import TtyLockStatusReporter
from gitbulk.util.progress import Progress


class _FakeTTY(io.StringIO):
    """A StringIO that claims to be a TTY with a configurable encoding."""

    def __init__(self, encoding: str = "utf-8") -> None:
        super().__init__()
        self._encoding = encoding

    @property
    def encoding(self) -> str:
        return self._encoding

    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _plain_and_reset(monkeypatch):
    # Deterministic, ANSI-free output for assertions.
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    yield
    import gitbulk.util.progress as prog

    prog._active = None


# ─── own-line rendering ─────────────────────────────────────────────────────


def test_waiting_writes_own_line_with_countdown():
    buf = _FakeTTY()
    r = TtyLockStatusReporter(stream=buf)
    r.waiting(label="run-state (merge)", holder=None, remaining=26.4, elapsed=1.0)
    out = buf.getvalue()
    assert out.startswith("\r")
    assert "⏳ waiting on run-state (merge) lock" in out
    assert "27s left" in out  # ceil(26.4)


def test_waiting_includes_running_holder():
    buf = _FakeTTY()
    r = TtyLockStatusReporter(stream=buf)
    r.waiting(
        label="repo (o/r)",
        holder={"pid": 42, "subcommand": "merge", "alive": True},
        remaining=5,
        elapsed=0,
    )
    assert "held by merge (pid 42 running)" in buf.getvalue()


def test_holder_phrase_variants():
    r = TtyLockStatusReporter(stream=_FakeTTY())
    assert r._holder_phrase(None) == ""
    assert "stale" in r._holder_phrase(
        {"pid": 1, "subcommand": "x", "alive": False}
    )
    # alive unknown, subcommand missing → "?" and no liveness word.
    phrase = r._holder_phrase({"pid": 7})
    assert "pid 7" in phrase and "(pid 7)" in phrase and "?" in phrase


def test_ascii_glyph_when_stream_not_unicode():
    buf = _FakeTTY(encoding="ascii")
    r = TtyLockStatusReporter(stream=buf)
    r.waiting(label="x", holder=None, remaining=5, elapsed=0)
    out = buf.getvalue()
    assert "[waiting]" in out
    assert "⏳" not in out


def test_acquired_after_wait_clears_own_line():
    buf = _FakeTTY()
    r = TtyLockStatusReporter(stream=buf)
    r.waiting(label="x", holder=None, remaining=5, elapsed=0)
    r.acquired(label="x", waited=2.0)
    assert buf.getvalue().endswith("\r")  # line cleared


def test_acquired_uncontended_is_silent():
    buf = _FakeTTY()
    r = TtyLockStatusReporter(stream=buf)
    r.acquired(label="x", waited=0.0)
    assert buf.getvalue() == ""


def test_gave_up_clears_own_line():
    buf = _FakeTTY()
    r = TtyLockStatusReporter(stream=buf)
    r.waiting(label="x", holder=None, remaining=5, elapsed=0)
    r.gave_up(label="x")
    assert buf.getvalue().endswith("\r")


# ─── gating ─────────────────────────────────────────────────────────────────


def test_silent_on_non_tty():
    buf = io.StringIO()  # plain StringIO → isatty() False
    r = TtyLockStatusReporter(stream=buf)
    r.waiting(label="x", holder=None, remaining=5, elapsed=0)
    r.acquired(label="x", waited=3.0)
    r.gave_up(label="x")
    assert buf.getvalue() == ""


# ─── fold into an active Progress bar ───────────────────────────────────────


def test_folds_into_active_progress_bar():
    pbuf = _FakeTTY()
    rbuf = _FakeTTY()
    p = Progress(3, prefix="rebasing: ", stream=pbuf)
    p.update(2, "owner/repo")  # registers as the active bar

    r = TtyLockStatusReporter(stream=rbuf)
    r.waiting(label="repo (owner/repo)", holder=None, remaining=9, elapsed=0)
    # Folded into the bar's line; nothing on the reporter's own stream.
    assert rbuf.getvalue() == ""
    assert "waiting on repo (owner/repo)" in pbuf.getvalue()

    r.acquired(label="repo (owner/repo)", waited=1.0)
    # Suffix removed on the bar's latest repaint.
    assert "waiting on repo" not in pbuf.getvalue().split("\r")[-1]


def test_color_is_applied_when_enabled(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    buf = _FakeTTY()
    r = TtyLockStatusReporter(stream=buf)
    r.waiting(label="x", holder=None, remaining=5, elapsed=0)
    assert "\033[" in buf.getvalue()  # ANSI present


def test_reporter_disabled_when_isatty_raises():
    class _Bad(io.StringIO):
        def isatty(self):
            raise ValueError("stream closed")

    r = TtyLockStatusReporter(stream=_Bad())
    assert r._enabled is False
    # No crash and nothing rendered when disabled.
    r.waiting(label="x", holder=None, remaining=5, elapsed=0)
    r.acquired(label="x", waited=2.0)


def test_clear_when_folded_but_bar_already_gone():
    pbuf = _FakeTTY()
    rbuf = _FakeTTY()
    p = Progress(3, prefix="x ", stream=pbuf)
    p.update(1, "m")
    r = TtyLockStatusReporter(stream=rbuf)
    r.waiting(label="repo (o/r)", holder=None, remaining=5, elapsed=0)
    assert r._folded is True
    p.done()  # the bar finishes → active_progress() becomes None
    r.gave_up(label="repo (o/r)")  # clears with _folded True but no active bar
    assert r._folded is False  # reset cleanly, no crash
