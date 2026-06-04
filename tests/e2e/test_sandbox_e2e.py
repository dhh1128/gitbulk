"""End-to-end sandbox tests — the test that would have caught SEC-F1.

These run REAL ``git`` and REAL ``bwrap`` (no network: a local bare repo is the
"origin"). They prove that git actually works inside the bwrap bind set when the
agent workspace is a self-contained clone (agecln4k), and — as a regression
control — that the OLD linked-worktree approach does NOT (the bug F1 described).

Marked ``e2e`` and skipped when bubblewrap / unprivileged user namespaces are
unavailable (e.g. restricted CI runners), so they never block the hermetic
suite. They are excluded from the coverage gate; isolated_clone.py / sandbox.py
get their 100% branch coverage from the hermetic unit tests.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gitbulk.isolated_clone import create_isolated_clone
from gitbulk.sandbox import (
    SANDBOX_FS_NO_NET,
    SANDBOX_FS_ONLY,
    bwrap_available,
    wrap_argv,
)
from gitbulk.worktree import create_worktree

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not bwrap_available(),
        reason="bubblewrap unavailable / unprivileged userns disabled",
    ),
]


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _make_origin_and_clone(tmp_path: Path) -> tuple[Path, str]:
    """Create a local bare 'origin' with one commit on main + a feature branch,
    and a local operator clone of it. Returns (operator_clone, head_sha)."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                   capture_output=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-b", "main", cwd=seed)
    _git("config", "user.email", "t@t", cwd=seed)
    _git("config", "user.name", "t", cwd=seed)
    (seed / "README.md").write_text("hello\n")
    _git("add", "-A", cwd=seed)
    _git("commit", "-m", "init", cwd=seed)
    _git("remote", "add", "origin", str(bare), cwd=seed)
    _git("push", "origin", "main", cwd=seed)
    # A feature branch = the "PR head".
    _git("checkout", "-b", "feature/x", cwd=seed)
    (seed / "feature.txt").write_text("change\n")
    _git("add", "-A", cwd=seed)
    _git("commit", "-m", "feature", cwd=seed)
    _git("push", "origin", "feature/x", cwd=seed)
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(seed), check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    # Operator clone (this is what gitbulk would have under ~/code).
    operator = tmp_path / "operator"
    subprocess.run(["git", "clone", str(bare), str(operator)], check=True,
                   capture_output=True)
    return operator, head_sha


def _run_in_sandbox(workspace: Path, policy: str) -> subprocess.CompletedProcess:
    """Run ``git status`` inside the bwrap sandbox bound for ``workspace``."""
    argv = wrap_argv(
        ["git", "status", "--porcelain"], worktree=workspace, policy=policy
    )
    return subprocess.run(argv, capture_output=True, text=True)


@pytest.mark.parametrize("policy", [SANDBOX_FS_ONLY, SANDBOX_FS_NO_NET])
def test_git_works_in_sandbox_over_isolated_clone(tmp_path, policy):
    """The fix: an isolated clone is self-contained, so git runs inside the
    sandbox under both fs-only and fs+no-net."""
    operator, head_sha = _make_origin_and_clone(tmp_path)
    clone = create_isolated_clone(
        operator, "owner/repo", 1, "feature/x", head_sha,
        worktree_root=tmp_path / "ws", runid="e2e",
    )
    result = _run_in_sandbox(clone, policy)
    assert result.returncode == 0, (
        f"git failed inside sandbox ({policy}): {result.stderr}"
    )


def test_linked_worktree_is_broken_in_sandbox(tmp_path):
    """Regression control for SEC-F1: a linked worktree's .git points into the
    operator clone, which the sandbox does not bind — so git CANNOT run. This is
    the failure the isolated-clone fix exists to avoid."""
    operator, head_sha = _make_origin_and_clone(tmp_path)
    wt = create_worktree(
        operator, "owner/repo", 1, "feature/x", head_sha,
        worktree_root=tmp_path / "ws", runid="e2e",
    )
    result = _run_in_sandbox(wt, SANDBOX_FS_ONLY)
    assert result.returncode != 0
    assert "not a git repository" in result.stderr.lower()
