"""Smoke tests for the CLI shell. Phase 0 only verifies the dispatcher
shape; real subcommand behavior is tested as it lands."""

import io
import sys
from contextlib import redirect_stderr, redirect_stdout

import pytest

from gitbulk import __version__
from gitbulk.cli import (
    EXIT_NOT_IMPLEMENTED,
    EXIT_OK,
    EXIT_STRUCTURAL_FAILURE,
    SUBCOMMANDS,
    _check_python_version,
    build_parser,
    main,
)


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


@pytest.mark.parametrize("name", [n for n, _ in SUBCOMMANDS])
def test_each_subcommand_is_stubbed_with_exit_99(name):
    err = io.StringIO()
    with redirect_stderr(err):
        rc = main([name])
    assert rc == EXIT_NOT_IMPLEMENTED
    assert "not yet implemented" in err.getvalue()
    assert name in err.getvalue()


def test_parser_constructs_without_error():
    parser = build_parser()
    assert parser.prog == "gitbulk"


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
