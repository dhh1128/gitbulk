"""Smoke tests for the CLI shell.

Phase 1C wires real handlers for ``ack`` and ``invariants``; other
subcommands remain stubs until their phases land. Exit-code-driven
ATTENTION wiring is tested here too.
"""

import io
import sys
from contextlib import redirect_stderr, redirect_stdout

import pytest

from gitbulk import __version__, paths
from gitbulk.cli import (
    EXIT_ATTENTION_NEEDED,
    EXIT_INVARIANT_SKIPPED,
    EXIT_NOT_IMPLEMENTED,
    EXIT_OK,
    EXIT_OVERRIDES_APPLIED,
    EXIT_STRUCTURAL_FAILURE,
    SUBCOMMANDS,
    _check_python_version,
    _configure_logging,
    _maybe_clear_superseded,
    _maybe_set_attention,
    _set_private_umask,
    build_parser,
    main,
)

_IMPLEMENTED_SUBCOMMANDS = {
    "ack",
    "invariants",
    "report",
    "summarize",
    "dispatch",
    "merge",
    "close-stale",
    "rebase-pr",
    "prune-branches",
    "prune-worktrees",
    "recover-branch",
    "show",
}
_STUB_SUBCOMMANDS = [n for n, _ in SUBCOMMANDS if n not in _IMPLEMENTED_SUBCOMMANDS]


def test_version_flag_exits_zero_and_prints_version():
    out = io.StringIO()
    with pytest.raises(SystemExit) as exc, redirect_stdout(out):
        main(["--version"])
    assert exc.value.code == EXIT_OK
    assert __version__ in out.getvalue()


def test_no_args_prints_help_and_exits_zero():
    out = io.StringIO()
    with redirect_stdout(out):
        rc = main([])
    assert rc == EXIT_OK
    assert "SUBCOMMAND" in out.getvalue()


def test_help_lists_every_subcommand():
    out = io.StringIO()
    with pytest.raises(SystemExit), redirect_stdout(out):
        main(["--help"])
    help_text = out.getvalue()
    for name, _ in SUBCOMMANDS:
        assert name in help_text, f"subcommand {name!r} missing from --help output"


@pytest.mark.parametrize("name", _STUB_SUBCOMMANDS)
def test_unimplemented_subcommands_exit_99(name):
    err = io.StringIO()
    with redirect_stderr(err):
        rc = main([name])
    assert rc == EXIT_NOT_IMPLEMENTED
    assert "not yet implemented" in err.getvalue()
    assert name in err.getvalue()


def test_parser_constructs_without_error():
    parser = build_parser()
    assert parser.prog == "gitbulk"


# ─── ack subcommand ────────────────────────────────────────────────────────


@pytest.fixture
def isolated_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    paths.ensure_directories()
    return tmp_path


def test_ack_clears_existing_sentinel(isolated_cache):
    from gitbulk import sentinel

    sentinel.set_attention(2, "report", "RID", "4 PRs need attention")
    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(["ack"])
    assert rc == EXIT_OK
    assert "cleared" in out.getvalue().lower()
    assert not sentinel.has_attention()


def test_ack_when_no_sentinel(isolated_cache):
    from gitbulk import sentinel

    assert not sentinel.has_attention()
    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(["ack"])
    assert rc == EXIT_OK
    assert "no attention sentinel" in out.getvalue().lower()


# ─── invariants subcommand ─────────────────────────────────────────────────


@pytest.fixture
def clean_registry():
    from gitbulk.invariants import registry as registry_mod

    saved = dict(registry_mod._REGISTRY)
    registry_mod._clear()
    yield
    registry_mod._clear()
    registry_mod._REGISTRY.update(saved)


def test_invariants_command_empty_registry(clean_registry):
    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(["invariants"])
    assert rc == EXIT_OK
    assert "no invariants registered" in out.getvalue().lower()


def test_invariants_command_lists_registered(clean_registry):
    from gitbulk.invariants import Invariant, InvariantKind, Pass, register

    inv_cls = type(
        "_TestInv",
        (Invariant,),
        {
            "name": "example.check",
            "kind": InvariantKind.PER_REPO,
            "subcommands": frozenset(["report", "merge"]),
            "check": lambda self, ctx: Pass(),
        },
    )
    register(inv_cls)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(["invariants"])
    assert rc == EXIT_OK
    text = out.getvalue()
    assert "example.check" in text
    assert "per-repo" in text
    assert "report" in text
    assert "merge" in text


# ─── ATTENTION sentinel exit-code wiring ───────────────────────────────────


def test_maybe_set_attention_on_exit_2_with_no_existing(isolated_cache):
    from gitbulk import sentinel

    assert not sentinel.has_attention()
    _maybe_set_attention(EXIT_ATTENTION_NEEDED, "report")
    assert sentinel.has_attention()
    parsed = sentinel.parse_attention()
    assert parsed is not None
    assert parsed["exit_code"] == EXIT_ATTENTION_NEEDED
    assert parsed["subcommand"] == "report"


def test_maybe_set_attention_on_exit_3_with_no_existing(isolated_cache):
    from gitbulk import sentinel

    _maybe_set_attention(EXIT_INVARIANT_SKIPPED, "merge")
    assert sentinel.has_attention()
    parsed = sentinel.parse_attention()
    assert parsed is not None
    assert parsed["exit_code"] == EXIT_INVARIANT_SKIPPED
    assert parsed["subcommand"] == "merge"


def test_maybe_set_attention_does_not_overwrite_existing(isolated_cache):
    from gitbulk import sentinel

    sentinel.set_attention(2, "report", "REAL-RUNID", "real handler summary")
    _maybe_set_attention(EXIT_ATTENTION_NEEDED, "report")
    # Existing richer sentinel is preserved (richer summary survives the fallback)
    parsed = sentinel.parse_attention()
    assert parsed is not None
    assert parsed["runid"] == "REAL-RUNID"
    assert parsed["summary"] == "real handler summary"


def test_maybe_set_attention_does_nothing_on_exit_0(isolated_cache):
    from gitbulk import sentinel

    _maybe_set_attention(EXIT_OK, "report")
    assert not sentinel.has_attention()


def test_maybe_set_attention_does_nothing_on_exit_4(isolated_cache):
    """Exit 4 is an audit signal, not user-attention per design-notes §8."""
    from gitbulk import sentinel

    _maybe_set_attention(EXIT_OVERRIDES_APPLIED, "merge")
    assert not sentinel.has_attention()


def test_maybe_set_attention_does_nothing_on_exit_99(isolated_cache):
    from gitbulk import sentinel

    _maybe_set_attention(EXIT_NOT_IMPLEMENTED, "report")
    assert not sentinel.has_attention()


# ─── ATTENTION supersession on clean run (node aklr5pq3 trigger 3) ─────────


def test_maybe_clear_superseded_clears_same_subcommand_on_exit_0(isolated_cache):
    from gitbulk import sentinel

    sentinel.set_attention(2, "report", "OLD-RID", "stale")
    _maybe_clear_superseded(EXIT_OK, "report")
    assert not sentinel.has_attention()


def test_maybe_clear_superseded_leaves_cross_subcommand(isolated_cache):
    from gitbulk import sentinel

    sentinel.set_attention(2, "dispatch", "DID", "agent failed")
    # A clean report run must not dismiss a dispatch failure sentinel.
    _maybe_clear_superseded(EXIT_OK, "report")
    assert sentinel.has_attention()


def test_maybe_clear_superseded_noop_on_nonzero_exit(isolated_cache):
    from gitbulk import sentinel

    sentinel.set_attention(2, "report", "RID", "stale")
    _maybe_clear_superseded(EXIT_STRUCTURAL_FAILURE, "report")
    assert sentinel.has_attention()


def test_maybe_clear_superseded_noop_for_non_attention_subcommand(isolated_cache):
    from gitbulk import sentinel

    # A sentinel can never name show/ack/invariants, but assert the guard
    # short-circuits before touching a (hypothetically) matching sentinel.
    sentinel.set_attention(2, "show", "RID", "impossible-but-defensive")
    _maybe_clear_superseded(EXIT_OK, "show")
    assert sentinel.has_attention()


def test_main_returns_exit_ok_when_no_subcommand_does_not_set_attention(isolated_cache):
    from gitbulk import sentinel

    out = io.StringIO()
    with redirect_stdout(out):
        rc = main([])
    assert rc == EXIT_OK
    assert not sentinel.has_attention()


# ─── Logging configuration ─────────────────────────────────────────────────


@pytest.fixture
def clean_gitbulk_logger():
    """Snapshot+restore the gitbulk logger config so each test runs clean."""
    import logging

    gl = logging.getLogger("gitbulk")
    saved_handlers = gl.handlers[:]
    saved_level = gl.level
    saved_propagate = gl.propagate
    for h in saved_handlers:
        gl.removeHandler(h)
    yield gl
    for h in gl.handlers[:]:
        gl.removeHandler(h)
    for h in saved_handlers:
        gl.addHandler(h)
    gl.setLevel(saved_level)
    gl.propagate = saved_propagate


def test_configure_logging_attaches_stderr_handler(clean_gitbulk_logger, monkeypatch):
    import logging
    monkeypatch.delenv("GITBULK_LOG_LEVEL", raising=False)
    _configure_logging()
    assert clean_gitbulk_logger.level == logging.INFO
    assert any(
        isinstance(h, logging.StreamHandler) and h.stream is sys.stderr
        for h in clean_gitbulk_logger.handlers
    )


def test_configure_logging_honors_env_var(clean_gitbulk_logger, monkeypatch):
    import logging
    monkeypatch.setenv("GITBULK_LOG_LEVEL", "debug")  # case-insensitive
    _configure_logging()
    assert clean_gitbulk_logger.level == logging.DEBUG


def test_configure_logging_invalid_env_falls_back_to_info(clean_gitbulk_logger, monkeypatch):
    import logging
    monkeypatch.setenv("GITBULK_LOG_LEVEL", "NOT_A_LEVEL")
    _configure_logging()
    assert clean_gitbulk_logger.level == logging.INFO


def test_configure_logging_idempotent(clean_gitbulk_logger, monkeypatch):
    """Calling twice must not stack handlers."""
    monkeypatch.delenv("GITBULK_LOG_LEVEL", raising=False)
    _configure_logging()
    _configure_logging()
    import logging
    stderr_handlers = [
        h for h in clean_gitbulk_logger.handlers
        if isinstance(h, logging.StreamHandler) and h.stream is sys.stderr
    ]
    assert len(stderr_handlers) == 1


def test_configure_logging_falls_back_when_env_is_other_truthy_non_level(
    clean_gitbulk_logger, monkeypatch
):
    """``getattr(logging, name)`` returns non-int things for many attrs;
    only int values are valid log levels."""
    import logging
    # 'getMessage' is a real attribute on logging (a method); not a level.
    monkeypatch.setenv("GITBULK_LOG_LEVEL", "getMessage")
    _configure_logging()
    assert clean_gitbulk_logger.level == logging.INFO


def test_python_version_check_rejects_old_interpreter(monkeypatch):
    monkeypatch.setattr(sys, "version_info", (3, 9, 0, "final", 0))
    err = io.StringIO()
    with pytest.raises(SystemExit) as exc, redirect_stderr(err):
        _check_python_version()
    assert exc.value.code == EXIT_STRUCTURAL_FAILURE
    assert "Python 3.10 or later" in err.getvalue()


def test_python_version_check_accepts_current_interpreter():
    _check_python_version()


def test_module_entrypoint_runs_main(monkeypatch):
    import runpy

    monkeypatch.setattr(sys, "argv", ["gitbulk", "--version"])
    out = io.StringIO()
    with pytest.raises(SystemExit) as exc, redirect_stdout(out):
        runpy.run_module("gitbulk", run_name="__main__", alter_sys=True)
    assert exc.value.code == EXIT_OK
    assert __version__ in out.getvalue()


def test_script_entrypoint_runs_main(monkeypatch):
    import runpy
    from pathlib import Path

    cli_path = Path(__file__).resolve().parent.parent / "src" / "gitbulk" / "cli.py"
    monkeypatch.setattr(sys, "argv", [str(cli_path), "--version"])
    out = io.StringIO()
    with pytest.raises(SystemExit) as exc, redirect_stdout(out):
        runpy.run_path(str(cli_path), run_name="__main__")
    assert exc.value.code == EXIT_OK
    assert __version__ in out.getvalue()


# ─── private umask (security-hawk F3 fix, 2026-05-28) ──────────────────────


def test_set_private_umask_makes_new_files_owner_only(tmp_path):
    """After _set_private_umask, newly created files have permissions
    that exclude group + other access (mode 0o600 / 0o700)."""
    import os

    prev_umask = os.umask(0o022)  # set a permissive baseline
    try:
        _set_private_umask()
        # Create a file under the new umask; check its mode.
        target = tmp_path / "private.txt"
        target.write_text("hello")
        mode = target.stat().st_mode & 0o777
        # Owner-only: group and other bits cleared.
        assert mode & 0o077 == 0, f"expected owner-only mode, got {oct(mode)}"
        # Owner read+write at minimum.
        assert mode & 0o600 == 0o600, f"expected owner rw, got {oct(mode)}"
    finally:
        os.umask(prev_umask)


def test_set_private_umask_returns_none():
    """Idempotent: the helper has no return value; safe to call repeatedly."""
    import os

    prev = os.umask(0o022)
    try:
        assert _set_private_umask() is None
        assert _set_private_umask() is None
    finally:
        os.umask(prev)


def test_config_error_prints_clean_message_no_stack_trace(
    monkeypatch, capsys, tmp_path
):
    """User onboarding: a ConfigError raised by a subcommand handler
    must not bubble as a Python stack trace. The CLI catches it,
    prints a one-liner to stderr, and exits EXIT_STRUCTURAL_FAILURE."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-such-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    # No repos.txt → load_repos raises ConfigError → CLI catches.
    rc = main(["report"])
    assert rc == EXIT_STRUCTURAL_FAILURE
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "repos.txt not found" in err
    assert err.startswith("gitbulk report:")


def test_config_error_colorized_under_force_color(monkeypatch, capsys, tmp_path):
    """End-to-end wiring proof: with FORCE_COLOR set, the top-level
    ConfigError handler paints its stderr message red via error_line."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-such-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    rc = main(["report"])
    assert rc == EXIT_STRUCTURAL_FAILURE
    err = capsys.readouterr().err
    assert "\033[31m" in err  # red
    assert "\033[0m" in err   # reset
    assert "repos.txt not found" in err


def test_no_color_overrides_force_color_end_to_end(monkeypatch, capsys, tmp_path):
    """NO_COLOR outranks FORCE_COLOR even on the error path, and the
    emitted line stays byte-clean (no escapes) for downstream parsers —
    the backward-compatibility guarantee, exercised through main()."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-such-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("NO_COLOR", "1")
    rc = main(["report"])
    assert rc == EXIT_STRUCTURAL_FAILURE
    err = capsys.readouterr().err
    assert "\033[" not in err
    assert err.startswith("gitbulk report:")
    assert "repos.txt not found" in err


def test_not_implemented_handler_returns_99():
    """No subcommand is a stub anymore, but _not_implemented remains as
    the fallback for any future KNOWN subcommand added without a handler.
    Exercise it directly so the safety net stays covered + documented."""
    import argparse as _argparse
    from gitbulk.cli import _not_implemented

    handler = _not_implemented("future-cmd")
    err = io.StringIO()
    with redirect_stderr(err):
        rc = handler(_argparse.Namespace())
    assert rc == EXIT_NOT_IMPLEMENTED
    assert "future-cmd" in err.getvalue()
    assert "not yet implemented" in err.getvalue()


# ─── lock-status reporter install (node rsclk7nq UX) ────────────────────────


def test_configure_lock_status_installs_on_tty(monkeypatch):
    import gitbulk.locks as L
    from gitbulk.cli import _configure_lock_status
    from gitbulk.util.lockstatus import TtyLockStatusReporter

    monkeypatch.setattr(L, "_status_reporter", None)
    monkeypatch.delenv("GITBULK_LOCK_STATUS", raising=False)
    monkeypatch.setattr("gitbulk.cli._stream_isatty", lambda s: True)
    _configure_lock_status()
    assert isinstance(L._status_reporter, TtyLockStatusReporter)


def test_configure_lock_status_disabled_by_env(monkeypatch):
    import gitbulk.locks as L
    from gitbulk.cli import _configure_lock_status

    monkeypatch.setattr(L, "_status_reporter", None)
    monkeypatch.setenv("GITBULK_LOCK_STATUS", "off")
    monkeypatch.setattr("gitbulk.cli._stream_isatty", lambda s: True)
    _configure_lock_status()
    assert L._status_reporter is None


def test_configure_lock_status_silent_on_non_tty(monkeypatch):
    import gitbulk.locks as L
    from gitbulk.cli import _configure_lock_status

    monkeypatch.setattr(L, "_status_reporter", None)
    monkeypatch.delenv("GITBULK_LOCK_STATUS", raising=False)
    monkeypatch.setattr("gitbulk.cli._stream_isatty", lambda s: False)
    _configure_lock_status()
    assert L._status_reporter is None
