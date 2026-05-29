"""Self-install the running ``gitbulk`` binary onto the user's PATH
(this.i node ``bootp4mq``).

``gitbulk install`` copies the currently-running executable into
``~/.local/bin`` (the XDG user-bin convention, and exactly what
``bin/gitbulk-cron`` searches), marks it executable, and prints a
shell-specific PATH hint if that directory is not already on ``PATH``.
This is the second half of the bootstrap one-liner
(``gh release download … && ./gitbulk install``). Ported from agentprep.
"""

from __future__ import annotations

import enum
import os
import shutil
import stat
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

DEFAULT_TARGET_NAME = "gitbulk"
MANUAL_RESOURCE_NAME = "manual-install-instructions.md"
_REPO_URL = "https://github.com/dhh1128/gitbulk"


class InstallError(Exception):
    """Raised when self-install cannot complete."""


class PathStatus(enum.Enum):
    ON_PATH = "on_path"
    NOT_ON_PATH = "not_on_path"


@dataclass(frozen=True)
class InstallResult:
    target: Path
    path_status: PathStatus
    hint: str | None


def _default_target_dir(home: Path) -> Path:
    return home / ".local" / "bin"


def default_target_dir() -> Path:
    return _default_target_dir(Path.home())


def detect_path_status(target_dir: Path, *, env_path: str) -> PathStatus:
    target_dir = target_dir.resolve() if target_dir.exists() else target_dir.absolute()
    for entry in (p for p in env_path.split(os.pathsep) if p):
        entry_path = Path(entry).expanduser()
        try:
            resolved = (
                entry_path.resolve() if entry_path.exists() else entry_path.absolute()
            )
        except OSError:
            continue
        if resolved == target_dir:
            return PathStatus.ON_PATH
    return PathStatus.NOT_ON_PATH


def shell_hint(*, target_dir: Path, shell: str) -> str:
    dir_str = str(target_dir)
    shell = (shell or "").rsplit("/", 1)[-1].lower()
    if shell == "bash":
        return f'Add to ~/.bashrc: export PATH="{dir_str}:$PATH"'
    if shell == "zsh":
        return f'Add to ~/.zshrc: export PATH="{dir_str}:$PATH"'
    if shell == "fish":
        return f"Run: fish_add_path {dir_str}"
    return f'Add to your shell profile: export PATH="{dir_str}:$PATH"'


def manual_instructions() -> str:
    """Return the bundled manual install instructions as text."""
    try:
        return (
            resources.files("gitbulk")
            .joinpath(MANUAL_RESOURCE_NAME)
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise InstallError(f"manual install instructions resource missing: {exc}") from exc


def install_self(
    *,
    source: Path,
    target_dir: Path,
    env_path: str | None = None,
    shell: str | None = None,
) -> InstallResult:
    source = Path(source)
    if not source.exists() or not source.is_file():
        raise InstallError(f"source binary not found: {source}")

    target_dir = Path(target_dir)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InstallError(f"could not create target directory {target_dir}: {exc}") from exc

    target = target_dir / DEFAULT_TARGET_NAME
    intended = (
        (target_dir.resolve() / DEFAULT_TARGET_NAME) if target_dir.exists() else target
    )
    if source.resolve() != intended:
        try:
            shutil.copyfile(source, target)
        except OSError as exc:
            raise InstallError(f"could not write {target}: {exc}") from exc

    try:
        mode = target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        target.chmod(mode)
    except OSError as exc:
        raise InstallError(f"could not mark {target} executable: {exc}") from exc

    if env_path is None:
        env_path = os.environ.get("PATH", "")
    status = detect_path_status(target_dir, env_path=env_path)
    hint = None
    if status is PathStatus.NOT_ON_PATH:
        hint = shell_hint(target_dir=target_dir, shell=shell or os.environ.get("SHELL", ""))
    return InstallResult(target=target, path_status=status, hint=hint)


def resolve_default_source(argv0: str | None) -> Path:
    """Best guess at the currently running gitbulk binary to copy."""
    if not argv0:
        raise InstallError("cannot determine source binary from sys.argv[0]")
    candidate = Path(argv0)
    if not candidate.is_absolute():
        located = shutil.which(argv0)
        if located:
            candidate = Path(located)
    candidate = candidate.resolve() if candidate.exists() else candidate
    if not candidate.exists() or not candidate.is_file():
        raise InstallError(f"source binary not found: {candidate}")
    return candidate


def print_manual_instructions(stream=sys.stderr) -> None:
    try:
        stream.write(manual_instructions())
    except InstallError as exc:
        stream.write(f"gitbulk install failed and manual instructions are unavailable: {exc}\n")
        stream.write(f"See {_REPO_URL}#install for manual install steps.\n")
