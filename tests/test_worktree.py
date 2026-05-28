"""Tests for :mod:`gitbulk.worktree`.

Per AGENTS.md "no network in tests" we mock :func:`subprocess.run` so
no real ``git`` invocation happens. The tests exercise:

  - Argv shape of ``git worktree add`` / ``git worktree remove`` /
    ``git status --porcelain``.
  - The path-verification gate that protects the local-git safety
    contract.
  - Parsing of the porcelain conflict codes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from gitbulk import worktree as worktree_mod
from gitbulk.worktree import (
    WorktreeError,
    create_worktree,
    is_worktree_in_conflict,
    remove_worktree,
)


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ─── create_worktree ───────────────────────────────────────────────────────


def test_create_worktree_builds_correct_argv(tmp_path: Path):
    repo_path = tmp_path / "clone"
    repo_path.mkdir()
    root = tmp_path / "wtroot"
    captured: dict = {}

    def fake_run(argv, capture_output, text, check):
        del capture_output, text, check
        captured["argv"] = list(argv)
        # Simulate git creating the target dir.
        target = Path(argv[-2])
        target.mkdir(parents=True, exist_ok=True)
        return _completed()

    with patch("gitbulk.worktree.subprocess.run", side_effect=fake_run):
        result = create_worktree(
            repo_path,
            "owner/repo",
            42,
            "feature-branch",
            "deadbeef" * 5,
            worktree_root=root,
            runid="20260528T120000Z",
        )

    expected = (
        root / "20260528T120000Z" / "owner__repo" / "pr42"
    )
    assert result == expected
    assert captured["argv"] == [
        "git",
        "-C",
        str(repo_path),
        "worktree",
        "add",
        "--detach",
        str(expected),
        "deadbeef" * 5,
    ]


def test_create_worktree_uses_default_root_when_omitted(
    tmp_path: Path, monkeypatch
):
    """When ``worktree_root`` is None we fall back to paths.default_worktree_root."""
    repo_path = tmp_path / "clone"
    repo_path.mkdir()
    root = tmp_path / "default-wtroot"
    monkeypatch.setattr(
        worktree_mod.paths, "default_worktree_root", lambda: root
    )

    def fake_run(argv, **kwargs):
        Path(argv[-2]).mkdir(parents=True, exist_ok=True)
        return _completed()

    with patch("gitbulk.worktree.subprocess.run", side_effect=fake_run):
        result = create_worktree(
            repo_path, "owner/repo", 7, "br", "abc1234", runid="r1"
        )
    assert result == root / "r1" / "owner__repo" / "pr7"


def test_create_worktree_default_runid_is_adhoc(tmp_path: Path):
    repo_path = tmp_path / "clone"
    repo_path.mkdir()
    root = tmp_path / "wtroot"

    def fake_run(argv, **kwargs):
        Path(argv[-2]).mkdir(parents=True, exist_ok=True)
        return _completed()

    with patch("gitbulk.worktree.subprocess.run", side_effect=fake_run):
        result = create_worktree(
            repo_path, "owner/repo", 7, "br", "abc1234", worktree_root=root
        )
    assert result == root / "adhoc" / "owner__repo" / "pr7"


def test_create_worktree_path_verification_rejects_escape(tmp_path, monkeypatch):
    """If paths.worktree_dir is somehow patched to return a path outside
    the configured root, create_worktree must refuse rather than ask git
    to write outside its sandbox."""
    repo_path = tmp_path / "clone"
    repo_path.mkdir()
    root = tmp_path / "wtroot"
    # Trick paths.worktree_dir to return an escape path.
    monkeypatch.setattr(
        worktree_mod.paths,
        "worktree_dir",
        lambda runid, slug, root=None: tmp_path / "outside",
    )

    with patch("gitbulk.worktree.subprocess.run") as run_mock:
        with pytest.raises(WorktreeError, match="outside worktree_root"):
            create_worktree(
                repo_path, "owner/repo", 1, "br", "sha", worktree_root=root
            )
        run_mock.assert_not_called()


def test_create_worktree_refuses_main_clone_path(tmp_path, monkeypatch):
    """If the computed target somehow equals the main clone, refuse.

    To exercise the equality branch we set up ``repo_path = root/clone/pr9``
    (so it's under ``root``) and patch ``paths.worktree_dir`` to return
    ``root/clone``. ``create_worktree`` then appends ``pr9`` (from
    ``pr_number=9``), making ``target == repo_path`` → ``is_relative_to``
    passes but the equality guard fires."""
    root = tmp_path / "wtroot"
    root.mkdir()
    clone_parent = root / "clone"
    clone_parent.mkdir()
    repo_path = clone_parent / "pr9"
    repo_path.mkdir()

    monkeypatch.setattr(
        worktree_mod.paths,
        "worktree_dir",
        lambda runid, slug, root=None: clone_parent,
    )

    with patch("gitbulk.worktree.subprocess.run") as run_mock:
        with pytest.raises(WorktreeError, match="main clone path"):
            create_worktree(
                repo_path,
                "owner/repo",
                9,
                "br",
                "sha",
                worktree_root=root,
                runid="r",
            )
        run_mock.assert_not_called()


def test_create_worktree_raises_when_target_missing_after_add(tmp_path):
    """If git worktree add 'succeeds' but the directory isn't there,
    refuse to return the path — otherwise the caller would write into
    a non-existent path or worse, the main clone."""
    repo_path = tmp_path / "clone"
    repo_path.mkdir()
    root = tmp_path / "wtroot"

    def fake_run(*args, **kwargs):
        # Pretend success without actually creating the target dir.
        return _completed()

    with patch("gitbulk.worktree.subprocess.run", side_effect=fake_run):
        with pytest.raises(WorktreeError, match="target does not exist"):
            create_worktree(
                repo_path, "o/r", 1, "br", "sha", worktree_root=root, runid="r"
            )


def test_create_worktree_propagates_git_failure(tmp_path):
    repo_path = tmp_path / "clone"
    repo_path.mkdir()
    root = tmp_path / "wtroot"

    def fake_run(*args, **kwargs):
        return _completed(stderr="fatal: bad sha\n", returncode=128)

    with patch("gitbulk.worktree.subprocess.run", side_effect=fake_run):
        with pytest.raises(WorktreeError, match="worktree add"):
            create_worktree(
                repo_path, "o/r", 1, "br", "sha", worktree_root=root, runid="r"
            )


# ─── remove_worktree ───────────────────────────────────────────────────────


def test_remove_worktree_builds_correct_argv(tmp_path):
    repo_path = tmp_path / "clone"
    repo_path.mkdir()
    wt = tmp_path / "some-wt"
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        return _completed()

    with patch("gitbulk.worktree.subprocess.run", side_effect=fake_run):
        remove_worktree(repo_path, wt)
    assert captured["argv"] == [
        "git",
        "-C",
        str(repo_path),
        "worktree",
        "remove",
        "--force",
        str(wt),
    ]


def test_remove_worktree_raises_on_git_failure(tmp_path):
    repo_path = tmp_path / "clone"
    repo_path.mkdir()
    wt = tmp_path / "some-wt"

    def fake_run(*args, **kwargs):
        return _completed(stderr="boom", returncode=1)

    with patch("gitbulk.worktree.subprocess.run", side_effect=fake_run):
        with pytest.raises(WorktreeError) as exc:
            remove_worktree(repo_path, wt)
    assert exc.value.stderr == "boom"
    assert exc.value.command is not None
    assert "worktree" in exc.value.command


# ─── is_worktree_in_conflict ───────────────────────────────────────────────


def test_is_worktree_in_conflict_detects_each_code(tmp_path):
    wt = tmp_path / "wt"
    for code in ("UU", "AA", "DD", "AU", "UA", "DU", "UD"):
        with patch(
            "gitbulk.worktree.subprocess.run",
            return_value=_completed(stdout=f"{code} path/to/file\n"),
        ):
            assert is_worktree_in_conflict(wt), f"missed code {code}"


def test_is_worktree_in_conflict_clean_returns_false(tmp_path):
    wt = tmp_path / "wt"
    with patch(
        "gitbulk.worktree.subprocess.run",
        return_value=_completed(stdout=" M file\n?? another\n"),
    ):
        assert is_worktree_in_conflict(wt) is False


def test_is_worktree_in_conflict_empty_porcelain_clean(tmp_path):
    wt = tmp_path / "wt"
    with patch(
        "gitbulk.worktree.subprocess.run",
        return_value=_completed(stdout=""),
    ):
        assert is_worktree_in_conflict(wt) is False


def test_is_worktree_in_conflict_short_line_ignored(tmp_path):
    """Defensive: a stray short line (e.g., trailing blank) must not
    crash and must not be misread as a conflict."""
    wt = tmp_path / "wt"
    with patch(
        "gitbulk.worktree.subprocess.run",
        return_value=_completed(stdout="X\n"),
    ):
        assert is_worktree_in_conflict(wt) is False


def test_is_worktree_in_conflict_git_failure_treated_as_conflict(tmp_path):
    """If git status itself errors (broken worktree), the caller must
    NOT silently rm the directory — we return True so the policy gate
    leaves it on disk for human inspection."""
    wt = tmp_path / "wt"
    with patch(
        "gitbulk.worktree.subprocess.run",
        return_value=_completed(stdout="", stderr="fatal", returncode=128),
    ):
        assert is_worktree_in_conflict(wt) is True
