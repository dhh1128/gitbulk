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
from gitbulk.config.policy import Defaults, Policy, RepoOverride
from gitbulk.gh import FakeGHClient, GHError
from gitbulk.pr_info import BranchRef, ClosedPRRef, PRInfo
from gitbulk.worktree import WorktreeEntry, WorktreeError


# ─── fixtures ──────────────────────────────────────────────────────────────


# isolated_xdg, code_root, and fresh_org_cache live in tests/conftest.py
# (shared across the command tests). write_config stays local because it
# materializes REAL git repos with an origin remote (not empty dirs).
@pytest.fixture
def write_config(isolated_xdg, code_root):
    def _write(*, repos_slugs, remote="match", defaults_extra=None):
        cfg_dir = paths.config_dir()
        cfg_dir.mkdir(parents=True, exist_ok=True)
        defaults = {"retain_runs": 5, "prune_min_age_days": 7}
        if defaults_extra:
            defaults.update(defaults_extra)
        policy_yaml = {
            "defaults": defaults,
            "humans": {"org": "provenant-dev", "cache_ttl_hours": 24},
        }
        (cfg_dir / "gitbulk.yaml").write_text(yaml.safe_dump(policy_yaml))
        (cfg_dir / "repos.txt").write_text("\n".join(repos_slugs) + "\n")
        # Materialize clones AS git repos so local.exists / local.remote_matches
        # pass. ``remote`` controls origin: "match" => git@github.com:<slug>
        # (the default, makes remote_matches pass); None => NO origin remote;
        # any other string => that literal URL (e.g. a wrong-slug remote).
        import subprocess
        for slug in repos_slugs:
            owner, name = slug.split("/", 1)
            clone = code_root / name
            clone.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "-q", str(clone)], check=True)
            if remote is not None:
                url = (
                    f"git@github.com:{slug}.git" if remote == "match" else remote
                )
                subprocess.run(
                    ["git", "-C", str(clone), "remote", "add", "origin", url],
                    check=True, capture_output=True,
                )
        return cfg_dir

    return _write


def _args(*, apply=False, code_root=None, skip_check=None,
          refresh_org_members=False, include_untracked=False, org=None,
          repo=None, base=None, mergeable_state=None, author=None, filter=None,
          concurrency=None, no_prune_local_branches=False,
          trust_local_default=False):
    return argparse.Namespace(
        subcommand="prune-worktrees", apply=apply,
        code_root=str(code_root) if code_root else None,
        skip_check=list(skip_check) if skip_check else None,
        refresh_org_members=refresh_org_members,
        include_untracked=include_untracked,
        org=org, repo=repo, base=base, mergeable_state=mergeable_state,
        author=author, filter=filter,
        concurrency=concurrency,
        no_prune_local_branches=no_prune_local_branches,
        trust_local_default=trust_local_default,
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


# The single reference "now" every test timestamp derives from. The handler
# computes its own "now" via ``pw._utc_now`` (pinned to this value by the
# autouse ``_pin_clock`` fixture below), and the ``_classify_worktree`` unit
# tests pass this same constant as their explicit ``now`` argument. Deriving
# the date helpers from it (rather than wall-clock ``datetime.now()``) makes
# a "``days_ago`` days old" PR genuinely that old relative to the clock the
# code uses, so grace-boundary tests exercise the real boundary (TST-F3).
NOW = datetime(2026, 6, 3, tzinfo=timezone.utc)

# The genuine, unpatched production clock, captured before any test pins it,
# so ``test_utc_now_returns_aware`` can assert on the real implementation.
_REAL_UTC_NOW = pw._utc_now


@pytest.fixture(autouse=True)
def _pin_clock(monkeypatch):
    """Pin the handler's clock to ``NOW`` so handler-path grace decisions are
    deterministic and consistent with the date helpers (TST-F3). Tests that
    need to control time monkeypatch ``pw._utc_now`` themselves afterwards;
    that later setattr wins over this autouse default."""
    monkeypatch.setattr(pw, "_utc_now", lambda: NOW)


def _closed(slug, number, *, head_ref, merged=True, days_ago=30):
    return ClosedPRRef(
        number=number, title="t", url="u", merged=merged, base_ref="main",
        head_ref=head_ref, head_sha="z" * 40, head_repo_slug=slug,
        closed_at=NOW - timedelta(days=days_ago),
    )


def _open_pr(slug, number, head_ref):
    return PRInfo(
        slug=slug, number=number, title="o", url="u", author="dhh1128",
        base_ref="main", head_ref=head_ref, head_sha="f" * 40, state="OPEN",
        is_draft=False, mergeable_state="CLEAN",
        created_at=NOW,
        updated_at=NOW, last_pushed_at=None,
        labels=(), review_decision=None, checks_status=None,
    )


@pytest.fixture
def clean_helpers(monkeypatch):
    """Patch the read-only worktree helpers to a clean/no-op baseline.
    Individual tests override specific ones.

    The no-PR-path helpers (State 1 / 2a / 2b) default to "can't establish"
    (``branch_ahead_behind`` → None, age helpers → None, ``branch_contained_in``
    → False) so a clean baseline run keeps a no-PR branch rather than inventing
    a deletion; the State 1/2a/2b tests override exactly what they exercise."""
    monkeypatch.setattr(pw, "worktree_in_progress_op", lambda p: None)
    monkeypatch.setattr(pw, "worktree_change_summary", lambda p: (False, False, False))
    monkeypatch.setattr(pw, "branch_unpushed_commit_count", lambda r, b: 0)
    monkeypatch.setattr(pw, "branch_ahead_behind", lambda r, b, base: None)
    monkeypatch.setattr(pw, "ref_last_update_age_days", lambda r, ref, now: None)
    monkeypatch.setattr(pw, "branch_contained_in", lambda r, base, b: False)


# ─── _classify_worktree unit tests ─────────────────────────────────────────


def _classify(fake, wt, open_heads=frozenset(), include_untracked=False,
              *, default_branch=None, trust_local_default=False):
    return _classify_worktree(
        fake, Policy(), "o/r", Path("/clone"), wt, set(open_heads), NOW,
        include_untracked, default_branch=default_branch,
        trust_local_default=trust_local_default,
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


def test_classify_skips_no_upstream_pr(monkeypatch, clean_helpers):
    # A fork-only closed PR is not an UPSTREAM PR, so the branch has no
    # qualifying PR. With unpushed commits (work only local) and none of the
    # no-PR safe paths applying, it keeps the historical skip.
    monkeypatch.setattr(pw, "branch_unpushed_commit_count", lambda r, b: 1)
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
    # PR-merged path proved unpushed==0, so apply may force-delete (prnfd8kq).
    assert out["all_commits_remote"] is True


# ─── no-PR safe paths: State 1 (empty worktree behind its local base) ───────
#
# These configure ``closed_prs_for_head`` to ``[]`` so the branch reaches the
# no-PR classifier (an UNconfigured key would raise GHError instead).


def _no_pr_fake(branch="feat"):
    return FakeGHClient(closed_prs_for_head={("o/r", branch): []})


def test_state1_empty_behind_stale_deletes(monkeypatch, clean_helpers):
    monkeypatch.setattr(pw, "branch_ahead_behind", lambda r, b, base: (0, 3))
    monkeypatch.setattr(pw, "ref_last_update_age_days", lambda r, ref, now: 30.0)
    out = _classify(_no_pr_fake(), _entry("/wt"), default_branch="main")
    assert out["decision"] == "delete"
    assert "empty worktree behind local 'main' by 3" in out["reason"]
    # State-1 does NOT prove unpushed==0, so it keeps ``git branch -d`` and is
    # NOT flagged for force-delete (prnfd8kq).
    assert "all_commits_remote" not in out


def test_state1_empty_not_behind_kept(monkeypatch, clean_helpers):
    # ahead==0 but behind==0: nothing unique AND base hasn't moved → maybe fresh.
    monkeypatch.setattr(pw, "branch_ahead_behind", lambda r, b, base: (0, 0))
    monkeypatch.setattr(pw, "ref_last_update_age_days", lambda r, ref, now: 99.0)
    out = _classify(_no_pr_fake(), _entry("/wt"), default_branch="main")
    assert out["decision"] == "skip" and "not behind local 'main'" in out["reason"]


def test_state1_empty_behind_but_fresh_kept(monkeypatch, clean_helpers):
    monkeypatch.setattr(pw, "branch_ahead_behind", lambda r, b, base: (0, 5))
    monkeypatch.setattr(pw, "ref_last_update_age_days", lambda r, ref, now: 2.0)
    out = _classify(_no_pr_fake(), _entry("/wt"), default_branch="main")
    assert out["decision"] == "skip"
    assert "untouched only 2d" in out["reason"] and "grace period" in out["reason"]


def test_state1_empty_behind_age_unknown_kept(monkeypatch, clean_helpers):
    monkeypatch.setattr(pw, "branch_ahead_behind", lambda r, b, base: (0, 5))
    monkeypatch.setattr(pw, "ref_last_update_age_days", lambda r, ref, now: None)
    out = _classify(_no_pr_fake(), _entry("/wt"), default_branch="main")
    assert out["decision"] == "skip" and "could not determine age" in out["reason"]


def test_state1_empty_does_not_fall_through_to_2a(monkeypatch, clean_helpers):
    # An empty worktree typically has unpushed==0, which WOULD satisfy State 2a.
    # The behind-guardrail must still win: an empty-but-not-behind tree is kept
    # even though every commit is on a remote and it is stale.
    monkeypatch.setattr(pw, "branch_ahead_behind", lambda r, b, base: (0, 0))
    monkeypatch.setattr(pw, "branch_unpushed_commit_count", lambda r, b: 0)
    monkeypatch.setattr(pw, "ref_last_update_age_days", lambda r, ref, now: 365.0)
    out = _classify(_no_pr_fake(), _entry("/wt"), default_branch="main")
    assert out["decision"] == "skip" and "not behind" in out["reason"]


def test_state1_skipped_when_no_local_base(monkeypatch, clean_helpers):
    # default_branch=None → no local base → State 1 cannot apply; with unpushed
    # commits and 2b off, it keeps the historical skip.
    monkeypatch.setattr(pw, "branch_unpushed_commit_count", lambda r, b: 1)
    out = _classify(_no_pr_fake(), _entry("/wt"), default_branch=None)
    assert out["decision"] == "skip" and "no merged/closed PR" in out["reason"]


# ─── no-PR safe paths: State 2a (every commit already on a remote) ──────────


def test_state2a_on_remote_stale_deletes(monkeypatch, clean_helpers):
    monkeypatch.setattr(pw, "branch_ahead_behind", lambda r, b, base: (2, 0))
    monkeypatch.setattr(pw, "branch_unpushed_commit_count", lambda r, b: 0)
    monkeypatch.setattr(pw, "ref_last_update_age_days", lambda r, ref, now: 30.0)
    out = _classify(_no_pr_fake(), _entry("/wt"), default_branch="main")
    assert out["decision"] == "delete"
    assert "every commit is already on a remote" in out["reason"]
    # A worktree passed the clean-tree gate, so the report says so (tick 3sp5).
    assert "the worktree is clean (no uncommitted tracked work)" in out["reason"]
    # State-2a also proved unpushed==0 → force-delete eligible (prnfd8kq).
    assert out["all_commits_remote"] is True


def test_state2a_fresh_kept(monkeypatch, clean_helpers):
    monkeypatch.setattr(pw, "branch_ahead_behind", lambda r, b, base: (2, 0))
    monkeypatch.setattr(pw, "branch_unpushed_commit_count", lambda r, b: 0)
    monkeypatch.setattr(pw, "ref_last_update_age_days", lambda r, ref, now: 1.0)
    out = _classify(_no_pr_fake(), _entry("/wt"), default_branch="main")
    assert out["decision"] == "skip" and "only 1d old" in out["reason"]


def test_state2a_age_unknown_kept(monkeypatch, clean_helpers):
    monkeypatch.setattr(pw, "branch_ahead_behind", lambda r, b, base: (2, 0))
    monkeypatch.setattr(pw, "branch_unpushed_commit_count", lambda r, b: 0)
    monkeypatch.setattr(pw, "ref_last_update_age_days", lambda r, ref, now: None)
    out = _classify(_no_pr_fake(), _entry("/wt"), default_branch="main")
    assert out["decision"] == "skip" and "could not determine age" in out["reason"]


def test_state2a_commit_check_error_kept(monkeypatch, clean_helpers):
    # No PR, not an empty worktree (ahead>0) → State 2a; the unpushed-commit
    # probe errors, so the branch is kept with a clear reason.
    monkeypatch.setattr(pw, "branch_ahead_behind", lambda r, b, base: (2, 0))

    def boom(r, b):
        raise WorktreeError("rev-list failed")
    monkeypatch.setattr(pw, "branch_unpushed_commit_count", boom)
    out = _classify(_no_pr_fake(), _entry("/wt"), default_branch="main")
    assert out["decision"] == "skip" and "could not verify commits" in out["reason"]


def test_state2a_free_branch_uses_ref_age(monkeypatch, clean_helpers):
    monkeypatch.setattr(pw, "branch_unpushed_commit_count", lambda r, b: 0)
    monkeypatch.setattr(pw, "ref_last_update_age_days", lambda r, ref, now: 40.0)
    out = pw._classify_local_branch(
        _no_pr_fake("stale"), Policy(), "o/r", Path("/clone"), "stale",
        set(), NOW, default_branch="main", protected_upstreams=frozenset(),
        upstream="stale",
    )
    assert out["decision"] == "delete"
    assert out["kind"] == "branch"
    assert "every commit is already on a remote" in out["reason"]
    # A free branch has no worktree, so the clean-tree clause is omitted (3sp5).
    assert "the worktree is clean" not in out["reason"]


# ─── no-PR safe paths: State 2b (merged into local default, opt-in) ─────────


def test_state2b_opt_in_deletes(monkeypatch, clean_helpers):
    monkeypatch.setattr(pw, "branch_ahead_behind", lambda r, b, base: (2, 0))
    monkeypatch.setattr(pw, "branch_unpushed_commit_count", lambda r, b: 1)
    monkeypatch.setattr(pw, "branch_contained_in", lambda r, base, b: True)
    out = _classify(
        _no_pr_fake(), _entry("/wt"), default_branch="main",
        trust_local_default=True,
    )
    assert out["decision"] == "delete"
    assert out["trust_local_default"] is True and out["local_default"] == "main"
    assert "merged into local default 'main'" in out["reason"]


def test_state2b_without_flag_kept(monkeypatch, clean_helpers):
    monkeypatch.setattr(pw, "branch_ahead_behind", lambda r, b, base: (2, 0))
    monkeypatch.setattr(pw, "branch_unpushed_commit_count", lambda r, b: 1)
    monkeypatch.setattr(pw, "branch_contained_in", lambda r, base, b: True)
    out = _classify(_no_pr_fake(), _entry("/wt"), default_branch="main")
    assert out["decision"] == "skip" and "no merged/closed PR" in out["reason"]
    assert "trust_local_default" not in out


def test_state2b_not_contained_kept(monkeypatch, clean_helpers):
    monkeypatch.setattr(pw, "branch_ahead_behind", lambda r, b, base: (2, 0))
    monkeypatch.setattr(pw, "branch_unpushed_commit_count", lambda r, b: 1)
    monkeypatch.setattr(pw, "branch_contained_in", lambda r, base, b: False)
    out = _classify(
        _no_pr_fake(), _entry("/wt"), default_branch="main",
        trust_local_default=True,
    )
    assert out["decision"] == "skip" and "no merged/closed PR" in out["reason"]


def test_state2b_containment_check_error_kept(monkeypatch, clean_helpers):
    monkeypatch.setattr(pw, "branch_ahead_behind", lambda r, b, base: (2, 0))
    monkeypatch.setattr(pw, "branch_unpushed_commit_count", lambda r, b: 1)

    def boom(r, base, b):
        raise WorktreeError("rev-list failed")
    monkeypatch.setattr(pw, "branch_contained_in", boom)
    out = _classify(
        _no_pr_fake(), _entry("/wt"), default_branch="main",
        trust_local_default=True,
    )
    assert out["decision"] == "skip"
    assert "could not verify local-default merge" in out["reason"]


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
        branches={"dhh1128/alpha": []},
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
    # A PR-merged candidate proves unpushed==0, so it is flagged
    # ``all_commits_remote`` and applied via the force-delete helper (prnfd8kq).
    monkeypatch.setattr(
        pw, "delete_branch_all_commits_remote",
        lambda r, b: (deleted.append(b) or True),
    )
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        branches={"dhh1128/alpha": []},
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
        branches={"dhh1128/alpha": []},
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
    # The apply-time re-check finds work that would be lost → helper returns
    # False, branch is kept (prnfd8kq).
    monkeypatch.setattr(pw, "delete_branch_all_commits_remote", lambda r, b: False)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        branches={"dhh1128/alpha": []},
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


def test_handler_grace_boundary_honored_through_injected_clock(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, clean_helpers,
):
    """End-to-end coverage of the handler's clock wiring (TST-F4).

    The grace gate in ``_classify_worktree`` is unit-tested with an explicit
    ``now`` argument, but the HANDLER's wiring of ``_utc_now()`` into that
    call (prune_worktrees.py: ``now = _utc_now()`` → passed to
    ``_classify_worktree``) had no end-to-end test. This drives the whole
    handler with a fixed injected clock ``T`` and two worktrees on the same
    repo whose closed PRs are anchored *relative to T* — one 6 days old
    (inside the 7-day grace window → kept) and one 8 days old (outside →
    pruned). The boundary decision is therefore driven entirely by the
    injected clock reaching the classifier.

    Regression guard: anchoring ``closed_at`` to ``T`` (not ``NOW``/wall
    clock) means BOTH the "kept" and "pruned" assertions only hold when the
    handler actually passes the injected ``T`` through. If the handler
    hardcoded a different ``now`` or stopped threading the clock to
    ``_classify_worktree`` (falling back to real wall-clock, which is well
    before ``T``), both PRs would read as "negative age" / within grace and
    the 8-day worktree would be (wrongly) kept — failing this test.
    """
    T = datetime(2026, 9, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(pw, "_utc_now", lambda: T)  # overrides autouse pin

    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    clone = code_root / "alpha"
    inside_wt = clone.parent / "alpha-inside"   # 6d old → kept (grace)
    outside_wt = clone.parent / "alpha-outside"  # 8d old → pruned
    monkeypatch.setattr(pw, "list_worktrees", lambda r: [
        _entry(clone, branch="main", is_main=True),
        _entry(inside_wt, branch="inside"),
        _entry(outside_wt, branch="outside"),
    ])
    removed = []
    monkeypatch.setattr(pw, "remove_linked_worktree", lambda r, p: removed.append(p))

    def _closed_at(slug, number, head_ref, days_before_T):
        return ClosedPRRef(
            number=number, title="t", url="u", merged=True, base_ref="main",
            head_ref=head_ref, head_sha="z" * 40, head_repo_slug=slug,
            closed_at=T - timedelta(days=days_before_T),
        )

    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        branches={"dhh1128/alpha": []},
        my_open_prs={"dhh1128/alpha": []},
        closed_prs_for_head={
            ("dhh1128/alpha", "inside"): [
                _closed_at("dhh1128/alpha", 5, "inside", days_before_T=6)
            ],
            ("dhh1128/alpha", "outside"): [
                _closed_at("dhh1128/alpha", 6, "outside", days_before_T=8)
            ],
        },
    )
    _install(monkeypatch, fake)
    rc = prune_worktrees_handler(_args(code_root=code_root))  # dry-run
    assert rc == EXIT_OK
    assert removed == []  # dry-run never removes

    summary = (
        paths.latest_run_symlink("prune-worktrees").resolve() / "summary.md"
    ).read_text()
    # The 8-day worktree crosses the boundary → "Would remove"; the 6-day one
    # stays inside grace → "Kept (guardrail)" citing the grace period.
    assert "## Would remove" in summary
    assert "alpha-outside" in summary
    assert "alpha-inside" not in summary.split("## Kept (guardrail)")[0]
    kept_section = summary.split("## Kept (guardrail)")[1]
    assert "alpha-inside" in kept_section
    assert "grace period" in kept_section


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
        branches={"dhh1128/alpha": []},
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
        branches={"dhh1128/alpha": []},
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
        branches={"dhh1128/alpha": []},
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
        branches={"dhh1128/alpha": []},
        my_open_prs={"dhh1128/alpha": []},
    )


def _summary():
    return (paths.latest_run_symlink("prune-worktrees").resolve() / "summary.md").read_text()


def _recorded_actions():
    """The ``context.action`` values recorded to the run's errors.log (the
    apply-mode audit trail), in order."""
    import json
    log = paths.latest_run_symlink("prune-worktrees").resolve() / "errors.log"
    if not log.exists():
        return []
    out = []
    for line in log.read_text().splitlines():
        if not line.strip():
            continue
        ctx = json.loads(line).get("context", {})
        if "action" in ctx:
            out.append(ctx["action"])
    return out


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
    # Assert on the genuine implementation, not the pinned test clock.
    assert _REAL_UTC_NOW().tzinfo is not None


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


# ─── local-branch sweep + parallel-scan tests (nodes prnwlb7q, prnwpf9k) ────


def test_classify_local_branch_delete(clean_helpers):
    fake = FakeGHClient(closed_prs_for_head={
        ("o/r", "stale"): [_closed("o/r", 7, head_ref="stale")]
    })
    out = pw._classify_local_branch(
        fake, Policy(), "o/r", Path("/clone"), "stale", set(), NOW
    )
    assert out["decision"] == "delete"
    assert out["kind"] == "branch"
    assert out["path"] is None


def test_classify_local_branch_skip_open_head(clean_helpers):
    out = pw._classify_local_branch(
        FakeGHClient(), Policy(), "o/r", Path("/clone"), "stale", {"stale"}, NOW
    )
    assert out["decision"] == "skip" and "open PR" in out["reason"]


def test_delete_branch_for_routes_by_flag(monkeypatch):
    """``_delete_branch_for`` picks the safe mechanism per candidate flag
    (node prnfd8kq): State-2b → trusting-local-default force-delete;
    all-commits-remote → re-verified force-delete; otherwise ``git branch -d``.
    """
    calls = []
    monkeypatch.setattr(
        pw, "delete_branch_trusting_local_default",
        lambda r, b, d: (calls.append(("trust", b, d)) or True),
    )
    monkeypatch.setattr(
        pw, "delete_branch_all_commits_remote",
        lambda r, b: (calls.append(("remote", b)) or True),
    )
    monkeypatch.setattr(
        pw, "delete_merged_local_branch",
        lambda r, b: (calls.append(("merged", b)) or True),
    )
    clone = Path("/clone")
    pw._delete_branch_for(
        clone, {"branch": "b1", "trust_local_default": True, "local_default": "main"}
    )
    pw._delete_branch_for(clone, {"branch": "b2", "all_commits_remote": True})
    pw._delete_branch_for(clone, {"branch": "b3"})
    assert calls == [("trust", "b1", "main"), ("remote", "b2"), ("merged", "b3")]


def _sweep_fake(*, closed):
    return FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        branches={"dhh1128/alpha": []},
        my_open_prs={"dhh1128/alpha": []},
        closed_prs_for_head=closed,
    )


def test_local_branch_swept_dry_run(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
    clean_helpers, capsys,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    clone = code_root / "alpha"
    monkeypatch.setattr(pw, "list_worktrees", lambda r: [
        _entry(clone, branch="main", is_main=True),
    ])
    monkeypatch.setattr(pw, "local_branch_upstreams",
                        lambda r: [("main", "main"), ("stale", "stale")])
    fake = _sweep_fake(closed={
        ("dhh1128/alpha", "stale"): [_closed("dhh1128/alpha", 7, head_ref="stale")]
    })
    _install(monkeypatch, fake)
    rc = prune_worktrees_handler(_args(code_root=code_root))
    assert rc == EXIT_OK
    summary = _summary()
    assert "## Would remove" in summary
    assert "branch `stale`" in summary
    assert "0 worktrees + 1 local branches" in capsys.readouterr().out


def test_local_branch_kept_grace_dry_run(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, clean_helpers,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    clone = code_root / "alpha"
    monkeypatch.setattr(pw, "list_worktrees", lambda r: [
        _entry(clone, branch="main", is_main=True),
    ])
    monkeypatch.setattr(pw, "local_branch_upstreams",
                        lambda r: [("main", "main"), ("fresh", "fresh")])
    fake = _sweep_fake(closed={
        ("dhh1128/alpha", "fresh"): [
            _closed("dhh1128/alpha", 8, head_ref="fresh", days_ago=2)
        ]
    })
    _install(monkeypatch, fake)
    rc = prune_worktrees_handler(_args(code_root=code_root))
    assert rc == EXIT_OK
    summary = _summary()
    assert "## Kept (guardrail)" in summary
    kept = summary.split("## Kept (guardrail)")[1]
    assert "branch `fresh`" in kept and "grace period" in kept


def test_local_branch_swept_apply_deletes(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
    clean_helpers, capsys,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    clone = code_root / "alpha"
    monkeypatch.setattr(pw, "list_worktrees", lambda r: [
        _entry(clone, branch="main", is_main=True),
    ])
    monkeypatch.setattr(pw, "local_branch_upstreams",
                        lambda r: [("main", "main"), ("stale", "stale")])
    removed, deleted = [], []
    monkeypatch.setattr(pw, "remove_linked_worktree", lambda r, p: removed.append(p))
    # PR-merged candidate → all_commits_remote → force-delete helper (prnfd8kq).
    monkeypatch.setattr(
        pw, "delete_branch_all_commits_remote", lambda r, b: (deleted.append(b) or True)
    )
    fake = _sweep_fake(closed={
        ("dhh1128/alpha", "stale"): [_closed("dhh1128/alpha", 7, head_ref="stale")]
    })
    _install(monkeypatch, fake)
    rc = prune_worktrees_handler(_args(apply=True, code_root=code_root))
    assert rc == EXIT_OK
    assert removed == []          # no worktree candidate
    assert deleted == ["stale"]   # the standalone branch was deleted
    assert "deleted 1 of 1 local branches" in capsys.readouterr().out
    summary = _summary()
    assert "branch `stale`" in summary and "branch deleted" in summary
    assert _recorded_actions() == ["deleted-branch"]  # audit reflects reality


def test_local_branch_apply_kept_when_unmerged(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
    clean_helpers, capsys,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    clone = code_root / "alpha"
    monkeypatch.setattr(pw, "list_worktrees", lambda r: [
        _entry(clone, branch="main", is_main=True),
    ])
    monkeypatch.setattr(pw, "local_branch_upstreams",
                        lambda r: [("main", "main"), ("stale", "stale")])
    # The apply-time re-check declines (work would be lost) → returns False;
    # branch is kept and surfaced as "git refused" (prnfd8kq).
    monkeypatch.setattr(pw, "delete_branch_all_commits_remote", lambda r, b: False)
    fake = _sweep_fake(closed={
        ("dhh1128/alpha", "stale"): [_closed("dhh1128/alpha", 7, head_ref="stale")]
    })
    _install(monkeypatch, fake)
    rc = prune_worktrees_handler(_args(apply=True, code_root=code_root))
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    # The headline now surfaces the kept-difference instead of silently
    # dropping it (the 47-of-50 gap the user observed).
    assert "deleted 0 of 1 local branches (1 kept: git refused)" in out
    summary = _summary()
    assert "branch `stale`" in summary and "branch kept" in summary
    # Audit action must reflect the kept outcome, not a deletion (Copilot #17).
    assert _recorded_actions() == ["kept-branch"]


def test_no_prune_local_branches_opt_out(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, clean_helpers,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    clone = code_root / "alpha"
    monkeypatch.setattr(pw, "list_worktrees", lambda r: [
        _entry(clone, branch="main", is_main=True),
    ])
    # The helper IS still read (worktree branches need upstreams), but with the
    # sweep off the free branch "stale" must NOT become a candidate.
    monkeypatch.setattr(pw, "local_branch_upstreams",
                        lambda r: [("main", "main"), ("stale", "stale")])
    fake = _sweep_fake(closed={
        ("dhh1128/alpha", "stale"): [_closed("dhh1128/alpha", 7, head_ref="stale")]
    })
    _install(monkeypatch, fake)
    rc = prune_worktrees_handler(
        _args(code_root=code_root, no_prune_local_branches=True)
    )
    assert rc == EXIT_OK
    assert fake.call_count["closed_prs_for_head"] == 0  # "stale" never classified
    assert "stale" not in _summary()


def test_branch_with_worktree_not_double_swept(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
    clean_helpers, capsys,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    clone = code_root / "alpha"
    wt = clone.parent / "alpha-feat"
    monkeypatch.setattr(pw, "list_worktrees", lambda r: [
        _entry(clone, branch="main", is_main=True),
        _entry(wt, branch="feat"),
    ])
    # "feat" is both checked out in a worktree AND a local branch → it must be
    # classified ONCE (by the worktree pass), never also as a free branch.
    monkeypatch.setattr(pw, "local_branch_upstreams",
                        lambda r: [("main", "main"), ("feat", "feat")])
    monkeypatch.setattr(pw, "remove_linked_worktree", lambda r, p: None)
    # PR-merged candidate → all_commits_remote → force-delete helper (prnfd8kq).
    monkeypatch.setattr(pw, "delete_branch_all_commits_remote", lambda r, b: True)
    fake = _sweep_fake(closed={
        ("dhh1128/alpha", "feat"): [_closed("dhh1128/alpha", 5, head_ref="feat")]
    })
    _install(monkeypatch, fake)
    rc = prune_worktrees_handler(_args(apply=True, code_root=code_root))
    assert rc == EXIT_OK
    assert fake.call_count["closed_prs_for_head"] == 1  # not 2
    out = capsys.readouterr().out
    assert "removed 1 of 1 worktrees" in out
    assert "deleted 0 of 0 local branches" in out


def test_concurrency_one_sequential_scan(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, clean_helpers,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    clone = code_root / "alpha"
    monkeypatch.setattr(pw, "list_worktrees", lambda r: [
        _entry(clone, branch="main", is_main=True),
        _entry(clone.parent / "alpha-feat", branch="feat"),
    ])
    monkeypatch.setattr(pw, "local_branch_upstreams",
                        lambda r: [("main", "main"), ("feat", "feat")])
    fake = _sweep_fake(closed={
        ("dhh1128/alpha", "feat"): [_closed("dhh1128/alpha", 5, head_ref="feat")]
    })
    _install(monkeypatch, fake)
    rc = prune_worktrees_handler(_args(code_root=code_root, concurrency=1))
    assert rc == EXIT_OK
    assert "alpha-feat" in _summary()


def test_open_pr_fetch_failure_aborts_structural(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, clean_helpers,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    clone = code_root / "alpha"
    monkeypatch.setattr(pw, "list_worktrees", lambda r: [
        _entry(clone, branch="main", is_main=True),
    ])
    fake = _base_fake()

    def _boom(*a, **k):
        raise GHError("search rate-limited")
    monkeypatch.setattr(fake, "open_pr_heads", _boom)
    _install(monkeypatch, fake)
    rc = prune_worktrees_handler(_args(code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE
    assert "open-PR fetch failed" in _summary()


def test_open_pr_fetch_failure_logs_gh_command_and_points_at_errors(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, clean_helpers,
):
    """On a fetch 5xx, the failing gh command lands in errors.log and the
    summary points the operator at the raw logs + `--errors` (node 6bm7)."""
    import json

    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    clone = code_root / "alpha"
    monkeypatch.setattr(pw, "list_worktrees", lambda r: [
        _entry(clone, branch="main", is_main=True),
    ])
    fake = _base_fake()
    gh_cmd = ("gh", "api", "graphql", "-f", "query=...", "-F", "q=is:open is:pr")

    def _boom(*a, **k):
        raise GHError("gh exhausted 5 attempts: gh: HTTP 502", command=gh_cmd)
    monkeypatch.setattr(fake, "open_pr_heads", _boom)
    _install(monkeypatch, fake)

    rc = prune_worktrees_handler(_args(code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE

    # The failing gh invocation is captured in the structured log.
    run_dir = paths.latest_run_symlink("prune-worktrees").resolve()
    errors = [
        json.loads(line)
        for line in (run_dir / "errors.log").read_text().splitlines()
        if line.strip()
    ]
    fetch_err = next(e for e in errors if "open-PR fetch failed" in e["message"])
    assert fetch_err["context"]["gh_command"] == " ".join(gh_cmd)

    # The summary surfaces a transient-error hint + where to read raw logs.
    summary = _summary()
    assert "HTTP 5xx" in summary
    assert "gitbulk show prune-worktrees --errors" in summary
    assert str(run_dir) in summary


def test_open_pr_fetch_receives_progress_callback(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, clean_helpers,
):
    """The handler wires an on_progress callback into the batched fetch so the
    long fetch window is not silent (node 6bm7)."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    clone = code_root / "alpha"
    monkeypatch.setattr(pw, "list_worktrees", lambda r: [
        _entry(clone, branch="main", is_main=True),
    ])
    fake = _base_fake()
    captured = {}

    def _capture(slugs, *, timeout=None, on_progress=None):
        captured["on_progress"] = on_progress
        if on_progress is not None:
            on_progress(1, 1)  # exercise the callback wiring
        return {s: set() for s in slugs}
    monkeypatch.setattr(fake, "open_pr_heads", _capture)
    _install(monkeypatch, fake)

    rc = prune_worktrees_handler(_args(code_root=code_root))
    assert rc == EXIT_OK
    assert callable(captured["on_progress"])


# ─── remote-driven protection guard (node prnwlb7q) ────────────────────────


def _classify_lb(fake, branch, *, default_branch="main",
                 protected_upstreams=frozenset(), upstream=None, open_heads=(),
                 policy=None):
    return pw._classify_local_branch(
        fake, policy or Policy(), "o/r", Path("/clone"), branch,
        set(open_heads), NOW,
        default_branch=default_branch, protected_upstreams=protected_upstreams,
        upstream=upstream,
    )


def test_classify_skips_when_upstream_is_default(clean_helpers):
    # Name differs from the default ("mydev"), but it TRACKS the default
    # branch upstream → kept. Proves the decision is remote-driven, not by name.
    out = _classify_lb(FakeGHClient(), "mydev", default_branch="dev", upstream="dev")
    assert out["decision"] == "skip"
    assert "protected/default upstream 'dev'" in out["reason"]


def test_classify_skips_when_upstream_is_protected(clean_helpers):
    out = _classify_lb(
        FakeGHClient(), "release",
        protected_upstreams=frozenset({"release"}), upstream="release",
    )
    assert out["decision"] == "skip" and "protected/default upstream 'release'" in out["reason"]


def test_classify_refuses_when_protection_unknown(clean_helpers):
    out = _classify_lb(FakeGHClient(), "stale", protected_upstreams=None, upstream="stale")
    assert out["decision"] == "skip"
    assert "could not verify remote branch protection" in out["reason"]


def test_classify_deletes_when_upstream_not_protected(clean_helpers):
    fake = FakeGHClient(closed_prs_for_head={
        ("o/r", "stale"): [_closed("o/r", 7, head_ref="stale")]
    })
    out = _classify_lb(
        fake, "stale", default_branch="main",
        protected_upstreams=frozenset({"main"}), upstream="stale",
    )
    assert out["decision"] == "delete"


def test_classify_keeps_sacred_master_without_upstream(clean_helpers):
    # Real failure mode (codecraft.co): a legacy local `master` with NO upstream
    # configured. The remote-driven guard keys off the upstream and so never
    # fires; the name backstop must keep it. Default is `main` (master is not it).
    out = _classify_lb(
        FakeGHClient(), "master", default_branch="main", upstream=None,
    )
    assert out["decision"] == "skip"
    assert "never auto-pruned" in out["reason"]


def test_classify_keeps_sacred_main_tracking_nondefault_upstream(clean_helpers):
    # Real failure mode (kswg-cesr-specification): local `main` tracks
    # `origin/main`, but the repo's GitHub default branch is something else
    # (`revised-format`), so `main` is neither default nor protected. The
    # remote-driven guard would delete it; the sacred-name backstop keeps it.
    out = _classify_lb(
        FakeGHClient(), "main", default_branch="revised-format",
        protected_upstreams=frozenset({"revised-format"}), upstream="main",
    )
    assert out["decision"] == "skip"
    assert "never auto-pruned" in out["reason"]


def test_classify_keeps_actual_default_branch_without_upstream(clean_helpers):
    # Most dangerous real case: the branch IS the repo's GitHub default
    # (`revised-format`, an unconventional name) but has no upstream set, so the
    # remote-driven guard misses it. Protected by name == default_branch.
    out = _classify_lb(
        FakeGHClient(), "revised-format", default_branch="revised-format",
        upstream=None,
    )
    assert out["decision"] == "skip"
    assert "never auto-pruned" in out["reason"]


def test_sacred_backstop_runs_before_pr_lookup(clean_helpers):
    # The backstop short-circuits ahead of any closed-PR lookup, so even a
    # `main` with a merged PR past grace is never consulted/deleted.
    fake = FakeGHClient(closed_prs_for_head={
        ("o/r", "main"): [_closed("o/r", 7, head_ref="main")]
    })
    out = _classify_lb(fake, "main", default_branch="dev", upstream=None)
    assert out["decision"] == "skip"
    assert fake.call_count["closed_prs_for_head"] == 0


def test_classify_keeps_configured_sacred_branch(clean_helpers):
    # Operator-configured sacred name (defaults.sacred_branches) is unioned with
    # the built-in main/master and protected just like them — even with no
    # upstream and no PR.
    policy = Policy(defaults=Defaults(sacred_branches=("develop", "trunk")))
    out = _classify_lb(
        FakeGHClient(), "develop", default_branch="main", upstream=None,
        policy=policy,
    )
    assert out["decision"] == "skip"
    assert "never auto-pruned" in out["reason"]


def test_classify_configured_sacred_branch_per_repo_override(clean_helpers):
    # A per-repo override appends to the defaults' sacred set for that slug.
    policy = Policy(
        defaults=Defaults(sacred_branches=("develop",)),
        repos={"o/r": RepoOverride(sacred_branches=("release/prod",))},
    )
    out = _classify_lb(
        FakeGHClient(), "release/prod", default_branch="main", upstream=None,
        policy=policy,
    )
    assert out["decision"] == "skip" and "never auto-pruned" in out["reason"]


def test_classify_non_sacred_branch_unaffected_by_config(clean_helpers):
    # A branch NOT in the configured set is unaffected by the feature: the
    # sacred backstop does not fire, so it proceeds to the normal no-PR path.
    policy = Policy(defaults=Defaults(sacred_branches=("develop",)))
    out = _classify_lb(
        FakeGHClient(), "feature", default_branch="main", upstream=None,
        policy=policy,
    )
    assert "never auto-pruned" not in out["reason"]


def test_configured_sacred_branch_kept_end_to_end(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, clean_helpers,
):
    # End-to-end: a `develop` branch configured via defaults.sacred_branches is
    # kept by the handler even though it has no upstream and no PR — proving the
    # config flows from gitbulk.yaml through to the classifier.
    write_config(
        repos_slugs=["dhh1128/alpha"], defaults_extra={"sacred_branches": ["develop"]}
    )
    fresh_org_cache("provenant-dev", ["dhh1128"])
    clone = code_root / "alpha"
    monkeypatch.setattr(pw, "list_worktrees", lambda r: [
        _entry(clone, branch="main", is_main=True),  # clone checked out on main
    ])
    # `develop` has no upstream and is not the GitHub default (main).
    monkeypatch.setattr(pw, "local_branch_upstreams",
                        lambda r: [("main", "main"), ("develop", None)])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        branches={"dhh1128/alpha": []},
        my_open_prs={"dhh1128/alpha": []},
        closed_prs_for_head={
            ("dhh1128/alpha", "develop"): [_closed("dhh1128/alpha", 9, head_ref="develop")]
        },
    )
    _install(monkeypatch, fake)
    rc = prune_worktrees_handler(_args(code_root=code_root))
    assert rc == EXIT_OK
    # Sacred backstop fires before the PR lookup.
    assert fake.call_count["closed_prs_for_head"] == 0
    summary = _summary()
    assert "branch name 'develop' (never auto-pruned)" in summary
    assert "## Would remove" not in summary


def test_local_branch_kept_when_tracks_default_upstream(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, clean_helpers,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    clone = code_root / "alpha"
    monkeypatch.setattr(pw, "list_worktrees", lambda r: [
        _entry(clone, branch="feature", is_main=True),  # clone checked out elsewhere
    ])
    # "mydev" tracks the remote default (dev) though its local name differs.
    monkeypatch.setattr(pw, "local_branch_upstreams",
                        lambda r: [("feature", "feature"), ("mydev", "dev")])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "dev"},
        branches={"dhh1128/alpha": []},
        my_open_prs={"dhh1128/alpha": []},
        closed_prs_for_head={
            ("dhh1128/alpha", "mydev"): [_closed("dhh1128/alpha", 9, head_ref="mydev")]
        },
    )
    _install(monkeypatch, fake)
    rc = prune_worktrees_handler(_args(code_root=code_root))
    assert rc == EXIT_OK
    # Guard fires BEFORE the PR lookup, so closed_prs is never consulted.
    assert fake.call_count["closed_prs_for_head"] == 0
    summary = _summary()
    assert "tracks protected/default upstream 'dev'" in summary
    assert "## Would remove" not in summary


def test_local_branch_kept_when_tracks_protected_upstream(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, clean_helpers,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    clone = code_root / "alpha"
    monkeypatch.setattr(pw, "list_worktrees", lambda r: [
        _entry(clone, branch="feature", is_main=True),
    ])
    monkeypatch.setattr(pw, "local_branch_upstreams",
                        lambda r: [("feature", "feature"), ("release", "release")])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        branches={"dhh1128/alpha": [
            BranchRef(name="release", sha="r" * 40, protected=True),
        ]},
        my_open_prs={"dhh1128/alpha": []},
        closed_prs_for_head={
            ("dhh1128/alpha", "release"): [_closed("dhh1128/alpha", 9, head_ref="release")]
        },
    )
    _install(monkeypatch, fake)
    rc = prune_worktrees_handler(_args(code_root=code_root))
    assert rc == EXIT_OK
    assert fake.call_count["closed_prs_for_head"] == 0
    assert "tracks protected/default upstream 'release'" in _summary()


def test_protection_fetch_failure_refuses_candidate(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, clean_helpers,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    clone = code_root / "alpha"
    monkeypatch.setattr(pw, "list_worktrees", lambda r: [
        _entry(clone, branch="feature", is_main=True),
    ])
    monkeypatch.setattr(pw, "local_branch_upstreams",
                        lambda r: [("feature", "feature"), ("stale", "stale")])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        branches={"dhh1128/alpha": GHError("branches API down")},
        my_open_prs={"dhh1128/alpha": []},
        closed_prs_for_head={
            ("dhh1128/alpha", "stale"): [_closed("dhh1128/alpha", 9, head_ref="stale")]
        },
    )
    _install(monkeypatch, fake)
    rc = prune_worktrees_handler(_args(code_root=code_root))
    assert rc == EXIT_OK
    assert fake.call_count["closed_prs_for_head"] == 0
    summary = _summary()
    assert "could not verify remote branch protection" in summary
    assert "## Would remove" not in summary


# ─── remote-less / wrong-remote clones are out of scope (local.remote_matches)


def test_remoteless_clone_skipped_no_deletions(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, clean_helpers,
):
    """A clone with NO origin remote is skipped by local.remote_matches before
    the scan, so none of its branches are even classified — let alone deleted.
    Uses the REAL invariant chain (run_chain is not faked)."""
    write_config(repos_slugs=["dhh1128/alpha"], remote=None)
    fresh_org_cache("provenant-dev", ["dhh1128"])
    # Stand-in branches/worktrees that WOULD be swept if the repo were scanned.
    monkeypatch.setattr(pw, "local_branch_upstreams",
                        lambda r: [("feature", None), ("stale", "stale")])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        branches={"dhh1128/alpha": []},
        my_open_prs={"dhh1128/alpha": []},
        closed_prs_for_head={
            ("dhh1128/alpha", "stale"): [_closed("dhh1128/alpha", 9, head_ref="stale")]
        },
    )
    _install(monkeypatch, fake)
    rc = prune_worktrees_handler(_args(apply=True, code_root=code_root))
    assert rc == EXIT_INVARIANT_SKIPPED
    summary = _summary()
    assert "Skipped repos" in summary
    assert "origin remote not configured" in summary
    # Never scanned: no classification, no protection fetch.
    assert fake.call_count["closed_prs_for_head"] == 0
    assert fake.call_count["list_branches"] == 0


def test_wrong_remote_clone_skipped_no_deletions(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, clean_helpers,
):
    """A clone whose origin points at a DIFFERENT slug is likewise skipped."""
    write_config(
        repos_slugs=["dhh1128/alpha"], remote="git@github.com:someone/else.git"
    )
    fresh_org_cache("provenant-dev", ["dhh1128"])
    monkeypatch.setattr(pw, "local_branch_upstreams",
                        lambda r: [("stale", "stale")])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        branches={"dhh1128/alpha": []},
        my_open_prs={"dhh1128/alpha": []},
        closed_prs_for_head={
            ("dhh1128/alpha", "stale"): [_closed("dhh1128/alpha", 9, head_ref="stale")]
        },
    )
    _install(monkeypatch, fake)
    rc = prune_worktrees_handler(_args(apply=True, code_root=code_root))
    assert rc == EXIT_INVARIANT_SKIPPED
    summary = _summary()
    assert "Skipped repos" in summary
    assert "origin points at" in summary
    assert fake.call_count["closed_prs_for_head"] == 0
    assert fake.call_count["list_branches"] == 0


# ─── no-PR safe paths through the handler (State 1 / State 2b) ──────────────


def test_state1_empty_worktree_swept_dry_run(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, clean_helpers,
):
    """An empty worktree (no commits vs local default) that is behind that base
    and untouched past the grace period is a remove candidate even with NO PR."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    clone = code_root / "alpha"
    monkeypatch.setattr(pw, "list_worktrees", lambda r: [
        _entry(clone, branch="main", is_main=True),
        _entry(clone.parent / "alpha-empty", branch="empty"),
    ])
    # Empty vs local main, base moved 4 commits ahead, untouched 30 days.
    monkeypatch.setattr(pw, "branch_ahead_behind", lambda r, b, base: (0, 4))
    monkeypatch.setattr(pw, "ref_last_update_age_days", lambda r, ref, now: 30.0)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        branches={"dhh1128/alpha": []},
        my_open_prs={"dhh1128/alpha": []},
        closed_prs_for_head={("dhh1128/alpha", "empty"): []},
    )
    _install(monkeypatch, fake)
    rc = prune_worktrees_handler(_args(code_root=code_root))
    assert rc == EXIT_OK
    summary = _summary()
    assert "## Would remove" in summary
    assert "alpha-empty" in summary
    assert "empty worktree behind local 'main' by 4" in summary


def test_state1_empty_worktree_kept_without_flag_irrelevant(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, clean_helpers,
):
    """A fresh empty worktree (not behind its base) is kept — no State-1 reap."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    clone = code_root / "alpha"
    monkeypatch.setattr(pw, "list_worktrees", lambda r: [
        _entry(clone, branch="main", is_main=True),
        _entry(clone.parent / "alpha-fresh", branch="fresh"),
    ])
    monkeypatch.setattr(pw, "branch_ahead_behind", lambda r, b, base: (0, 0))
    monkeypatch.setattr(pw, "ref_last_update_age_days", lambda r, ref, now: 30.0)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        branches={"dhh1128/alpha": []},
        my_open_prs={"dhh1128/alpha": []},
        closed_prs_for_head={("dhh1128/alpha", "fresh"): []},
    )
    _install(monkeypatch, fake)
    rc = prune_worktrees_handler(_args(code_root=code_root))
    assert rc == EXIT_OK
    summary = _summary()
    assert "## Would remove" not in summary
    kept = summary.split("## Kept (guardrail)")[1]
    assert "alpha-fresh" in kept and "not behind local 'main'" in kept


def test_state2b_apply_force_deletes_with_flag(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
    clean_helpers, capsys,
):
    """--trust-local-default: a worktree branch merged only into local default
    (no PR, commits not on a remote) is removed and force-deleted."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    clone = code_root / "alpha"
    wt = clone.parent / "alpha-local"
    monkeypatch.setattr(pw, "list_worktrees", lambda r: [
        _entry(clone, branch="main", is_main=True),
        _entry(wt, branch="local"),
    ])
    # Not empty (has commits), commits NOT on any remote → only 2b can act.
    monkeypatch.setattr(pw, "branch_ahead_behind", lambda r, b, base: (3, 0))
    monkeypatch.setattr(pw, "branch_unpushed_commit_count", lambda r, b: 3)
    monkeypatch.setattr(pw, "branch_contained_in", lambda r, base, b: True)
    removed, force_deleted = [], []
    monkeypatch.setattr(pw, "remove_linked_worktree", lambda r, p: removed.append(p))
    monkeypatch.setattr(
        pw, "delete_branch_trusting_local_default",
        lambda r, b, d: (force_deleted.append((b, d)) or True),
    )
    # Guard: the plain merged-only delete must NOT be used for a 2b candidate.
    monkeypatch.setattr(
        pw, "delete_merged_local_branch",
        lambda r, b: pytest.fail("2b must force-delete, not git branch -d"),
    )
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        branches={"dhh1128/alpha": []},
        my_open_prs={"dhh1128/alpha": []},
        closed_prs_for_head={("dhh1128/alpha", "local"): []},
    )
    _install(monkeypatch, fake)
    rc = prune_worktrees_handler(
        _args(apply=True, code_root=code_root, trust_local_default=True)
    )
    assert rc == EXIT_OK
    assert removed == [wt]
    assert force_deleted == [("local", "main")]
    summary = _summary()
    assert "merged into local default 'main'" in summary


def test_state2b_branch_not_pruned_without_flag(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, clean_helpers,
):
    """Same branch as above, but WITHOUT --trust-local-default → kept."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    clone = code_root / "alpha"
    wt = clone.parent / "alpha-local"
    monkeypatch.setattr(pw, "list_worktrees", lambda r: [
        _entry(clone, branch="main", is_main=True),
        _entry(wt, branch="local"),
    ])
    monkeypatch.setattr(pw, "branch_ahead_behind", lambda r, b, base: (3, 0))
    monkeypatch.setattr(pw, "branch_unpushed_commit_count", lambda r, b: 3)
    monkeypatch.setattr(pw, "branch_contained_in", lambda r, base, b: True)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        branches={"dhh1128/alpha": []},
        my_open_prs={"dhh1128/alpha": []},
        closed_prs_for_head={("dhh1128/alpha", "local"): []},
    )
    _install(monkeypatch, fake)
    rc = prune_worktrees_handler(_args(code_root=code_root))  # no flag
    assert rc == EXIT_OK
    summary = _summary()
    assert "## Would remove" not in summary
    assert "alpha-local" in summary.split("## Kept (guardrail)")[1]
