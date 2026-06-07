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
from gitbulk.git import GIT
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
        GIT,
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
        GIT,
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


# ─── prune-worktrees helpers (node prnwt5nq) ───────────────────────────────

from gitbulk.worktree import (  # noqa: E402
    WorktreeEntry,
    branch_ahead_behind,
    branch_contained_in,
    branch_unpushed_commit_count,
    delete_branch_trusting_local_default,
    delete_merged_local_branch,
    list_worktrees,
    ref_last_update_age_days,
    remove_linked_worktree,
    worktree_change_summary,
    worktree_in_progress_op,
)


_PORCELAIN = """\
worktree /home/u/code/repo
HEAD aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
branch refs/heads/main

worktree /home/u/code/repo-feat
HEAD bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
branch refs/heads/feature/x
locked

worktree /home/u/code/repo-detached
HEAD cccccccccccccccccccccccccccccccccccccccc
detached
"""


def test_list_worktrees_parses_main_linked_detached(tmp_path: Path):
    with patch(
        "gitbulk.worktree.subprocess.run",
        side_effect=lambda *a, **k: _completed(stdout=_PORCELAIN),
    ) as mock_run:
        entries = list_worktrees(tmp_path / "clone")
    argv = mock_run.call_args[0][0]
    assert argv[-3:] == ["worktree", "list", "--porcelain"]
    assert [e.branch for e in entries] == ["main", "feature/x", None]
    assert [e.is_main for e in entries] == [True, False, False]
    assert entries[1].is_locked is True
    assert entries[2].is_detached is True
    assert entries[0].head_sha == "a" * 40


def test_list_worktrees_handles_bare_main():
    porcelain = "worktree /srv/bare\nbare\n"
    with patch(
        "gitbulk.worktree.subprocess.run",
        side_effect=lambda *a, **k: _completed(stdout=porcelain),
    ):
        entries = list_worktrees(Path("/srv/bare"))
    assert entries[0].is_bare is True
    assert entries[0].branch is None


def test_list_worktrees_raises_on_git_failure():
    with patch(
        "gitbulk.worktree.subprocess.run",
        side_effect=lambda *a, **k: _completed(returncode=1, stderr="boom"),
    ):
        with pytest.raises(WorktreeError):
            list_worktrees(Path("/x"))


def test_worktree_change_summary_clean():
    with patch(
        "gitbulk.worktree.subprocess.run",
        side_effect=lambda *a, **k: _completed(stdout=""),
    ):
        assert worktree_change_summary(Path("/wt")) == (False, False, False)


def test_worktree_change_summary_tracked_untracked_conflict():
    out = " M tracked.py\n?? new.txt\nUU merged.py\nX\n"
    with patch(
        "gitbulk.worktree.subprocess.run",
        side_effect=lambda *a, **k: _completed(stdout=out),
    ):
        tracked, untracked, conflicted = worktree_change_summary(Path("/wt"))
    assert tracked is True
    assert untracked is True
    assert conflicted is True


def test_worktree_change_summary_only_untracked():
    with patch(
        "gitbulk.worktree.subprocess.run",
        side_effect=lambda *a, **k: _completed(stdout="?? only.txt\n"),
    ):
        assert worktree_change_summary(Path("/wt")) == (False, True, False)


def test_worktree_change_summary_git_failure_is_all_dirty():
    with patch(
        "gitbulk.worktree.subprocess.run",
        side_effect=lambda *a, **k: _completed(returncode=128),
    ):
        assert worktree_change_summary(Path("/wt")) == (True, True, True)


def test_worktree_in_progress_op_detects_rebase(tmp_path: Path):
    marker = tmp_path / "rebase-merge"
    marker.mkdir()

    def fake_run(argv, **k):
        # rev-parse --git-path <rel> → echo an absolute path; rebase-merge
        # exists, the rest do not.
        rel = argv[-1]
        return _completed(stdout=str(tmp_path / rel))

    with patch("gitbulk.worktree.subprocess.run", side_effect=fake_run):
        assert worktree_in_progress_op(tmp_path) == "rebase"


def test_worktree_in_progress_op_none_when_clean(tmp_path: Path):
    def fake_run(argv, **k):
        rel = argv[-1]
        return _completed(stdout=str(tmp_path / rel))  # none exist

    with patch("gitbulk.worktree.subprocess.run", side_effect=fake_run):
        assert worktree_in_progress_op(tmp_path) is None


def test_worktree_in_progress_op_relative_path(tmp_path: Path):
    (tmp_path / "MERGE_HEAD").write_text("x")

    def fake_run(argv, **k):
        rel = argv[-1]
        # Return a RELATIVE path; function resolves against worktree_path.
        if rel == "MERGE_HEAD":
            return _completed(stdout="MERGE_HEAD")
        return _completed(stdout="does-not-exist")

    with patch("gitbulk.worktree.subprocess.run", side_effect=fake_run):
        assert worktree_in_progress_op(tmp_path) == "merge"


def test_worktree_in_progress_op_rev_parse_failure_fails_safe():
    def fake_run(argv, **k):
        return _completed(returncode=1)

    with patch("gitbulk.worktree.subprocess.run", side_effect=fake_run):
        # First probe (rebase-merge) fails → returns "rebase" (fail safe).
        assert worktree_in_progress_op(Path("/x")) == "rebase"


def test_worktree_in_progress_op_empty_candidate_continues(tmp_path: Path):
    (tmp_path / "MERGE_HEAD").write_text("x")

    def fake_run(argv, **k):
        rel = argv[-1]
        if rel == "MERGE_HEAD":
            return _completed(stdout="MERGE_HEAD")
        return _completed(stdout="")  # empty → continue

    with patch("gitbulk.worktree.subprocess.run", side_effect=fake_run):
        assert worktree_in_progress_op(tmp_path) == "merge"


def test_branch_unpushed_commit_count_zero():
    with patch(
        "gitbulk.worktree.subprocess.run",
        side_effect=lambda *a, **k: _completed(stdout="0\n"),
    ) as mock_run:
        assert branch_unpushed_commit_count(Path("/r"), "feat") == 0
    argv = mock_run.call_args[0][0]
    assert argv[-4:] == ["rev-list", "--count", "feat", "--not"] or (
        "rev-list" in argv and "--remotes" in argv
    )


def test_branch_unpushed_commit_count_nonzero():
    with patch(
        "gitbulk.worktree.subprocess.run",
        side_effect=lambda *a, **k: _completed(stdout="4\n"),
    ):
        assert branch_unpushed_commit_count(Path("/r"), "feat") == 4


def test_branch_unpushed_commit_count_bad_output_raises():
    with patch(
        "gitbulk.worktree.subprocess.run",
        side_effect=lambda *a, **k: _completed(stdout="weird"),
    ):
        with pytest.raises(WorktreeError):
            branch_unpushed_commit_count(Path("/r"), "feat")


def test_local_branch_upstreams_parses_and_strips():
    # tab-separated: short-name <TAB> upstream:remoteref. Empty remoteref =>
    # no upstream (None). Blank lines ignored.
    out = (
        "main\trefs/heads/main\n"
        "feature/foo\trefs/heads/dev\n"
        "orphan\t\n"
        "\n"
        "\trefs/heads/x\n"  # defensive: non-blank line with empty name -> skip
        # Defensive: a tracking-ref form (refs/remotes/<remote>/<branch>) is
        # NOT what :remoteref emits, but the parser strips it to the bare name
        # so a format regression can't break the protection guard. Branch names
        # with slashes survive the remote-prefix strip.
        "tracked\trefs/remotes/origin/feature/x\n"
        "weird\tsomething-else\n"
    )
    with patch(
        "gitbulk.worktree.subprocess.run",
        side_effect=lambda *a, **k: _completed(stdout=out),
    ) as mock_run:
        assert worktree_mod.local_branch_upstreams(Path("/r")) == [
            ("main", "main"),
            ("feature/foo", "dev"),
            ("orphan", None),
            ("tracked", "feature/x"),
            ("weird", "something-else"),
        ]
    argv = mock_run.call_args[0][0]
    assert "for-each-ref" in argv and "refs/heads" in argv
    assert "--format=%(refname:short)%09%(upstream:remoteref)" in argv


def test_local_branch_upstreams_raises_on_git_failure():
    with patch(
        "gitbulk.worktree.subprocess.run",
        side_effect=lambda *a, **k: _completed(returncode=1, stderr="boom"),
    ):
        with pytest.raises(WorktreeError):
            worktree_mod.local_branch_upstreams(Path("/r"))


def test_remove_linked_worktree_argv_and_prune(tmp_path: Path):
    calls = []

    def fake_run(argv, **k):
        calls.append(list(argv))
        return _completed()

    wt = tmp_path / "linked"
    with patch("gitbulk.worktree.subprocess.run", side_effect=fake_run):
        remove_linked_worktree(tmp_path / "clone", wt)
    assert calls[0][-3:] == ["worktree", "remove", str(wt)]
    assert "--force" not in calls[0]
    assert calls[1][-2:] == ["worktree", "prune"]


def test_remove_linked_worktree_refuses_main(tmp_path: Path):
    clone = tmp_path / "clone"
    clone.mkdir()
    with pytest.raises(WorktreeError, match="main worktree"):
        remove_linked_worktree(clone, clone)


def test_delete_merged_local_branch_success():
    with patch(
        "gitbulk.worktree.subprocess.run",
        side_effect=lambda *a, **k: _completed(returncode=0),
    ) as mock_run:
        assert delete_merged_local_branch(Path("/r"), "feat") is True
    argv = mock_run.call_args[0][0]
    assert argv[-3:] == ["branch", "-d", "feat"]


def test_delete_merged_local_branch_refused_returns_false():
    with patch(
        "gitbulk.worktree.subprocess.run",
        side_effect=lambda *a, **k: _completed(
            returncode=1, stderr="not fully merged"
        ),
    ):
        assert delete_merged_local_branch(Path("/r"), "feat") is False


def test_list_worktrees_skips_blank_blocks_and_unknown_keys():
    # Leading blank line (empty _flush), an unknown 'prunable' key (no elif
    # matches → falls through), and a trailing block.
    porcelain = (
        "\n"  # leading blank → _flush with empty cur
        "worktree /a\n"
        "HEAD " + "a" * 40 + "\n"
        "branch refs/heads/main\n"
        "prunable gitdir file points to non-existent location\n"
        "\n"
        "worktree /b\n"
        "HEAD " + "b" * 40 + "\n"
        "branch refs/heads/feat\n"
    )
    with patch(
        "gitbulk.worktree.subprocess.run",
        side_effect=lambda *a, **k: _completed(stdout=porcelain),
    ):
        entries = list_worktrees(Path("/x"))
    assert [e.branch for e in entries] == ["main", "feat"]
    assert entries[0].is_main is True


# ─── no-PR safe-path helpers (States 1 / 2a / 2b) ──────────────────────────

from datetime import datetime, timedelta, timezone  # noqa: E402

_T = datetime(2026, 6, 6, tzinfo=timezone.utc)


def test_branch_ahead_behind_parses_left_right():
    # `git rev-list --left-right --count <branch>...refs/heads/<base>` emits
    # "<ahead>\t<behind>" (left=branch, right=base).
    with patch(
        "gitbulk.worktree.subprocess.run",
        side_effect=lambda *a, **k: _completed(stdout="0\t3\n"),
    ) as mock_run:
        assert branch_ahead_behind(Path("/r"), "feat", "main") == (0, 3)
    argv = mock_run.call_args[0][0]
    assert argv[-4:] == [
        "rev-list", "--left-right", "--count", "feat...refs/heads/main",
    ]


def test_branch_ahead_behind_none_on_git_error():
    with patch(
        "gitbulk.worktree.subprocess.run",
        side_effect=lambda *a, **k: _completed(returncode=128, stderr="bad rev"),
    ):
        assert branch_ahead_behind(Path("/r"), "feat", "nope") is None


def test_branch_ahead_behind_none_on_unexpected_output():
    with patch(
        "gitbulk.worktree.subprocess.run",
        side_effect=lambda *a, **k: _completed(stdout="garbage"),
    ):
        assert branch_ahead_behind(Path("/r"), "feat", "main") is None


def test_branch_ahead_behind_none_on_two_nonint_tokens():
    # Two tokens but not integers → the int() parse fails (defensive).
    with patch(
        "gitbulk.worktree.subprocess.run",
        side_effect=lambda *a, **k: _completed(stdout="a\tb\n"),
    ):
        assert branch_ahead_behind(Path("/r"), "feat", "main") is None


def test_branch_contained_in_true_when_zero():
    with patch(
        "gitbulk.worktree.subprocess.run",
        side_effect=lambda *a, **k: _completed(stdout="0\n"),
    ) as mock_run:
        assert branch_contained_in(Path("/r"), "main", "feat") is True
    argv = mock_run.call_args[0][0]
    assert argv[-3:] == ["rev-list", "--count", "refs/heads/main..feat"]


def test_branch_contained_in_false_when_nonzero():
    with patch(
        "gitbulk.worktree.subprocess.run",
        side_effect=lambda *a, **k: _completed(stdout="2\n"),
    ):
        assert branch_contained_in(Path("/r"), "main", "feat") is False


def test_branch_contained_in_raises_on_git_failure():
    with patch(
        "gitbulk.worktree.subprocess.run",
        side_effect=lambda *a, **k: _completed(returncode=1, stderr="no base"),
    ):
        with pytest.raises(WorktreeError):
            branch_contained_in(Path("/r"), "missing", "feat")


def test_branch_contained_in_raises_on_nonnumeric_output():
    # rev-list "succeeds" but emits non-numeric output (defensive parse guard).
    with patch(
        "gitbulk.worktree.subprocess.run",
        side_effect=lambda *a, **k: _completed(stdout="weird\n"),
    ):
        with pytest.raises(WorktreeError, match="unexpected output"):
            branch_contained_in(Path("/r"), "main", "feat")


def test_ref_last_update_age_days_parses_reflog_unix():
    # `git log -g -1 --date=unix --format=%gd <ref>` renders as
    # "<ref>@{<unixtime>}"; age is measured from that local-update time, NOT the
    # tip commit's committer date.
    ts = int((_T - timedelta(days=5)).timestamp())
    with patch(
        "gitbulk.worktree.subprocess.run",
        side_effect=lambda *a, **k: _completed(stdout=f"feat@{{{ts}}}\n"),
    ) as mock_run:
        age = ref_last_update_age_days(Path("/r"), "feat", _T)
    assert age is not None and 4.9 < age < 5.1
    argv = mock_run.call_args[0][0]
    assert argv[-6:] == [
        "log", "-g", "-1", "--date=unix", "--format=%gd", "feat",
    ]


def test_ref_last_update_age_days_head_for_worktree():
    ts = int((_T - timedelta(days=2)).timestamp())
    with patch(
        "gitbulk.worktree.subprocess.run",
        side_effect=lambda *a, **k: _completed(stdout=f"HEAD@{{{ts}}}\n"),
    ) as mock_run:
        age = ref_last_update_age_days(Path("/wt"), "HEAD", _T)
    assert age is not None and 1.9 < age < 2.1
    assert mock_run.call_args[0][0][-1] == "HEAD"


def test_ref_last_update_age_days_none_on_git_error():
    # Absent ref → git exits nonzero.
    with patch(
        "gitbulk.worktree.subprocess.run",
        side_effect=lambda *a, **k: _completed(returncode=128, stderr="bad rev"),
    ):
        assert ref_last_update_age_days(Path("/r"), "gone", _T) is None


def test_ref_last_update_age_days_none_when_no_reflog():
    # A ref with reflog disabled/expired emits no @{...} selector (empty stdout).
    with patch(
        "gitbulk.worktree.subprocess.run",
        side_effect=lambda *a, **k: _completed(stdout="\n"),
    ):
        assert ref_last_update_age_days(Path("/r"), "feat", _T) is None


def test_delete_branch_trusting_local_default_force_deletes_when_contained():
    calls = []

    def fake_run(argv, capture_output, text, check):
        del capture_output, text, check
        calls.append(list(argv))
        # First call is the containment rev-list (return 0 = contained); the
        # second is the force delete.
        if "rev-list" in argv:
            return _completed(stdout="0\n")
        return _completed(returncode=0)

    with patch("gitbulk.worktree.subprocess.run", side_effect=fake_run):
        assert delete_branch_trusting_local_default(
            Path("/r"), "feat", "main"
        ) is True
    assert calls[-1][-3:] == ["branch", "-D", "feat"]


def test_delete_branch_trusting_local_default_keeps_when_not_contained():
    def fake_run(argv, capture_output, text, check):
        del capture_output, text, check
        assert "branch" not in argv, "must not delete when not contained"
        return _completed(stdout="2\n")  # rev-list: 2 commits not in base

    with patch("gitbulk.worktree.subprocess.run", side_effect=fake_run):
        assert delete_branch_trusting_local_default(
            Path("/r"), "feat", "main"
        ) is False
