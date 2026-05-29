"""End-to-end tests for ``gitbulk rebase-pr``.

The invariant chain + PR fetch go through FakeGHClient. The git
mechanics (worktree create/remove, rebase, force-push) are patched at
the rebase_pr module seam so no real git runs — AGENTS.md 'no network
in tests' and the local-git safety contract.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from gitbulk import paths, sentinel
from gitbulk.commands.rebase_pr import (
    EXIT_ATTENTION_NEEDED,
    EXIT_INVARIANT_SKIPPED,
    EXIT_OK,
    EXIT_STRUCTURAL_FAILURE,
    rebase_pr_handler,
)
from gitbulk.gh import FakeGHClient
from gitbulk.org_members_cache import CachedMembers, save_cache
from gitbulk.pr_info import PRInfo
from gitbulk.rebase import RebaseResult, RebaseStatus


# ─── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    paths.ensure_directories()
    return tmp_path


@pytest.fixture
def code_root(tmp_path):
    root = tmp_path / "code"
    root.mkdir()
    return root


def _init_real_clone(code_root: Path, slug: str, default_branch: str = "main") -> None:
    """Materialize a real git clone so the clone-touching local.*
    invariants pass without --skip-check (which would pollute exit
    codes). Mirrors test_dispatch.fake_clones(init=True)."""
    import subprocess as _sp

    _, name = slug.split("/", 1)
    path = code_root / name
    _sp.run(["git", "init", "-q", "-b", default_branch, str(path)], check=True, capture_output=True)
    _sp.run(
        ["git", "-C", str(path), "remote", "add", "origin", f"git@github.com:{slug}.git"],
        check=True, capture_output=True,
    )
    _sp.run(
        ["git", "-C", str(path), "-c", "user.email=t@t.t", "-c", "user.name=t",
         "commit", "--allow-empty", "-q", "-m", "init"],
        check=True, capture_output=True,
    )
    head = _sp.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    _sp.run(
        ["git", "-C", str(path), "update-ref", f"refs/remotes/origin/{default_branch}", head],
        check=True, capture_output=True,
    )
    _sp.run(
        ["git", "-C", str(path), "symbolic-ref", "refs/remotes/origin/HEAD",
         f"refs/remotes/origin/{default_branch}"],
        check=True, capture_output=True,
    )


@pytest.fixture
def write_config(isolated_xdg, code_root):
    def _write(*, repos_slugs):
        cfg_dir = paths.config_dir()
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "gitbulk.yaml").write_text(
            yaml.safe_dump(
                {
                    "defaults": {"retain_runs": 5},
                    "humans": {"org": "provenant-dev", "cache_ttl_hours": 24},
                }
            )
        )
        (cfg_dir / "repos.txt").write_text("\n".join(repos_slugs) + "\n")
        for slug in repos_slugs:
            _init_real_clone(code_root, slug)
        return cfg_dir

    return _write


@pytest.fixture
def fresh_org_cache():
    def _save(org, members):
        save_cache(
            CachedMembers(
                org=org,
                fetched_at=datetime.now(timezone.utc),
                members=frozenset(members),
            )
        )

    return _save


def _make_pr(*, slug, number, mergeable_state="BEHIND", base_ref="main"):
    return PRInfo(
        slug=slug,
        number=number,
        title=f"PR #{number}",
        url=f"https://github.com/{slug}/pull/{number}",
        author="dhh1128",
        base_ref=base_ref,
        head_ref=f"feature/{number}",
        head_sha="a" * 40,
        state="OPEN",
        is_draft=False,
        mergeable_state=mergeable_state,
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 28, tzinfo=timezone.utc),
        last_pushed_at=datetime(2026, 5, 28, tzinfo=timezone.utc),
        labels=(),
        review_decision="APPROVED",
        checks_status="SUCCESS",
    )


def _args(*, apply=False, code_root=None, skip_check=None):
    return argparse.Namespace(
        subcommand="rebase-pr",
        apply=apply,
        code_root=str(code_root) if code_root else None,
        skip_check=list(skip_check) if skip_check else None,
        refresh_org_members=False,
    )


def _fake(slugs_to_prs):
    """Build a FakeGHClient with default branches + open PRs."""
    return FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={slug: "main" for slug in slugs_to_prs},
        my_open_prs=slugs_to_prs,
    )


def _patch_gh(monkeypatch, fake):
    monkeypatch.setattr(
        "gitbulk.commands.rebase_pr.ProductionGHClient", lambda: fake
    )


# ─── Dry-run ───────────────────────────────────────────────────────────────


def test_dry_run_lists_behind_and_dirty_skips_clean(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    behind = _make_pr(slug="dhh1128/alpha", number=1, mergeable_state="BEHIND")
    dirty = _make_pr(slug="dhh1128/alpha", number=2, mergeable_state="DIRTY")
    clean = _make_pr(slug="dhh1128/alpha", number=3, mergeable_state="CLEAN")
    fake = _fake({"dhh1128/alpha": [behind, dirty, clean]})
    _patch_gh(monkeypatch, fake)

    rc = rebase_pr_handler(_args(code_root=code_root))
    # Eligible PRs exist (behind + dirty) → ATTENTION on dry-run.
    assert rc == EXIT_ATTENTION_NEEDED
    summary = (paths.latest_run_symlink("rebase-pr").resolve() / "summary.md").read_text()
    assert "Would rebase" in summary
    assert "/alpha/pull/1" in summary
    assert "/alpha/pull/2" in summary
    assert "/alpha/pull/3" not in summary  # CLEAN skipped by pr.needs_rebase


def test_dry_run_no_eligible_exit_ok(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    clean = _make_pr(slug="dhh1128/alpha", number=1, mergeable_state="CLEAN")
    fake = _fake({"dhh1128/alpha": [clean]})
    _patch_gh(monkeypatch, fake)
    rc = rebase_pr_handler(_args(code_root=code_root))
    assert rc == EXIT_OK
    assert not sentinel.has_attention()


# ─── --apply: clean rebase ─────────────────────────────────────────────────


def test_apply_clean_rebase_force_pushes_and_removes_worktree(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr = _make_pr(slug="dhh1128/alpha", number=1, mergeable_state="BEHIND")
    fake = _fake({"dhh1128/alpha": [pr]})
    _patch_gh(monkeypatch, fake)

    wt = Path("/tmp/fake-worktree")
    calls = {"create": 0, "rebase": 0, "push": 0, "remove": 0}

    def fake_create(*a, **k):
        calls["create"] += 1
        return wt

    def fake_rebase(worktree, base):
        calls["rebase"] += 1
        assert base == "main"
        return RebaseResult(RebaseStatus.CLEAN, "rebased onto origin/main")

    def fake_push(worktree, head_ref, expected):
        calls["push"] += 1
        assert head_ref == "feature/1"
        assert expected == "a" * 40

    def fake_remove(repo_path, worktree):
        calls["remove"] += 1

    monkeypatch.setattr("gitbulk.commands.rebase_pr.create_worktree", fake_create)
    monkeypatch.setattr("gitbulk.commands.rebase_pr.rebase_onto_base", fake_rebase)
    monkeypatch.setattr("gitbulk.commands.rebase_pr.force_push_with_lease", fake_push)
    monkeypatch.setattr("gitbulk.commands.rebase_pr.remove_worktree", fake_remove)

    rc = rebase_pr_handler(_args(apply=True, code_root=code_root))
    assert rc == EXIT_OK
    assert calls == {"create": 1, "rebase": 1, "push": 1, "remove": 1}
    summary = (paths.latest_run_symlink("rebase-pr").resolve() / "summary.md").read_text()
    assert "Rebased (force-pushed)" in summary


# ─── --apply: conflict preserves worktree ──────────────────────────────────


def test_apply_conflict_preserves_worktree_writes_marker(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr = _make_pr(slug="dhh1128/alpha", number=1, mergeable_state="DIRTY")
    fake = _fake({"dhh1128/alpha": [pr]})
    _patch_gh(monkeypatch, fake)

    wt = code_root / "wt-pr1"
    wt.mkdir()
    removed = {"n": 0}

    monkeypatch.setattr("gitbulk.commands.rebase_pr.create_worktree", lambda *a, **k: wt)
    monkeypatch.setattr(
        "gitbulk.commands.rebase_pr.rebase_onto_base",
        lambda w, b: RebaseResult(RebaseStatus.CONFLICT, "src/foo.py"),
    )
    monkeypatch.setattr(
        "gitbulk.commands.rebase_pr.force_push_with_lease",
        lambda *a, **k: pytest.fail("must not push on conflict"),
    )
    monkeypatch.setattr(
        "gitbulk.commands.rebase_pr.remove_worktree",
        lambda *a, **k: removed.__setitem__("n", removed["n"] + 1),
    )

    rc = rebase_pr_handler(_args(apply=True, code_root=code_root))
    assert rc == EXIT_ATTENTION_NEEDED
    # Worktree NOT removed; CONFLICT.md written.
    assert removed["n"] == 0
    assert (wt / "CONFLICT.md").exists()
    marker = (wt / "CONFLICT.md").read_text()
    assert "git rebase --continue" in marker
    assert "src/foo.py" in marker
    summary = (paths.latest_run_symlink("rebase-pr").resolve() / "summary.md").read_text()
    assert "worktree preserved" in summary.lower()


# ─── --apply: rebase error ─────────────────────────────────────────────────


def test_apply_rebase_error_removes_worktree(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr = _make_pr(slug="dhh1128/alpha", number=1, mergeable_state="BEHIND")
    fake = _fake({"dhh1128/alpha": [pr]})
    _patch_gh(monkeypatch, fake)
    removed = {"n": 0}
    monkeypatch.setattr("gitbulk.commands.rebase_pr.create_worktree", lambda *a, **k: Path("/tmp/wt"))
    monkeypatch.setattr(
        "gitbulk.commands.rebase_pr.rebase_onto_base",
        lambda w, b: RebaseResult(RebaseStatus.ERROR, "bad revision"),
    )
    monkeypatch.setattr(
        "gitbulk.commands.rebase_pr.remove_worktree",
        lambda *a, **k: removed.__setitem__("n", removed["n"] + 1),
    )
    rc = rebase_pr_handler(_args(apply=True, code_root=code_root))
    assert rc == EXIT_ATTENTION_NEEDED  # failure → attention
    assert removed["n"] == 1  # error path removes worktree
    summary = (paths.latest_run_symlink("rebase-pr").resolve() / "summary.md").read_text()
    assert "Errors" in summary


# ─── --apply: force-push lease violation ───────────────────────────────────


def test_apply_force_push_failure_records_error(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    from gitbulk.rebase import RebaseError

    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr = _make_pr(slug="dhh1128/alpha", number=1, mergeable_state="BEHIND")
    fake = _fake({"dhh1128/alpha": [pr]})
    _patch_gh(monkeypatch, fake)
    removed = {"n": 0}
    monkeypatch.setattr("gitbulk.commands.rebase_pr.create_worktree", lambda *a, **k: Path("/tmp/wt"))
    monkeypatch.setattr(
        "gitbulk.commands.rebase_pr.rebase_onto_base",
        lambda w, b: RebaseResult(RebaseStatus.CLEAN, "ok"),
    )

    def boom(*a, **k):
        raise RebaseError("lease failed", stderr="remote moved")

    monkeypatch.setattr("gitbulk.commands.rebase_pr.force_push_with_lease", boom)
    monkeypatch.setattr(
        "gitbulk.commands.rebase_pr.remove_worktree",
        lambda *a, **k: removed.__setitem__("n", removed["n"] + 1),
    )
    rc = rebase_pr_handler(_args(apply=True, code_root=code_root))
    assert rc == EXIT_ATTENTION_NEEDED
    assert removed["n"] == 1  # cleaned up after push failure


# ─── --apply: worktree creation failure ────────────────────────────────────


def test_apply_worktree_creation_failure_skips_pr(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    from gitbulk.worktree import WorktreeError

    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr = _make_pr(slug="dhh1128/alpha", number=1, mergeable_state="BEHIND")
    fake = _fake({"dhh1128/alpha": [pr]})
    _patch_gh(monkeypatch, fake)

    def boom(*a, **k):
        raise WorktreeError("worktree add failed", stderr="git error")

    monkeypatch.setattr("gitbulk.commands.rebase_pr.create_worktree", boom)
    rc = rebase_pr_handler(_args(apply=True, code_root=code_root))
    assert rc == EXIT_ATTENTION_NEEDED  # failure


# ─── CLI smoke through main() ──────────────────────────────────────────────


def test_main_rebase_pr_default_is_dry_run(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    from gitbulk.cli import main

    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr = _make_pr(slug="dhh1128/alpha", number=1, mergeable_state="BEHIND")
    fake = _fake({"dhh1128/alpha": [pr]})
    _patch_gh(monkeypatch, fake)
    # No worktree functions should be called on a dry run; if they are,
    # the unconfigured fake create_worktree would hit real git — patch
    # to fail loudly to prove dry-run never touches git.
    monkeypatch.setattr(
        "gitbulk.commands.rebase_pr.create_worktree",
        lambda *a, **k: pytest.fail("dry-run must not create a worktree"),
    )
    rc = main(["rebase-pr", "--code-root", str(code_root)])
    assert rc == EXIT_ATTENTION_NEEDED  # eligible PR in dry-run


# ─── Coverage: structural-failure, skip, and summary-section paths ─────────


def test_lock_timeout_returns_structural_failure(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])

    def _raise(*a, **k):
        from gitbulk.locks import LockTimeoutError
        raise LockTimeoutError(Path("/tmp/x.lock"), None)

    monkeypatch.setattr("gitbulk.commands.rebase_pr.global_lock", _raise)
    rc = rebase_pr_handler(_args(code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE


def test_universal_preflight_failure(monkeypatch, isolated_xdg, code_root, write_config):
    # No org cache → org.members.fresh fails the universal preflight.
    write_config(repos_slugs=["dhh1128/alpha"])
    fake = _fake({"dhh1128/alpha": []})
    _patch_gh(monkeypatch, fake)
    rc = rebase_pr_handler(_args(code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE
    summary = (paths.latest_run_symlink("rebase-pr").resolve() / "summary.md").read_text()
    assert "FAILED" in summary


def test_per_repo_fail_aborts(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    from gitbulk.invariants.base import Fail as _Fail
    from gitbulk.invariants import catalog as _catalog

    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = _fake({"dhh1128/alpha": []})
    _patch_gh(monkeypatch, fake)
    monkeypatch.setattr(
        _catalog.GithubReachableInvariant, "check",
        lambda self, ctx: _Fail("forced fail"),
    )
    rc = rebase_pr_handler(_args(code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE


def test_my_open_prs_failure_returns_structural_failure(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    # default_branches present (so prefetch/invariants pass) but
    # my_open_prs unconfigured → raises.
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
    )
    _patch_gh(monkeypatch, fake)
    rc = rebase_pr_handler(_args(code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE


def test_skipped_repo_surfaces_exit_3(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    """A repo unreachable on GitHub (default_branches missing it) is
    skipped during per-repo preflight → exit 3, listed in summary."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    # github.reachable Skips when default_branch lookup fails. Force that
    # by configuring a fake whose default_branches lacks the slug AND
    # whose prefetch is a no-op (so the cache stays empty → default_branch
    # raises → reachable Skips).
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={},  # alpha not reachable
        my_open_prs={"dhh1128/alpha": []},
    )
    _patch_gh(monkeypatch, fake)
    rc = rebase_pr_handler(_args(code_root=code_root))
    assert rc == EXIT_INVARIANT_SKIPPED
    summary = (paths.latest_run_symlink("rebase-pr").resolve() / "summary.md").read_text()
    assert "Skipped repos" in summary


def test_skipped_repos_txt_entry_surfaces(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    # Append a bad path entry to repos.txt.
    repos_file = paths.repos_file()
    repos_file.write_text(repos_file.read_text() + "/nonexistent/bad\n")
    fake = _fake({"dhh1128/alpha": []})
    _patch_gh(monkeypatch, fake)
    rc = rebase_pr_handler(_args(code_root=code_root))
    assert rc == EXIT_INVARIANT_SKIPPED
    summary = (paths.latest_run_symlink("rebase-pr").resolve() / "summary.md").read_text()
    assert "Skipped repos.txt entries" in summary


def test_runid_from_run_dir_non_rebase_suffix():
    from gitbulk.commands.rebase_pr import _runid_from_run_dir
    assert _runid_from_run_dir(Path("/x/20260529T120000Z-merge")) == "20260529T120000Z"


def test_dry_run_skip_check_exit_overrides(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    """--skip-check on a dry-run with no eligible PRs and no skipped
    repos → EXIT_OVERRIDES_APPLIED (4)."""
    from gitbulk.commands.rebase_pr import EXIT_OVERRIDES_APPLIED
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    clean = _make_pr(slug="dhh1128/alpha", number=1, mergeable_state="CLEAN")
    fake = _fake({"dhh1128/alpha": [clean]})
    _patch_gh(monkeypatch, fake)
    rc = rebase_pr_handler(
        _args(code_root=code_root, skip_check=["pr.author_known"])
    )
    assert rc == EXIT_OVERRIDES_APPLIED


def test_apply_skip_check_exit_overrides(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    """--apply + --skip-check + a clean successful rebase (no failures,
    no skipped repos) → EXIT_OVERRIDES_APPLIED."""
    from gitbulk.commands.rebase_pr import EXIT_OVERRIDES_APPLIED
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr = _make_pr(slug="dhh1128/alpha", number=1, mergeable_state="BEHIND")
    fake = _fake({"dhh1128/alpha": [pr]})
    _patch_gh(monkeypatch, fake)
    monkeypatch.setattr("gitbulk.commands.rebase_pr.create_worktree", lambda *a, **k: Path("/tmp/wt"))
    monkeypatch.setattr(
        "gitbulk.commands.rebase_pr.rebase_onto_base",
        lambda w, b: RebaseResult(RebaseStatus.CLEAN, "ok"),
    )
    monkeypatch.setattr("gitbulk.commands.rebase_pr.force_push_with_lease", lambda *a, **k: None)
    monkeypatch.setattr("gitbulk.commands.rebase_pr.remove_worktree", lambda *a, **k: None)
    rc = rebase_pr_handler(
        _args(apply=True, code_root=code_root, skip_check=["pr.author_known"])
    )
    assert rc == EXIT_OVERRIDES_APPLIED


def test_apply_passing_repo_with_no_eligible_prs(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    """Two reachable repos: one has a BEHIND PR (rebased), the other has
    only a CLEAN PR (no eligible). The no-result repo is skipped in the
    per-repo state recording loop."""
    write_config(repos_slugs=["dhh1128/alpha", "dhh1128/beta"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    behind = _make_pr(slug="dhh1128/alpha", number=1, mergeable_state="BEHIND")
    clean = _make_pr(slug="dhh1128/beta", number=2, mergeable_state="CLEAN")
    fake = _fake({"dhh1128/alpha": [behind], "dhh1128/beta": [clean]})
    _patch_gh(monkeypatch, fake)
    monkeypatch.setattr("gitbulk.commands.rebase_pr.create_worktree", lambda *a, **k: Path("/tmp/wt"))
    monkeypatch.setattr(
        "gitbulk.commands.rebase_pr.rebase_onto_base",
        lambda w, b: RebaseResult(RebaseStatus.CLEAN, "ok"),
    )
    monkeypatch.setattr("gitbulk.commands.rebase_pr.force_push_with_lease", lambda *a, **k: None)
    monkeypatch.setattr("gitbulk.commands.rebase_pr.remove_worktree", lambda *a, **k: None)
    rc = rebase_pr_handler(_args(apply=True, code_root=code_root))
    assert rc == EXIT_OK
    state = yaml.safe_load(
        (paths.latest_run_symlink("rebase-pr").resolve() / "state.yaml").read_text()
    )
    # Only alpha (which had an eligible PR) is recorded; beta isn't.
    assert "dhh1128/alpha" in state["repos"]
    assert "dhh1128/beta" not in state["repos"]


def test_apply_worktree_teardown_failure_is_swallowed(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    """A WorktreeError during cleanup is recorded, not raised — the run
    still completes with the rebase counted."""
    from gitbulk.worktree import WorktreeError
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr = _make_pr(slug="dhh1128/alpha", number=1, mergeable_state="BEHIND")
    fake = _fake({"dhh1128/alpha": [pr]})
    _patch_gh(monkeypatch, fake)
    monkeypatch.setattr("gitbulk.commands.rebase_pr.create_worktree", lambda *a, **k: Path("/tmp/wt"))
    monkeypatch.setattr(
        "gitbulk.commands.rebase_pr.rebase_onto_base",
        lambda w, b: RebaseResult(RebaseStatus.CLEAN, "ok"),
    )
    monkeypatch.setattr("gitbulk.commands.rebase_pr.force_push_with_lease", lambda *a, **k: None)

    def boom(*a, **k):
        raise WorktreeError("remove failed", stderr="git error")

    monkeypatch.setattr("gitbulk.commands.rebase_pr.remove_worktree", boom)
    rc = rebase_pr_handler(_args(apply=True, code_root=code_root))
    # Teardown failure doesn't crash or fail the run; rebase succeeded.
    assert rc == EXIT_OK


def test_apply_with_skipped_repo_exit_invariant_skipped(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    """--apply: one repo rebases cleanly, another is skipped (unreachable
    on GitHub). No failures → EXIT_INVARIANT_SKIPPED (3)."""
    write_config(repos_slugs=["dhh1128/alpha", "dhh1128/beta"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    behind = _make_pr(slug="dhh1128/alpha", number=1, mergeable_state="BEHIND")
    beta_pr = _make_pr(slug="dhh1128/beta", number=2, mergeable_state="BEHIND")
    # alpha reachable (in default_branches), beta NOT → github.reachable
    # skips beta after its local.* invariants pass.
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [behind], "dhh1128/beta": [beta_pr]},
    )
    _patch_gh(monkeypatch, fake)
    monkeypatch.setattr("gitbulk.commands.rebase_pr.create_worktree", lambda *a, **k: Path("/tmp/wt"))
    monkeypatch.setattr(
        "gitbulk.commands.rebase_pr.rebase_onto_base",
        lambda w, b: RebaseResult(RebaseStatus.CLEAN, "ok"),
    )
    monkeypatch.setattr("gitbulk.commands.rebase_pr.force_push_with_lease", lambda *a, **k: None)
    monkeypatch.setattr("gitbulk.commands.rebase_pr.remove_worktree", lambda *a, **k: None)
    rc = rebase_pr_handler(_args(apply=True, code_root=code_root))
    assert rc == EXIT_INVARIANT_SKIPPED
