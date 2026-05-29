"""Self-installer (this.i node ``bootp4mq``).

``install_self`` copies the running binary into a PATH directory, marks it
executable, and reports whether that directory is on PATH (with a
shell-specific hint when not). All tests are offline and use tmp dirs.
"""

import stat
from pathlib import Path

import pytest

from gitbulk import install
from gitbulk.install import (
    DEFAULT_TARGET_NAME,
    InstallError,
    InstallResult,
    PathStatus,
    default_target_dir,
    detect_path_status,
    install_self,
    manual_instructions,
    print_manual_instructions,
    resolve_default_source,
    shell_hint,
)


def _make_source(tmp_path: Path) -> Path:
    src = tmp_path / "gitbulk-src"
    src.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    return src


# ── install_self ────────────────────────────────────────────────────────────


def test_install_copies_and_marks_executable_not_on_path(tmp_path):
    src = _make_source(tmp_path)
    target_dir = tmp_path / "bin"
    result = install_self(source=src, target_dir=target_dir, env_path="/usr/bin", shell="bash")
    assert isinstance(result, InstallResult)
    assert result.target == target_dir / DEFAULT_TARGET_NAME
    assert result.target.exists()
    assert result.target.stat().st_mode & stat.S_IXUSR
    assert result.path_status is PathStatus.NOT_ON_PATH
    assert "~/.bashrc" in result.hint


def test_install_reports_on_path_with_no_hint(tmp_path):
    src = _make_source(tmp_path)
    target_dir = tmp_path / "bin"
    target_dir.mkdir()
    result = install_self(source=src, target_dir=target_dir, env_path=str(target_dir), shell="bash")
    assert result.path_status is PathStatus.ON_PATH
    assert result.hint is None


def test_install_missing_source_raises(tmp_path):
    with pytest.raises(InstallError, match="source binary not found"):
        install_self(source=tmp_path / "nope", target_dir=tmp_path / "bin", env_path="")


def test_install_source_is_directory_raises(tmp_path):
    a_dir = tmp_path / "adir"
    a_dir.mkdir()
    with pytest.raises(InstallError, match="source binary not found"):
        install_self(source=a_dir, target_dir=tmp_path / "bin", env_path="")


def test_install_mkdir_failure_raises(tmp_path):
    src = _make_source(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    with pytest.raises(InstallError, match="could not create target directory"):
        install_self(source=src, target_dir=blocker / "sub", env_path="")


def test_install_copy_failure_raises(tmp_path, monkeypatch):
    src = _make_source(tmp_path)

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(install.shutil, "copyfile", _boom)
    with pytest.raises(InstallError, match="could not write"):
        install_self(source=src, target_dir=tmp_path / "bin", env_path="")


def test_install_chmod_failure_raises(tmp_path, monkeypatch):
    src = _make_source(tmp_path)

    def _boom(self, *_a, **_k):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "chmod", _boom)
    with pytest.raises(InstallError, match="could not mark"):
        install_self(source=src, target_dir=tmp_path / "bin", env_path="")


def test_install_noop_when_source_is_already_the_target(tmp_path):
    target_dir = tmp_path / "bin"
    target_dir.mkdir()
    target = target_dir / DEFAULT_TARGET_NAME
    target.write_text("already here", encoding="utf-8")
    result = install_self(source=target, target_dir=target_dir, env_path=str(target_dir))
    assert result.target.read_text() == "already here"
    assert result.target.stat().st_mode & stat.S_IXUSR


def test_install_defaults_env_path_and_shell_from_environment(tmp_path, monkeypatch):
    src = _make_source(tmp_path)
    target_dir = tmp_path / "bin"
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("SHELL", "/usr/bin/zsh")
    result = install_self(source=src, target_dir=target_dir)
    assert result.path_status is PathStatus.NOT_ON_PATH
    assert "~/.zshrc" in result.hint


# ── detect_path_status ────────────────────────────────────────────────────────


def test_detect_path_status_skips_empty_entries_and_unmatched(tmp_path):
    target_dir = tmp_path / "bin"
    target_dir.mkdir()
    env_path = f"::{tmp_path / 'other'}"
    assert detect_path_status(target_dir, env_path=env_path) is PathStatus.NOT_ON_PATH


def test_detect_path_status_continues_past_unresolvable_entry(tmp_path):
    """An ENAMETOOLONG entry raises OSError and is skipped; a later
    matching entry still yields ON_PATH."""
    target_dir = tmp_path / "bin"
    target_dir.mkdir()
    too_long = "/" + "z" * 5000
    env_path = f"{too_long}{__import__('os').pathsep}{target_dir}"
    assert detect_path_status(target_dir, env_path=env_path) is PathStatus.ON_PATH


def test_detect_path_status_handles_nonexistent_target_dir(tmp_path):
    target_dir = tmp_path / "does-not-exist"
    assert detect_path_status(target_dir, env_path=str(target_dir)) is PathStatus.ON_PATH


# ── shell_hint ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "shell, needle",
    [
        ("/bin/bash", "~/.bashrc"),
        ("zsh", "~/.zshrc"),
        ("/usr/bin/fish", "fish_add_path"),
        ("dash", "shell profile"),
        ("", "shell profile"),
    ],
)
def test_shell_hint_per_shell(shell, needle, tmp_path):
    assert needle in shell_hint(target_dir=tmp_path, shell=shell)


# ── resolve_default_source ────────────────────────────────────────────────────


def test_resolve_default_source_none_raises():
    with pytest.raises(InstallError, match="cannot determine source"):
        resolve_default_source(None)


def test_resolve_default_source_absolute_existing(tmp_path):
    src = _make_source(tmp_path)
    assert resolve_default_source(str(src)) == src.resolve()


def test_resolve_default_source_relative_via_which(tmp_path, monkeypatch):
    src = _make_source(tmp_path)
    monkeypatch.setattr(install.shutil, "which", lambda name: str(src))
    assert resolve_default_source("gitbulk") == src.resolve()


def test_resolve_default_source_relative_not_found_raises(monkeypatch):
    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    with pytest.raises(InstallError, match="source binary not found"):
        resolve_default_source("gitbulk-not-real")


# ── manual instructions ───────────────────────────────────────────────────────


def test_manual_instructions_returns_text():
    text = manual_instructions()
    assert "gitbulk" in text and "PATH" in text


def test_manual_instructions_missing_resource_raises(monkeypatch):
    def _boom(_pkg):
        raise ModuleNotFoundError("no package")

    monkeypatch.setattr(install.resources, "files", _boom)
    with pytest.raises(InstallError, match="manual install instructions resource missing"):
        manual_instructions()


def test_print_manual_instructions_success(capsys):
    import sys

    print_manual_instructions(sys.stderr)
    assert "gitbulk manual install" in capsys.readouterr().err


def test_print_manual_instructions_falls_back_on_error(capsys, monkeypatch):
    import sys

    def _boom():
        raise InstallError("gone")

    monkeypatch.setattr(install, "manual_instructions", _boom)
    print_manual_instructions(sys.stderr)
    err = capsys.readouterr().err
    assert "unavailable" in err and "github.com/dhh1128/gitbulk" in err


# ── default_target_dir ────────────────────────────────────────────────────────


def test_default_target_dir_is_local_bin():
    assert default_target_dir() == Path.home() / ".local" / "bin"
