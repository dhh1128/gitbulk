"""Tests for gitbulk.isolated_clone (self-contained sandbox workspaces).

All git invocations are mocked (AGENTS.md 'no network in tests'); the real
bwrap/git round-trip is exercised separately in tests/e2e/. The single seam is
gitbulk.isolated_clone.subprocess.run.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from gitbulk import isolated_clone as ic
from gitbulk.isolated_clone import create_isolated_clone, remove_isolated_clone
from gitbulk.worktree import WorktreeError


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _git_url():
    return "git@github.com:owner/repo.git"


# ─── create_isolated_clone: happy path + argv sequence ─────────────────────


def test_create_builds_standalone_clone_sequence(tmp_path):
    repo_path = tmp_path / "clone"
    repo_path.mkdir()
    root = tmp_path / "wsroot"
    calls: list[tuple[list[str], str | None]] = []

    def fake_run(argv, cwd=None, capture_output=True, text=True, check=False):
        calls.append((list(argv), str(cwd) if cwd is not None else None))
        if argv[1] == "clone":
            Path(argv[-1], ".git").mkdir(parents=True, exist_ok=True)
            return _Completed()
        if argv[1:4] == ["remote", "get-url", "origin"]:
            return _Completed(stdout=_git_url() + "\n")
        return _Completed()

    with patch("gitbulk.isolated_clone.subprocess.run", side_effect=fake_run):
        result = create_isolated_clone(
            repo_path, "owner/repo", 42, "feature/x", "d" * 40,
            worktree_root=root, runid="run1",
        )

    expected = root / "run1" / "owner__repo" / "pr42-clone"
    assert result == expected
    seqs = [c[0][1:] for c in calls]  # drop the leading "git"
    # standalone clone (objects copied, no checkout), then origin reset, hooks
    # neutralized, head fetched + checked out detached.
    assert seqs[0] == [
        "clone", "--no-hardlinks", "--no-checkout", "--quiet",
        str(repo_path), str(expected),
    ]
    assert ["remote", "get-url", "origin"] in seqs
    assert ["remote", "set-url", "origin", _git_url()] in seqs
    assert any(s[:2] == ["config", "core.hooksPath"] for s in seqs)
    assert ["fetch", "origin", "feature/x"] in seqs
    assert seqs[-1] == ["checkout", "--detach", "--quiet", "d" * 40]
    # The hooks dir was actually created inside the clone's .git.
    assert (expected / ".git" / "gitbulk-no-hooks").is_dir()


def test_create_default_root_and_runid(tmp_path, monkeypatch):
    repo_path = tmp_path / "clone"
    repo_path.mkdir()
    root = tmp_path / "defroot"
    monkeypatch.setattr(ic.paths, "default_worktree_root", lambda: root)

    def fake_run(argv, cwd=None, **k):
        if argv[1] == "clone":
            Path(argv[-1], ".git").mkdir(parents=True, exist_ok=True)
        if argv[1:4] == ["remote", "get-url", "origin"]:
            return _Completed(stdout=_git_url())
        return _Completed()

    with patch("gitbulk.isolated_clone.subprocess.run", side_effect=fake_run):
        result = create_isolated_clone(
            repo_path, "owner/repo", 7, "br", "a" * 40
        )
    assert result == root / "adhoc" / "owner__repo" / "pr7-clone"


# ─── create_isolated_clone: guards / failures ──────────────────────────────


def test_create_rejects_escape_outside_root(tmp_path, monkeypatch):
    repo_path = tmp_path / "clone"
    repo_path.mkdir()
    root = tmp_path / "wsroot"
    monkeypatch.setattr(
        ic.paths, "worktree_dir",
        lambda runid, slug, root=None: tmp_path / "outside",
    )
    with patch("gitbulk.isolated_clone.subprocess.run", side_effect=AssertionError):
        with pytest.raises(WorktreeError, match="outside worktree_root"):
            create_isolated_clone(
                repo_path, "owner/repo", 1, "br", "a" * 40, worktree_root=root
            )


def test_create_refuses_main_clone_path(tmp_path, monkeypatch):
    root = tmp_path / "wsroot"
    repo_path = root / "owner__repo" / "pr9-clone"
    repo_path.mkdir(parents=True)
    monkeypatch.setattr(
        ic.paths, "worktree_dir",
        lambda runid, slug, root=None: repo_path.parent,
    )
    with patch("gitbulk.isolated_clone.subprocess.run", side_effect=AssertionError):
        with pytest.raises(WorktreeError, match="main clone path"):
            create_isolated_clone(
                repo_path, "owner/repo", 9, "br", "a" * 40, worktree_root=root
            )


def test_create_git_failure_raises(tmp_path):
    repo_path = tmp_path / "clone"
    repo_path.mkdir()
    root = tmp_path / "wsroot"
    with patch(
        "gitbulk.isolated_clone.subprocess.run",
        return_value=_Completed(returncode=128, stderr="clone failed"),
    ):
        with pytest.raises(WorktreeError, match="git clone .* failed"):
            create_isolated_clone(
                repo_path, "owner/repo", 1, "br", "a" * 40, worktree_root=root
            )


def test_create_missing_git_dir_after_clone(tmp_path):
    """git 'succeeded' but .git is absent → refuse to return the path."""
    repo_path = tmp_path / "clone"
    repo_path.mkdir()
    root = tmp_path / "wsroot"

    def fake_run(argv, cwd=None, **k):
        # Never create .git.
        if argv[1:4] == ["remote", "get-url", "origin"]:
            return _Completed(stdout=_git_url())
        return _Completed()

    with patch("gitbulk.isolated_clone.subprocess.run", side_effect=fake_run):
        with pytest.raises(WorktreeError, match="\\.git is missing"):
            create_isolated_clone(
                repo_path, "owner/repo", 1, "br", "a" * 40, worktree_root=root
            )


# ─── remove_isolated_clone ─────────────────────────────────────────────────


def test_remove_deletes_standalone_dir(tmp_path):
    root = tmp_path / "wsroot"
    clone = root / "run1" / "owner__repo" / "pr1-clone"
    clone.mkdir(parents=True)
    (clone / "file").write_text("x")
    remove_isolated_clone(clone, worktree_root=root)
    assert not clone.exists()


def test_remove_refuses_outside_root(tmp_path):
    root = tmp_path / "wsroot"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(WorktreeError, match="outside worktree_root"):
        remove_isolated_clone(outside, worktree_root=root)
    assert outside.exists()  # not deleted


def test_remove_oserror_wrapped(tmp_path):
    root = tmp_path / "wsroot"
    clone = root / "c"
    clone.mkdir(parents=True)
    with patch(
        "gitbulk.isolated_clone.shutil.rmtree", side_effect=OSError("busy")
    ):
        with pytest.raises(WorktreeError, match="failed to remove"):
            remove_isolated_clone(clone, worktree_root=root)
