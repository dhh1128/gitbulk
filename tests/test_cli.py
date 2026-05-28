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
    _maybe_set_attention,
    build_parser,
    main,
)

_IMPLEMENTED_SUBCOMMANDS = {"ack", "invariants", "report"}
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
