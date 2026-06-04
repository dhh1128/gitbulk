"""Tests for :mod:`gitbulk.util.progress`."""

from __future__ import annotations

import io

import pytest

from gitbulk.util.progress import Progress


class _TTYBuffer(io.StringIO):
    """A StringIO that lies about being a TTY so Progress enables itself."""

    def isatty(self) -> bool:
        return True


def test_progress_disabled_on_non_tty():
    """When the stream isn't a TTY (cron, piped output), progress
    writes nothing — log files would otherwise accumulate one line
    per step."""
    buf = io.StringIO()  # plain StringIO.isatty() is False
    p = Progress(10, prefix="loading: ", stream=buf)
    p.update(1, "first")
    p.update(5, "halfway")
    p.done()
    assert buf.getvalue() == ""


def test_progress_disabled_when_total_zero():
    """Zero items = nothing to show, even on a TTY."""
    buf = _TTYBuffer()
    p = Progress(0, stream=buf)
    p.update(0)
    p.done()
    assert buf.getvalue() == ""


def test_progress_writes_to_tty():
    buf = _TTYBuffer()
    p = Progress(3, prefix="check: ", stream=buf)
    p.update(1, "foo")
    written = buf.getvalue()
    assert "check: 1/3 — foo" in written
    assert written.startswith("\r")


def test_progress_overwrites_previous_line():
    """Subsequent updates rewrite (with \\r + padding) so a longer-then-
    shorter sequence doesn't leave trailing characters visible."""
    buf = _TTYBuffer()
    p = Progress(3, prefix="x ", stream=buf)
    p.update(1, "looooooooong message")
    p.update(2, "short")
    written = buf.getvalue()
    assert "x 2/3 — short" in written
    # Padding after the short line — count spaces.
    last_segment = written.split("x 2/3 — short")[-1]
    assert len(last_segment) >= len("looooooooong message") - len("short")


def test_progress_without_message_omits_dash():
    buf = _TTYBuffer()
    p = Progress(5, stream=buf)
    p.update(2)
    written = buf.getvalue()
    assert "2/5" in written
    assert " — " not in written


def test_progress_done_clears_line():
    buf = _TTYBuffer()
    p = Progress(2, prefix="x: ", stream=buf)
    p.update(1, "first")
    p.done()
    assert buf.getvalue().endswith("\r")


def test_progress_done_idempotent():
    buf = _TTYBuffer()
    p = Progress(2, stream=buf)
    p.update(1)
    p.done()
    before = buf.getvalue()
    p.done()
    after = buf.getvalue()
    assert before == after


def test_progress_done_noop_when_never_updated():
    buf = _TTYBuffer()
    p = Progress(5, stream=buf)
    p.done()
    assert buf.getvalue() == ""


def test_progress_context_manager_calls_done():
    buf = _TTYBuffer()
    with Progress(3, prefix="ctx: ", stream=buf) as p:
        p.update(1, "first")
    assert buf.getvalue().endswith("\r")


# ─── wait-suffix fold + active registry (node rsclk7nq UX) ──────────────────


@pytest.fixture(autouse=True)
def _reset_active_progress():
    yield
    import gitbulk.util.progress as prog
    prog._active = None


def test_progress_set_wait_suffix_folds_into_line():
    buf = _TTYBuffer()
    p = Progress(3, prefix="rebasing: ", stream=buf)
    p.update(2, "owner/repo")
    p.set_wait_suffix("waiting 27s")
    assert "rebasing: 2/3 — owner/repo — waiting 27s" in buf.getvalue()


def test_progress_clear_wait_suffix_repaints_without_it():
    buf = _TTYBuffer()
    p = Progress(3, prefix="x ", stream=buf)
    p.update(1, "m")
    p.set_wait_suffix("waiting")
    p.clear_wait_suffix()
    assert "waiting" not in buf.getvalue().split("\r")[-1]


def test_progress_clear_wait_suffix_noop_when_absent():
    buf = _TTYBuffer()
    p = Progress(2, stream=buf)
    p.update(1)
    before = buf.getvalue()
    p.clear_wait_suffix()
    assert buf.getvalue() == before


def test_progress_set_wait_suffix_noop_when_disabled():
    buf = io.StringIO()  # not a TTY
    p = Progress(3, stream=buf)
    p.set_wait_suffix("x")
    p.clear_wait_suffix()
    assert buf.getvalue() == ""


def test_active_progress_tracks_current_bar():
    from gitbulk.util import progress as prog

    assert prog.active_progress() is None
    buf = _TTYBuffer()
    p = Progress(3, stream=buf)
    p.update(1)
    assert prog.active_progress() is p
    p.done()
    assert prog.active_progress() is None


def test_active_progress_none_for_disabled_bar():
    from gitbulk.util import progress as prog

    buf = io.StringIO()  # not a TTY → disabled, never registers
    p = Progress(3, stream=buf)
    p.update(1)
    assert prog.active_progress() is None
