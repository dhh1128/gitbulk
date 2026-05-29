"""CLI wiring for the self-management commands and the update notice
(this.i nodes ``dstbr5kq``, ``updnc5kr``, ``updtg6qn``).

The notice is TTY-gated, so it stays inert under pytest (captured stderr is
not a TTY) and never touches the network unless explicitly driven here with
an injected ``checker``.
"""

import io
import zipfile
from contextlib import redirect_stderr, redirect_stdout

import pytest

from gitbulk import update as update_mod
from gitbulk.cli import (
    EXIT_OK,
    EXIT_STRUCTURAL_FAILURE,
    _stream_isatty,
    build_parser,
    main,
    maybe_print_update_notice,
)


class _Status:
    def __init__(self, available, cur="1.0.0", latest="2.0.0"):
        self.update_available = available
        self.current_version = cur
        self.latest_version = latest


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = main(argv)
    return rc, out.getvalue(), err.getvalue()


# ── update notice ─────────────────────────────────────────────────────────────


def test_notice_prints_when_update_available_on_tty():
    err = io.StringIO()
    maybe_print_update_notice("report", isatty=True, stream=err, checker=lambda url: _Status(True))
    assert "newer version of gitbulk is available" in err.getvalue()
    assert "gitbulk update" in err.getvalue()


def test_notice_silent_when_current():
    err = io.StringIO()
    maybe_print_update_notice("report", isatty=True, stream=err, checker=lambda url: _Status(False))
    assert err.getvalue() == ""


def test_notice_silent_for_self_management_commands():
    err = io.StringIO()
    maybe_print_update_notice("update", isatty=True, stream=err, checker=lambda url: _Status(True))
    assert err.getvalue() == ""


def test_notice_silent_when_no_update_check_flag():
    err = io.StringIO()
    maybe_print_update_notice("report", no_update_check=True, isatty=True, stream=err, checker=lambda url: _Status(True))
    assert err.getvalue() == ""


def test_notice_silent_when_env_var_suppresses(monkeypatch):
    monkeypatch.setenv("GITBULK_NO_UPDATE_CHECK", "1")
    err = io.StringIO()
    maybe_print_update_notice("report", isatty=True, stream=err, checker=lambda url: _Status(True))
    assert err.getvalue() == ""


def test_notice_silent_when_not_a_tty():
    err = io.StringIO()
    maybe_print_update_notice("report", isatty=False, stream=err, checker=lambda url: _Status(True))
    assert err.getvalue() == ""


def test_notice_swallows_check_failure():
    def _boom(url):
        raise RuntimeError("offline")

    err = io.StringIO()
    maybe_print_update_notice("report", isatty=True, stream=err, checker=_boom)
    assert err.getvalue() == ""


def test_notice_uses_stream_isatty_when_isatty_not_given():
    # A StringIO is not a TTY → no notice, no network.
    err = io.StringIO()
    maybe_print_update_notice("report", stream=err, checker=lambda url: _Status(True))
    assert err.getvalue() == ""


def test_notice_default_stream_is_stderr_and_inert_under_pytest():
    # No stream/isatty given: real stderr under pytest is not a TTY.
    maybe_print_update_notice("report", checker=lambda url: _Status(True))


# ── _stream_isatty ────────────────────────────────────────────────────────────


def test_stream_isatty_true():
    class _S:
        def isatty(self):
            return True

    assert _stream_isatty(_S()) is True


def test_stream_isatty_stringio_is_false():
    assert _stream_isatty(io.StringIO()) is False


def test_stream_isatty_missing_method():
    assert _stream_isatty(object()) is False


def test_stream_isatty_value_error():
    class _S:
        def isatty(self):
            raise ValueError("closed")

    assert _stream_isatty(_S()) is False


# ── install command ───────────────────────────────────────────────────────────


def test_install_command_success(tmp_path):
    src = tmp_path / "gitbulk-src"
    src.write_text("#!/usr/bin/env python3\n")
    target_dir = tmp_path / "bin"
    rc, out, _ = _run(["install", "--dir", str(target_dir), "--source", str(src)])
    assert rc == EXIT_OK
    assert "installed" in out
    assert (target_dir / "gitbulk").exists()


def test_install_command_reports_not_on_path(tmp_path):
    src = tmp_path / "gitbulk-src"
    src.write_text("#!/usr/bin/env python3\n")
    target_dir = tmp_path / "offpath"
    rc, out, _ = _run(["install", "--dir", str(target_dir), "--source", str(src)])
    assert rc == EXIT_OK
    assert "not on PATH" in out


def test_install_command_on_path_omits_note(tmp_path, monkeypatch):
    src = tmp_path / "gitbulk-src"
    src.write_text("#!/usr/bin/env python3\n")
    target_dir = tmp_path / "bin"
    target_dir.mkdir()
    monkeypatch.setenv("PATH", str(target_dir))
    rc, out, _ = _run(["install", "--dir", str(target_dir), "--source", str(src)])
    assert rc == EXIT_OK
    assert "installed" in out
    assert "not on PATH" not in out


def test_install_command_failure_prints_manual(tmp_path):
    rc, _, err = _run(["install", "--source", str(tmp_path / "nope"), "--dir", str(tmp_path / "bin")])
    assert rc == EXIT_STRUCTURAL_FAILURE
    assert "install failed" in err
    assert "manual install" in err


# ── bundle command ────────────────────────────────────────────────────────────


def test_bundle_command_builds_zipapp(tmp_path):
    out_path = tmp_path / "gitbulk"
    rc, out, _ = _run(["bundle", str(out_path)])
    assert rc == EXIT_OK
    assert "wrote" in out
    assert zipfile.is_zipfile(out_path)


# ── update command ────────────────────────────────────────────────────────────


def test_update_check_current(monkeypatch):
    monkeypatch.setattr(update_mod, "check_update", lambda mp: _Status(False, latest="1.0.0"))
    rc, out, _ = _run(["update", "--check"])
    assert rc == EXIT_OK
    assert "gitbulk is current" in out


def test_update_check_available(monkeypatch):
    monkeypatch.setattr(update_mod, "check_update", lambda mp: _Status(True))
    rc, out, _ = _run(["update", "--check"])
    assert rc == EXIT_OK
    assert "newer version of gitbulk is available" in out
    assert "Update with:" in out


def test_update_check_error(monkeypatch):
    def _boom(mp):
        raise update_mod.UpdateError("manifest unreadable")

    monkeypatch.setattr(update_mod, "check_update", _boom)
    rc, _, err = _run(["update", "--check"])
    assert rc == EXIT_STRUCTURAL_FAILURE
    assert "update check failed" in err


def test_update_apply_refuses_pip_install(monkeypatch, tmp_path):
    target = tmp_path / "gitbulk"
    target.write_text("pip shim")
    monkeypatch.setattr(update_mod, "running_as_zipapp", lambda t: False)
    rc, _, err = _run(["update", "--target", str(target)])
    assert rc == EXIT_STRUCTURAL_FAILURE
    assert "pip-installed" in err
    assert "pip install -U gitbulk" in err


def test_update_apply_success(monkeypatch, tmp_path):
    target = tmp_path / "gitbulk"
    target.write_text("old")
    monkeypatch.setattr(update_mod, "running_as_zipapp", lambda t: True)
    monkeypatch.setattr(update_mod, "apply_update", lambda **kw: _Status(True))
    rc, out, _ = _run(["update", "--target", str(target)])
    assert rc == EXIT_OK
    assert "updated" in out


def test_update_apply_already_current(monkeypatch, tmp_path):
    target = tmp_path / "gitbulk"
    target.write_text("cur")
    monkeypatch.setattr(update_mod, "running_as_zipapp", lambda t: True)
    monkeypatch.setattr(update_mod, "apply_update", lambda **kw: _Status(False, latest="1.0.0"))
    rc, out, _ = _run(["update", "--target", str(target)])
    assert rc == EXIT_OK
    assert "gitbulk is current" in out


def test_update_apply_error(monkeypatch, tmp_path):
    target = tmp_path / "gitbulk"
    target.write_text("old")
    monkeypatch.setattr(update_mod, "running_as_zipapp", lambda t: True)

    def _boom(**kw):
        raise update_mod.UpdateError("sha mismatch")

    monkeypatch.setattr(update_mod, "apply_update", _boom)
    rc, _, err = _run(["update", "--target", str(target)])
    assert rc == EXIT_STRUCTURAL_FAILURE
    assert "update failed" in err


# ── help surface ──────────────────────────────────────────────────────────────


def test_help_lists_install_and_update_but_hides_bundle():
    out = io.StringIO()
    with pytest.raises(SystemExit), redirect_stdout(out):
        main(["--help"])
    text = out.getvalue()
    assert "install" in text
    assert "update" in text
    # bundle is a release-time internal tool (help=SUPPRESS).
    assert "bundle" not in text
