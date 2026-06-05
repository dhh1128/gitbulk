"""End-to-end + unit tests for ``gitbulk prune-worktrees`` (node prnwt5nq).

The worktree git helpers are monkeypatched at the module boundary so no
real ``git`` runs; gh data goes through :class:`FakeGHClient`.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from gitbulk import paths, sentinel
from gitbulk.commands import prune_worktrees as pw
from gitbulk.commands.prune_worktrees import (
    EXIT_ATTENTION_NEEDED,
    EXIT_INVARIANT_SKIPPED,
    EXIT_OK,
    EXIT_OVERRIDES_APPLIED,
    EXIT_STRUCTURAL_FAILURE,
    _classify_worktree,
    prune_worktrees_handler,
)
from gitbulk.config.policy import Policy
from gitbulk.gh import FakeGHClient, GHError
from gitbulk.pr_info import ClosedPRRef, PRInfo
from gitbulk.worktree import WorktreeEntry, WorktreeError


# ─── fixtures ──────────────────────────────────────────────────────────────


# isolated_xdg, code_root, and fresh_org_cache live in tests/conftest.py
# (shared across the command tests). write_config stays local because it
# materializes REAL git repos with an origin remote (not empty dirs).
@pytest.fixture
def write_config(isolated_xdg, code_root):
    def _write(*, repos_slugs):
        cfg_dir = paths.config_dir()
        cfg_dir.mkdir(parents=True, exist_ok=True)
        policy_yaml = {
            "defaults": {"retain_runs": 5, "prune_min_age_days": 7},
            "humans": {"org": "provenant-dev", "cache_ttl_hours": 24},
        }
        (cfg_dir / "gitbulk.yaml").write_text(yaml.safe_dump(policy_yaml))
        (cfg_dir / "repos.txt").write_text("\n".join(repos_slugs) + "\n")
        # Materialize clones AS git repos so local.exists / local.remote_matches
        # pass. We give each an origin remote matching the slug.
        import subprocess
        for slug in repos_slugs:
            owner, name = slug.split("/", 1)
            clone = code_root / name
            clone.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "-q", str(clone)], check=True)
            subprocess.run(
                ["git", "-C", str(clone), "remote", "add", "origin",
                 f"git@github.com:{slug}.git"],
                check=True, capture_output=True,
            )
        return cfg_dir

    return _write


def _args(*, apply=False, code_root=None, skip_check=None,
          refresh_org_members=False, include_untracked=False, org=None,
          repo=None, base=None, mergeable_state=None, author=None, filter=None):
    return argparse.Namespace(
        subcommand="prune-worktrees", apply=apply,
        code_root=str(code_root) if code_root else None,
        skip_check=list(skip_check) if skip_check else None,
        refresh_org_members=refresh_org_members,
        include_untracked=include_untracked,
        org=org, repo=repo, base=base, mergeable_state=mergeable_state,
        author=author, filter=filter,
    )


def _install(monkeypatch, fake):
    monkeypatch.setattr(
        "gitbulk.commands.prune_worktrees.ProductionGHClient", lambda: fake
    )


def _entry(path, branch="feat", *, is_main=False, is_detached=False,
           is_locked=False, is_bare=False, sha="a" * 40):
    return WorktreeEntry(
        path=Path(path), head_sha=sha, branch=branch, is_main=is_main,
        is_detached=is_detached, is_locked=is_locked, is_bare=is_bare,
    )


def _closed(slug, number, *, head_ref, merged=True, days_ago=30):
    return ClosedPRRef(
        number=number, title="t", url="u", merged=merged, base_ref="main",
        head_ref=head_ref, head_sha="z" * 40, head_repo_slug=slug,
        closed_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )


def _open_pr(slug, number, head_ref):
    return PRInfo(
        slug=slug, number=number, title="o", url="u", author="dhh1128",
        base_ref="main", head_ref=head_ref, head_sha="f" * 40, state="OPEN",
        is_draft=False, mergeable_state="CLEAN",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc), last_pushed_at=None,
        labels=(), review_decision=None, checks_status=None,
    )


NOW = datetime(2026, 6, 3, tzinfo=timezone.utc)


@pytest.fixture
def clean_helpers(monkeypatch):
    """Patch the read-only worktree helpers to a clean/no-op baseline.
    Individual tests override specific ones."""
    monkeypatch.setattr(pw, "worktree_in_progress_op", lambda p: None)
    monkeypatch.setattr(pw, "worktree_change_summary", lambda p: (False, False, False))
    monkeypatch.setattr(pw, "branch_unpushed_commit_count", lambda r, b: 0)


# ─── _classify_worktree unit tests ─────────────────────────────────────────


def _classify(fake, wt, open_heads=frozenset(), include_untracked=False):
    return _classify_worktree(
        fake, Policy(), "o/r", Path("/clone"), wt, set(open_heads), NOW,
        include_untracked,
    )


def test_classify_skips_bare(clean_helpers):
    out = _classify(FakeGHClient(), _entry("/wt", is_bare=True))
    assert out["decision"] == "skip" and "bare" in out["reason"]


def test_classify_skips_locked(clean_helpers):
    out = _classify(FakeGHClient(), _entry("/wt", is_locked=True))
    assert out["decision"] == "skip" and "locked" in out["reason"]


def test_classify_skips_detached(clean_helpers):
    out = _classify(FakeGHClient(), _entry("/wt", branch=None, is_detached=True))
    assert out["decision"] == "skip" and "detached" in out["reason"]


def test_classify_skips_in_progress(monkeypatch, clean_helpers):
    monkeypatch.setattr(pw, "worktree_in_progress_op", lambda p: "rebase")
    out = _classify(FakeGHClient(), _entry("/wt"))
    assert out["decision"] == "skip" and "rebase in progress" in out["reason"]


def test_classify_skips_conflicted(monkeypatch, clean_helpers):
    monkeypatch.setattr(pw, "worktree_change_summary", lambda p: (True, False, True))
    out = _classify(FakeGHClient(), _entry("/wt"))
    assert out["decision"] == "skip" and "conflict" in out["reason"]


def test_classify_skips_tracked_dirty(monkeypatch, clean_helpers):
    monkeypatch.setattr(pw, "worktree_change_summary", lambda p: (True, False, False))
    out = _classify(FakeGHClient(), _entry("/wt"))
    assert out["decision"] == "skip" and "uncommitted" in out["reason"]


def test_classify_skips_untracked_by_default(monkeypatch, clean_helpers):
    monkeypatch.setattr(pw, "worktree_change_summary", lambda p: (False, True, False))
    out = _classify(FakeGHClient(), _entry("/wt"))
    assert out["decision"] == "skip" and "untracked" in out["reason"]


def test_classify_allows_untracked_with_flag(monkeypatch, clean_helpers):
    monkeypatch.setattr(pw, "worktree_change_summary", lambda p: (False, True, False))
    fake = FakeGHClient(closed_prs_for_head={
        ("o/r", "feat"): [_closed("o/r", 1, head_ref="feat")]
    })
    out = _classify(fake, _entry("/wt"), include_untracked=True)
    assert out["decision"] == "delete"


def test_classify_skips_open_head(clean_helpers):
    out = _classify(FakeGHClient(), _entry("/wt", branch="feat"), open_heads={"feat"})
    assert out["decision"] == "skip" and "head of an open PR" in out["reason"]


def test_classify_skips_closed_lookup_error(clean_helpers):
    fake = FakeGHClient(closed_prs_for_head={("o/r", "feat"): GHError("x")})
    out = _classify(fake, _entry("/wt"))
    assert out["decision"] == "skip" and "could not list closed" in out["reason"]


def test_classify_skips_no_upstream_pr(clean_helpers):
    fork = ClosedPRRef(
        number=1, title="t", url="u", merged=True, base_ref="main",
        head_ref="feat", head_sha="z" * 40, head_repo_slug="x/fork",
        closed_at=NOW - timedelta(days=30),
    )
    fake = FakeGHClient(closed_prs_for_head={("o/r", "feat"): [fork]})
    out = _classify(fake, _entry("/wt"))
    assert out["decision"] == "skip" and "no merged/closed PR" in out["reason"]


def test_classify_skips_grace(clean_helpers):
    fake = FakeGHClient(closed_prs_for_head={
        ("o/r", "feat"): [_closed("o/r", 1, head_ref="feat", days_ago=2)]
    })
    out = _classify(fake, _entry("/wt"))
    assert out["decision"] == "skip" and "grace period" in out["reason"]


def test_classify_skips_unpushed_commits(monkeypatch, clean_helpers):
    monkeypatch.setattr(pw, "branch_unpushed_commit_count", lambda r, b: 4)
    fake = FakeGHClient(closed_prs_for_head={
        ("o/r", "feat"): [_closed("o/r", 1, head_ref="feat")]
    })
    out = _classify(fake, _entry("/wt"))
    assert out["decision"] == "skip" and "unpushed" in out["reason"]


def test_classify_skips_when_unpushed_check_errors(monkeypatch, clean_helpers):
    def boom(r, b):
        raise WorktreeError("rev-list failed")
    monkeypatch.setattr(pw, "branch_unpushed_commit_count", boom)
    fake = FakeGHClient(closed_prs_for_head={
        ("o/r", "feat"): [_closed("o/r", 1, head_ref="feat")]
    })
    out = _classify(fake, _entry("/wt"))
    assert out["decision"] == "skip" and "could not verify commits" in out["reason"]


def test_classify_deletes_clean_merged(clean_helpers):
    fake = FakeGHClient(closed_prs_for_head={
        ("o/r", "feat"): [_closed("o/r", 1, head_ref="feat")]
    })
    out = _classify(fake, _entry("/wt"))
    assert out["decision"] == "delete" and out["pr_number"] == 1


# ─── handler tests ─────────────────────────────────────────────────────────


def test_dry_run_lists_candidate(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, clean_helpers,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    clone = code_root / "alpha"
    monkeypatch.setattr(pw, "list_worktrees", lambda r: [
        _entry(clone, branch="main", is_main=True),
        _entry(clone.parent / "alpha-feat", branch="feat"),
    ])
    removed = []
    monkeypatch.setattr(pw, "remove_linked_worktree", lambda r, p: removed.append(p))
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
        closed_prs_for_head={
            ("dhh1128/alpha", "feat"): [_closed("dhh1128/alpha", 5, head_ref="feat")]
        },
    )
    _install(monkeypatch, fake)
    rc = prune_worktrees_handler(_args(code_root=code_root))
    assert rc == EXIT_OK
    assert removed == []  # dry run
    summary = (paths.latest_run_symlink("prune-worktrees").resolve() / "summary.md").read_text()
    assert "DRY-RUN" in summary and "Would remove" in summary and "alpha-feat" in summary


def test_apply_removes_worktree_and_deletes_branch(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, clean_helpers,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    clone = code_root / "alpha"
    wt = clone.parent / "alpha-feat"
    monkeypatch.setattr(pw, "list_worktrees", lambda r: [
        _entry(clone, branch="main", is_main=True),
        _entry(wt, branch="feat"),
    ])
    removed, deleted = [], []
    monkeypatch.setattr(pw, "remove_linked_worktree", lambda r, p: removed.append(p))
    monkeypatch.setattr(
        pw, "delete_merged_local_branch",
        lambda r, b: (deleted.append(b) or True),
    )
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
        closed_prs_for_head={
            ("dhh1128/alpha", "feat"): [_closed("dhh1128/alpha", 5, head_ref="feat")]
        },
    )
    _install(monkeypatch, fake)
    rc = prune_worktrees_handler(_args(apply=True, code_root=code_root))
    assert rc == EXIT_OK
    assert removed == [wt]
    assert deleted == ["feat"]
    assert not sentinel.has_attention()
    assert "Removed" in (paths.latest_run_symlink("prune-worktrees").resolve() / "summary.md").read_text()


def test_apply_removal_failure_raises_attention(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, clean_helpers,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    clone = code_root / "alpha"
    wt = clone.parent / "alpha-feat"
    monkeypatch.setattr(pw, "list_worktrees", lambda r: [
        _entry(clone, branch="main", is_main=True),
        _entry(wt, branch="feat"),
    ])

    def boom(r, p):
        raise WorktreeError("worktree is dirty")
    monkeypatch.setattr(pw, "remove_linked_worktree", boom)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
        closed_prs_for_head={
            ("dhh1128/alpha", "feat"): [_closed("dhh1128/alpha", 5, head_ref="feat")]
        },
    )
    _install(monkeypatch, fake)
    rc = prune_worktrees_handler(_args(apply=True, code_root=code_root))
    assert rc == EXIT_ATTENTION_NEEDED
    assert sentinel.has_attention()
    assert "FAILED" in (paths.latest_run_symlink("prune-worktrees").resolve() / "summary.md").read_text()


def test_apply_keeps_unmerged_branch(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, clean_helpers,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    clone = code_root / "alpha"
    wt = clone.parent / "alpha-feat"
    monkeypatch.setattr(pw, "list_worktrees", lambda r: [
        _entry(clone, branch="main", is_main=True), _entry(wt, branch="feat"),
    ])
    monkeypatch.setattr(pw, "remove_linked_worktree", lambda r, p: None)
    # git branch -d refuses (not fully merged) → returns False.
    monkeypatch.setattr(pw, "delete_merged_local_branch", lambda r, b: False)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
        closed_prs_for_head={
            ("dhh1128/alpha", "feat"): [_closed("dhh1128/alpha", 5, head_ref="feat")]
        },
    )
    _install(monkeypatch, fake)
    rc = prune_worktrees_handler(_args(apply=True, code_root=code_root))
    assert rc == EXIT_OK
    summary = (paths.latest_run_symlink("prune-worktrees").resolve() / "summary.md").read_text()
    assert "branch kept" in summary


def test_scan_error_recorded(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, clean_helpers,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])

    def boom(r):
        raise WorktreeError("worktree list failed")
    monkeypatch.setattr(pw, "list_worktrees", boom)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    _install(monkeypatch, fake)
    rc = prune_worktrees_handler(_args(apply=True, code_root=code_root))
    assert rc == EXIT_OK
    assert "Errors" in (paths.latest_run_symlink("prune-worktrees").resolve() / "summary.md").read_text()


def test_open_head_worktree_kept(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, clean_helpers,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    clone = code_root / "alpha"
    monkeypatch.setattr(pw, "list_worktrees", lambda r: [
        _entry(clone, branch="main", is_main=True),
        _entry(clone.parent / "alpha-active", branch="active"),
    ])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [_open_pr("dhh1128/alpha", 9, "active")]},
    )
    _install(monkeypatch, fake)
    rc = prune_worktrees_handler(_args(apply=True, code_root=code_root))
    assert rc == EXIT_OK
    assert fake.call_count["closed_prs_for_head"] == 0  # open-head short-circuit


def test_org_refresh_failure_aborts(monkeypatch, isolated_xdg, code_root, write_config):
    write_config(repos_slugs=["dhh1128/alpha"])
    fake = FakeGHClient(user={"login": "dhh1128"})
    _install(monkeypatch, fake)
    rc = prune_worktrees_handler(_args(code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE


def test_skip_check_exit_4(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, clean_helpers,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    clone = code_root / "alpha"
    monkeypatch.setattr(pw, "list_worktrees", lambda r: [
        _entry(clone, branch="main", is_main=True),
    ])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    _install(monkeypatch, fake)
    rc = prune_worktrees_handler(
        _args(apply=True, code_root=code_root, skip_check=["github.not_archived"])
    )
    assert rc == EXIT_OVERRIDES_APPLIED


def test_lock_timeout(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])  # warm org → no org_lock
    from gitbulk.locks import LockTimeoutError

    class _BoomLock:
        def __enter__(self):
            raise LockTimeoutError("busy", holder=None)

        def __exit__(self, *a):
            return False

    fake = FakeGHClient(
        user={"login": "dhh1128"}, default_branches={"dhh1128/alpha": "main"}
    )
    _install(monkeypatch, fake)
    # default_branches_lock (resource #3) times out — reached in prime, before
    # the clone is touched (node rsclk7nq); surfaced as exit 1.
    monkeypatch.setattr(
        "gitbulk.default_branch_cache.default_branches_lock",
        lambda *a, **k: _BoomLock(),
    )
    rc = prune_worktrees_handler(_args(code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE


# ─── structural / skip branches via target-aware fake run_chain ────────────

from types import SimpleNamespace  # noqa: E402


def _fake_run_chain(*, fail=(), skip=()):
    def fake(chain, ctx, *, skip_set, target):
        if target in fail:
            return SimpleNamespace(passed=False, fail_reason="boom", skips=[])
        if target in skip:
            return SimpleNamespace(
                passed=True, fail_reason=None,
                skips=[("local.exists", "no local clone")],
            )
        return SimpleNamespace(passed=True, fail_reason=None, skips=[])
    return fake


def _base_fake():
    return FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )


def _summary():
    return (paths.latest_run_symlink("prune-worktrees").resolve() / "summary.md").read_text()


def test_universal_failure_exits_structural(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    _install(monkeypatch, _base_fake())
    monkeypatch.setattr(pw, "run_chain", _fake_run_chain(fail={"global"}))
    assert prune_worktrees_handler(_args(code_root=code_root)) == EXIT_STRUCTURAL_FAILURE


def test_per_repo_failure_exits_structural(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    _install(monkeypatch, _base_fake())
    monkeypatch.setattr(pw, "run_chain", _fake_run_chain(fail={"dhh1128/alpha"}))
    assert prune_worktrees_handler(_args(code_root=code_root)) == EXIT_STRUCTURAL_FAILURE


def test_skipped_repo_dry_run_exits_3_with_filter(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    _install(monkeypatch, _base_fake())
    monkeypatch.setattr(pw, "run_chain", _fake_run_chain(skip={"dhh1128/alpha"}))
    rc = prune_worktrees_handler(_args(code_root=code_root, org=["dhh1128"]))
    assert rc == EXIT_INVARIANT_SKIPPED
    assert "Skipped repos" in _summary()


def test_skipped_repo_apply_exits_3(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    _install(monkeypatch, _base_fake())
    monkeypatch.setattr(pw, "run_chain", _fake_run_chain(skip={"dhh1128/alpha"}))
    assert prune_worktrees_handler(_args(apply=True, code_root=code_root)) == EXIT_INVARIANT_SKIPPED


def test_skipped_repos_txt_entry_in_summary(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, clean_helpers,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    repos_txt = paths.repos_file()
    repos_txt.write_text(repos_txt.read_text() + "definitely not a slug\n")
    monkeypatch.setattr(pw, "list_worktrees", lambda r: [])
    _install(monkeypatch, _base_fake())
    monkeypatch.setattr(pw, "run_chain", _fake_run_chain())
    rc = prune_worktrees_handler(_args(code_root=code_root))
    assert rc == EXIT_INVARIANT_SKIPPED
    assert "Skipped repos.txt entries" in _summary()


def test_no_linked_worktrees_message(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, clean_helpers,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    clone = code_root / "alpha"
    monkeypatch.setattr(pw, "list_worktrees", lambda r: [
        _entry(clone, branch="main", is_main=True),
    ])
    _install(monkeypatch, _base_fake())
    rc = prune_worktrees_handler(_args(code_root=code_root))
    assert rc == EXIT_OK
    assert "no linked worktrees matched" in _summary()


def test_runid_from_run_dir_fallback():
    assert pw._runid_from_run_dir(Path("20260603-prune-worktrees")) == "20260603"
    assert pw._runid_from_run_dir(Path("weird-name")) == "weird"


def test_cli_wrapper_delegates(monkeypatch):
    import gitbulk.cli as cli
    monkeypatch.setattr(
        "gitbulk.commands.prune_worktrees.prune_worktrees_handler", lambda args: 0
    )
    assert cli._prune_worktrees_handler(argparse.Namespace()) == 0


def test_utc_now_returns_aware():
    assert pw._utc_now().tzinfo is not None


def test_dry_run_skip_check_exits_4(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, clean_helpers,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    monkeypatch.setattr(pw, "list_worktrees", lambda r: [])
    _install(monkeypatch, _base_fake())
    monkeypatch.setattr(pw, "run_chain", _fake_run_chain())
    rc = prune_worktrees_handler(
        _args(code_root=code_root, skip_check=["github.not_archived"])
    )
    assert rc == EXIT_OVERRIDES_APPLIED
