"""Tests for the Phase 2 concrete invariants (this.i node ``ph2inv4n``).

Every invariant has happy + Skip/Fail branch coverage. The fake GH
client (gitbulk.gh.FakeGHClient) supplies canned answers for every
GH-touching path; local-git probes are exercised by monkeypatching
``subprocess.run`` on the catalog module.

The shared ``ctx_factory`` fixture builds a deterministic
``InvariantContext`` against a tmp ``RunState`` so that calling
``check()`` directly records to a real (but throwaway) run dir.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gitbulk import paths
from gitbulk.classifier import Classification
from gitbulk.config.policy import Defaults, HumansConfig, Policy, RepoOverride
from gitbulk.config.repos import RepoEntry
from gitbulk.gh import FakeGHClient, GHError
from gitbulk.invariants import (
    Fail,
    InvariantContext,
    InvariantKind,
    Pass,
    Skip,
    all_invariants,
)
from gitbulk.invariants import catalog
from gitbulk.invariants.catalog import (
    ConfigParseableInvariant,
    GhAuthenticatedInvariant,
    GithubNotArchivedInvariant,
    GithubReachableInvariant,
    LocalDefaultBranchInSyncInvariant,
    LocalExistsInvariant,
    LocalRemoteMatchesInvariant,
    OrgMembersFreshInvariant,
    PrAgeThresholdInvariant,
    PrApprovedPerPolicyInvariant,
    PrAuthorKnownInvariant,
    PrBaseIsDefaultInvariant,
    PrInactiveInvariant,
    PrNeedsRebaseInvariant,
    PrMergeableStateCleanInvariant,
    PrNoUnresolvedThreadsInvariant,
    PrRequiredChecksGreenInvariant,
    _extract_slug_from_remote_url,
)
from gitbulk.pr_info import PRInfo
from gitbulk.org_members_cache import CachedMembers, save_cache
from gitbulk.runstate import RunState


# ─── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_cache(monkeypatch, tmp_path):
    """Redirect XDG_CACHE_HOME / XDG_CONFIG_HOME to tmp."""
    cache_root = tmp_path / "cache-root"
    config_root = tmp_path / "config-root"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    paths.ensure_directories()
    return cache_root


@pytest.fixture
def runstate(isolated_cache):
    return RunState.begin("report", ["gitbulk", "report"], {})


@pytest.fixture
def repo(tmp_path):
    """A canned RepoEntry pointing at a tmp directory."""
    local = tmp_path / "gitbulk"
    local.mkdir()
    return RepoEntry(
        slug="dhh1128/gitbulk",
        owner="dhh1128",
        name="gitbulk",
        local_path=local,
        source_line=1,
    )


def _pr(
    *,
    number: int = 7,
    author: str = "dhh1128",
    base_ref: str = "main",
    slug: str = "dhh1128/gitbulk",
) -> SimpleNamespace:
    """Lightweight PRInfo-ish stand-in. The invariants only touch
    .number, .author, .base_ref; a SimpleNamespace is enough."""
    return SimpleNamespace(
        number=number,
        author=author,
        base_ref=base_ref,
        slug=slug,
    )


def _ctx(
    runstate: RunState,
    *,
    policy: Policy | None = None,
    repo: RepoEntry | None = None,
    pr: Any = None,
    gh: Any = None,
) -> InvariantContext:
    return InvariantContext(
        policy=policy if policy is not None else Policy(),
        runstate=runstate,
        repo=repo,
        pr=pr,
        gh=gh,
    )


def _save_fresh_cache(org: str, members: list[str]) -> None:
    save_cache(
        CachedMembers(
            org=org,
            fetched_at=datetime.now(timezone.utc),
            members=frozenset(members),
        )
    )


def _save_stale_cache(org: str, members: list[str]) -> None:
    save_cache(
        CachedMembers(
            org=org,
            fetched_at=datetime.now(timezone.utc) - timedelta(days=30),
            members=frozenset(members),
        )
    )


# ─── Registration sanity ───────────────────────────────────────────────────


def test_all_phase2_invariants_registered():
    # Importing the gitbulk.invariants package triggers the side-effect
    # import of catalog, which registers each class.
    from gitbulk import invariants  # noqa: F401

    expected = {
        "gh.authenticated",
        "config.parseable",
        "org.members.fresh",
        "local.exists",
        "local.remote_matches",
        "local.default_branch_in_sync",
        "github.reachable",
        "github.not_archived",
        "pr.base_is_default",
        "pr.author_known",
        "pr.mergeable_state_clean",
        "pr.required_checks_green",
        "pr.approved_per_policy",
        "pr.no_unresolved_threads",
        "pr.age_threshold",
        "pr.inactive",
        "pr.needs_rebase",
    }
    registered = set(all_invariants().keys())
    missing = expected - registered
    assert not missing, f"missing invariants: {missing}"


def test_kinds_are_set_correctly():
    assert GhAuthenticatedInvariant.kind == InvariantKind.UNIVERSAL
    assert ConfigParseableInvariant.kind == InvariantKind.UNIVERSAL
    assert OrgMembersFreshInvariant.kind == InvariantKind.UNIVERSAL
    assert LocalExistsInvariant.kind == InvariantKind.PER_REPO
    assert LocalRemoteMatchesInvariant.kind == InvariantKind.PER_REPO
    assert LocalDefaultBranchInSyncInvariant.kind == InvariantKind.PER_REPO
    assert GithubReachableInvariant.kind == InvariantKind.PER_REPO
    assert GithubNotArchivedInvariant.kind == InvariantKind.PER_REPO
    assert PrBaseIsDefaultInvariant.kind == InvariantKind.PER_PR
    assert PrAuthorKnownInvariant.kind == InvariantKind.PER_PR
    assert PrMergeableStateCleanInvariant.kind == InvariantKind.PER_PR
    assert PrRequiredChecksGreenInvariant.kind == InvariantKind.PER_PR
    assert PrApprovedPerPolicyInvariant.kind == InvariantKind.PER_PR
    assert PrAgeThresholdInvariant.kind == InvariantKind.PER_PR


def test_merge_only_invariants_subcommand_membership():
    """The four Phase-5 invariants apply only to ``merge``."""
    for cls in (
        PrMergeableStateCleanInvariant,
        PrRequiredChecksGreenInvariant,
        PrApprovedPerPolicyInvariant,
        PrAgeThresholdInvariant,
    ):
        assert cls.subcommands == frozenset({"merge"})


def test_clone_subcommand_membership():
    """local.* invariants apply only to subcommands that need a clone."""
    for cls in (
        LocalExistsInvariant,
        LocalRemoteMatchesInvariant,
        LocalDefaultBranchInSyncInvariant,
    ):
        assert cls.subcommands == frozenset(
            {"dispatch", "rebase-pr"}
        )


def test_universal_subcommand_membership_covers_six():
    expected = frozenset(
        {
            "report",
            "summarize",
            "dispatch",
            "merge",
            "rebase-pr",
            "close-stale",
        }
    )
    for cls in (
        GhAuthenticatedInvariant,
        ConfigParseableInvariant,
        OrgMembersFreshInvariant,
        GithubReachableInvariant,
        PrBaseIsDefaultInvariant,
        PrAuthorKnownInvariant,
    ):
        assert cls.subcommands == expected


# ─── gh.authenticated ──────────────────────────────────────────────────────


def test_gh_authenticated_pass(runstate):
    gh = FakeGHClient(user={"login": "dhh1128"})
    ctx = _ctx(runstate, gh=gh)
    assert GhAuthenticatedInvariant().check(ctx) == Pass()


def test_gh_authenticated_fail_no_gh(runstate):
    ctx = _ctx(runstate, gh=None)
    result = GhAuthenticatedInvariant().check(ctx)
    assert isinstance(result, Fail)
    assert "gh client not present" in result.reason


def test_gh_authenticated_fail_gh_error(runstate):
    gh = FakeGHClient()  # no user configured → GHError
    ctx = _ctx(runstate, gh=gh)
    result = GhAuthenticatedInvariant().check(ctx)
    assert isinstance(result, Fail)
    assert "gh not authenticated" in result.reason


def test_gh_authenticated_fail_user_has_no_login(runstate):
    gh = FakeGHClient(user={"name": "ghost"})  # missing 'login'
    ctx = _ctx(runstate, gh=gh)
    result = GhAuthenticatedInvariant().check(ctx)
    assert isinstance(result, Fail)
    assert "no login" in result.reason


def test_gh_authenticated_fail_user_login_empty_string(runstate):
    gh = FakeGHClient(user={"login": ""})  # falsy 'login'
    ctx = _ctx(runstate, gh=gh)
    result = GhAuthenticatedInvariant().check(ctx)
    assert isinstance(result, Fail)
    assert "no login" in result.reason


# ─── config.parseable ──────────────────────────────────────────────────────


def test_config_parseable_pass(runstate):
    ctx = _ctx(runstate)
    assert ConfigParseableInvariant().check(ctx) == Pass()


def test_config_parseable_fail_when_policy_is_none(runstate):
    # Bypass the dataclass to construct an invalid context.
    ctx = InvariantContext.__new__(InvariantContext)
    object.__setattr__(ctx, "policy", None)
    object.__setattr__(ctx, "runstate", runstate)
    object.__setattr__(ctx, "repo", None)
    object.__setattr__(ctx, "pr", None)
    object.__setattr__(ctx, "gh", None)
    result = ConfigParseableInvariant().check(ctx)
    assert isinstance(result, Fail)
    assert "policy not loaded" in result.reason


# ─── org.members.fresh ─────────────────────────────────────────────────────


def test_org_members_fresh_pass_no_org_configured(runstate, isolated_cache):
    """No org configured → invariant passes (classifier handles unknowns)."""
    policy = Policy(humans=HumansConfig(org=None))
    ctx = _ctx(runstate, policy=policy)
    assert OrgMembersFreshInvariant().check(ctx) == Pass()


def test_org_members_fresh_pass_when_cache_fresh(runstate, isolated_cache):
    _save_fresh_cache("provenant-dev", ["alice", "bob"])
    policy = Policy(humans=HumansConfig(org="provenant-dev", cache_ttl_hours=24))
    ctx = _ctx(runstate, policy=policy)
    assert OrgMembersFreshInvariant().check(ctx) == Pass()


def test_org_members_fresh_fail_when_cache_missing(runstate, isolated_cache):
    policy = Policy(humans=HumansConfig(org="provenant-dev"))
    ctx = _ctx(runstate, policy=policy)
    result = OrgMembersFreshInvariant().check(ctx)
    assert isinstance(result, Fail)
    assert "missing" in result.reason
    assert "provenant-dev" in result.reason


def test_org_members_fresh_fail_when_cache_stale(runstate, isolated_cache):
    _save_stale_cache("provenant-dev", ["alice"])
    policy = Policy(humans=HumansConfig(org="provenant-dev", cache_ttl_hours=24))
    ctx = _ctx(runstate, policy=policy)
    result = OrgMembersFreshInvariant().check(ctx)
    assert isinstance(result, Fail)
    assert "older than" in result.reason


# ─── _extract_slug_from_remote_url ─────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        ("git@github.com:dhh1128/gitbulk.git", "dhh1128/gitbulk"),
        ("git@github.com:dhh1128/gitbulk", "dhh1128/gitbulk"),
        ("https://github.com/dhh1128/gitbulk.git", "dhh1128/gitbulk"),
        ("https://github.com/dhh1128/gitbulk", "dhh1128/gitbulk"),
        ("https://github.com/dhh1128/gitbulk/", "dhh1128/gitbulk"),
        ("http://github.com/dhh1128/gitbulk", "dhh1128/gitbulk"),
        ("ssh://git@github.com/dhh1128/gitbulk.git", "dhh1128/gitbulk"),
        ("ssh://git@github.com/dhh1128/gitbulk", "dhh1128/gitbulk"),
    ],
)
def test_extract_slug_from_remote_url_recognized(url, expected):
    assert _extract_slug_from_remote_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "garbage",
        "git@gitlab.com:dhh1128/gitbulk.git",
        "https://gitlab.com/dhh1128/gitbulk",
        "https://bitbucket.org/dhh1128/gitbulk",
        "file:///home/dhh1128/code/gitbulk",
        "git@github.com:dhh1128",  # missing repo
    ],
)
def test_extract_slug_from_remote_url_unrecognized_returns_none(url):
    assert _extract_slug_from_remote_url(url) is None


# ─── local.exists ──────────────────────────────────────────────────────────


def _make_completed_process(
    *, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_local_exists_pass(monkeypatch, runstate, repo):
    monkeypatch.setattr(
        catalog.subprocess,
        "run",
        lambda *a, **kw: _make_completed_process(stdout="true\n"),
    )
    ctx = _ctx(runstate, repo=repo)
    assert LocalExistsInvariant().check(ctx) == Pass()


def test_local_exists_fail_when_repo_missing_from_context(runstate):
    ctx = _ctx(runstate, repo=None)
    result = LocalExistsInvariant().check(ctx)
    assert isinstance(result, Fail)
    assert "without ctx.repo" in result.reason


def test_local_exists_skip_when_path_missing(tmp_path, runstate):
    """Path doesn't exist on disk."""
    missing = tmp_path / "no-such-clone"
    entry = RepoEntry(
        slug="dhh1128/gitbulk",
        owner="dhh1128",
        name="gitbulk",
        local_path=missing,
        source_line=1,
    )
    ctx = _ctx(runstate, repo=entry)
    result = LocalExistsInvariant().check(ctx)
    assert isinstance(result, Skip)
    assert "local clone missing" in result.reason


def test_local_exists_skip_when_not_a_git_worktree(monkeypatch, runstate, repo):
    """The directory exists but `git rev-parse` returns nonzero."""
    monkeypatch.setattr(
        catalog.subprocess,
        "run",
        lambda *a, **kw: _make_completed_process(
            returncode=128, stderr="not a git repository"
        ),
    )
    ctx = _ctx(runstate, repo=repo)
    result = LocalExistsInvariant().check(ctx)
    assert isinstance(result, Skip)
    assert "not a git working tree" in result.reason


def test_local_exists_skip_when_git_reports_false(monkeypatch, runstate, repo):
    """git rev-parse exits 0 but stdout is not 'true' (defensive)."""
    monkeypatch.setattr(
        catalog.subprocess,
        "run",
        lambda *a, **kw: _make_completed_process(stdout="false\n"),
    )
    ctx = _ctx(runstate, repo=repo)
    result = LocalExistsInvariant().check(ctx)
    assert isinstance(result, Skip)


# ─── local.remote_matches ──────────────────────────────────────────────────


def test_local_remote_matches_pass(monkeypatch, runstate, repo):
    monkeypatch.setattr(
        catalog.subprocess,
        "run",
        lambda *a, **kw: _make_completed_process(
            stdout="git@github.com:dhh1128/gitbulk.git\n"
        ),
    )
    ctx = _ctx(runstate, repo=repo)
    assert LocalRemoteMatchesInvariant().check(ctx) == Pass()


def test_local_remote_matches_fail_no_repo(runstate):
    ctx = _ctx(runstate, repo=None)
    result = LocalRemoteMatchesInvariant().check(ctx)
    assert isinstance(result, Fail)


def test_local_remote_matches_skip_no_origin(monkeypatch, runstate, repo):
    monkeypatch.setattr(
        catalog.subprocess,
        "run",
        lambda *a, **kw: _make_completed_process(
            returncode=2, stderr="error: No such remote 'origin'"
        ),
    )
    ctx = _ctx(runstate, repo=repo)
    result = LocalRemoteMatchesInvariant().check(ctx)
    assert isinstance(result, Skip)
    assert "origin remote not configured" in result.reason


def test_local_remote_matches_skip_unrecognized_url(monkeypatch, runstate, repo):
    monkeypatch.setattr(
        catalog.subprocess,
        "run",
        lambda *a, **kw: _make_completed_process(
            stdout="git@gitlab.com:dhh1128/gitbulk.git\n"
        ),
    )
    ctx = _ctx(runstate, repo=repo)
    result = LocalRemoteMatchesInvariant().check(ctx)
    assert isinstance(result, Skip)
    assert "not a recognized GitHub remote" in result.reason


def test_local_remote_matches_skip_when_origin_points_elsewhere(
    monkeypatch, runstate, repo
):
    monkeypatch.setattr(
        catalog.subprocess,
        "run",
        lambda *a, **kw: _make_completed_process(
            stdout="git@github.com:someone-else/gitbulk.git\n"
        ),
    )
    ctx = _ctx(runstate, repo=repo)
    result = LocalRemoteMatchesInvariant().check(ctx)
    assert isinstance(result, Skip)
    assert "someone-else/gitbulk" in result.reason
    assert "dhh1128/gitbulk" in result.reason


# ─── local.default_branch_in_sync ─────────────────────────────────────────


def test_local_default_branch_in_sync_pass(monkeypatch, runstate, repo):
    monkeypatch.setattr(
        catalog.subprocess,
        "run",
        lambda *a, **kw: _make_completed_process(
            stdout="refs/remotes/origin/main\n"
        ),
    )
    gh = FakeGHClient(default_branches={"dhh1128/gitbulk": "main"})
    ctx = _ctx(runstate, repo=repo, gh=gh)
    assert LocalDefaultBranchInSyncInvariant().check(ctx) == Pass()


def test_local_default_branch_in_sync_fail_no_repo(runstate):
    ctx = _ctx(runstate, repo=None, gh=FakeGHClient())
    result = LocalDefaultBranchInSyncInvariant().check(ctx)
    assert isinstance(result, Fail)
    assert "without ctx.repo" in result.reason


def test_local_default_branch_in_sync_fail_no_gh(runstate, repo):
    ctx = _ctx(runstate, repo=repo, gh=None)
    result = LocalDefaultBranchInSyncInvariant().check(ctx)
    assert isinstance(result, Fail)
    assert "without ctx.gh" in result.reason


def test_local_default_branch_in_sync_skip_when_gh_errors(
    monkeypatch, runstate, repo
):
    gh = FakeGHClient()  # default_branch unconfigured → GHError
    ctx = _ctx(runstate, repo=repo, gh=gh)
    result = LocalDefaultBranchInSyncInvariant().check(ctx)
    assert isinstance(result, Skip)
    assert "could not determine" in result.reason


def test_local_default_branch_in_sync_skip_when_symref_unset(
    monkeypatch, runstate, repo
):
    monkeypatch.setattr(
        catalog.subprocess,
        "run",
        lambda *a, **kw: _make_completed_process(
            returncode=1, stderr="fatal: ref refs/remotes/origin/HEAD is not a symbolic ref"
        ),
    )
    gh = FakeGHClient(default_branches={"dhh1128/gitbulk": "main"})
    ctx = _ctx(runstate, repo=repo, gh=gh)
    result = LocalDefaultBranchInSyncInvariant().check(ctx)
    assert isinstance(result, Skip)
    assert "origin/HEAD not set" in result.reason


def test_local_default_branch_in_sync_skip_when_symref_malformed(
    monkeypatch, runstate, repo
):
    monkeypatch.setattr(
        catalog.subprocess,
        "run",
        lambda *a, **kw: _make_completed_process(
            stdout="refs/heads/main\n"  # unexpected prefix
        ),
    )
    gh = FakeGHClient(default_branches={"dhh1128/gitbulk": "main"})
    ctx = _ctx(runstate, repo=repo, gh=gh)
    result = LocalDefaultBranchInSyncInvariant().check(ctx)
    assert isinstance(result, Skip)
    assert "unrecognized origin/HEAD symref" in result.reason


def test_local_default_branch_in_sync_skip_on_divergence(
    monkeypatch, runstate, repo
):
    monkeypatch.setattr(
        catalog.subprocess,
        "run",
        lambda *a, **kw: _make_completed_process(
            stdout="refs/remotes/origin/master\n"
        ),
    )
    gh = FakeGHClient(default_branches={"dhh1128/gitbulk": "main"})
    ctx = _ctx(runstate, repo=repo, gh=gh)
    result = LocalDefaultBranchInSyncInvariant().check(ctx)
    assert isinstance(result, Skip)
    assert "'master'" in result.reason
    assert "'main'" in result.reason


# ─── github.reachable ──────────────────────────────────────────────────────


def test_github_reachable_pass(runstate, repo):
    gh = FakeGHClient(default_branches={"dhh1128/gitbulk": "main"})
    ctx = _ctx(runstate, repo=repo, gh=gh)
    assert GithubReachableInvariant().check(ctx) == Pass()


def test_github_reachable_fail_no_repo(runstate):
    ctx = _ctx(runstate, repo=None, gh=FakeGHClient())
    result = GithubReachableInvariant().check(ctx)
    assert isinstance(result, Fail)
    assert "without ctx.repo" in result.reason


def test_github_reachable_fail_no_gh(runstate, repo):
    ctx = _ctx(runstate, repo=repo, gh=None)
    result = GithubReachableInvariant().check(ctx)
    assert isinstance(result, Fail)
    assert "without ctx.gh" in result.reason


def test_github_reachable_skip_when_gh_errors(runstate, repo):
    gh = FakeGHClient()  # nothing configured → GHError
    ctx = _ctx(runstate, repo=repo, gh=gh)
    result = GithubReachableInvariant().check(ctx)
    assert isinstance(result, Skip)
    assert "github not reachable" in result.reason


# ─── github.not_archived ───────────────────────────────────────────────────


def test_github_not_archived_pass_when_not_archived(runstate, repo):
    gh = FakeGHClient(archived={"dhh1128/gitbulk": False})
    ctx = _ctx(runstate, repo=repo, gh=gh)
    assert GithubNotArchivedInvariant().check(ctx) == Pass()


def test_github_not_archived_pass_when_unconfigured(runstate, repo):
    """A repo absent from the archived map is treated as live → Pass.
    Keeps the gate transparent for the overwhelming majority of repos."""
    gh = FakeGHClient(archived={})
    ctx = _ctx(runstate, repo=repo, gh=gh)
    assert GithubNotArchivedInvariant().check(ctx) == Pass()


def test_github_not_archived_skip_when_archived(runstate, repo):
    gh = FakeGHClient(archived={"dhh1128/gitbulk": True})
    ctx = _ctx(runstate, repo=repo, gh=gh)
    result = GithubNotArchivedInvariant().check(ctx)
    assert isinstance(result, Skip)
    assert "archived" in result.reason
    assert "dhh1128/gitbulk" in result.reason


def test_github_not_archived_fail_no_repo(runstate):
    ctx = _ctx(runstate, repo=None, gh=FakeGHClient(archived={}))
    result = GithubNotArchivedInvariant().check(ctx)
    assert isinstance(result, Fail)
    assert "without ctx.repo" in result.reason


def test_github_not_archived_fail_no_gh(runstate, repo):
    ctx = _ctx(runstate, repo=repo, gh=None)
    result = GithubNotArchivedInvariant().check(ctx)
    assert isinstance(result, Fail)
    assert "without ctx.gh" in result.reason


def test_github_not_archived_skip_when_gh_errors(runstate, repo):
    gh = FakeGHClient(archived={"dhh1128/gitbulk": GHError("boom")})
    ctx = _ctx(runstate, repo=repo, gh=gh)
    result = GithubNotArchivedInvariant().check(ctx)
    assert isinstance(result, Skip)
    assert "dhh1128/gitbulk" in result.reason


# ─── pr.base_is_default ────────────────────────────────────────────────────


def test_pr_base_is_default_pass(runstate, repo):
    pr = _pr(base_ref="main")
    gh = FakeGHClient(default_branches={"dhh1128/gitbulk": "main"})
    ctx = _ctx(runstate, repo=repo, pr=pr, gh=gh)
    assert PrBaseIsDefaultInvariant().check(ctx) == Pass()


def test_pr_base_is_default_fail_no_pr(runstate, repo):
    ctx = _ctx(runstate, repo=repo, pr=None, gh=FakeGHClient())
    result = PrBaseIsDefaultInvariant().check(ctx)
    assert isinstance(result, Fail)
    assert "without ctx.pr" in result.reason


def test_pr_base_is_default_fail_no_repo(runstate):
    pr = _pr()
    ctx = _ctx(runstate, repo=None, pr=pr, gh=FakeGHClient())
    result = PrBaseIsDefaultInvariant().check(ctx)
    assert isinstance(result, Fail)
    assert "without ctx.pr" in result.reason


def test_pr_base_is_default_fail_no_gh(runstate, repo):
    pr = _pr()
    ctx = _ctx(runstate, repo=repo, pr=pr, gh=None)
    result = PrBaseIsDefaultInvariant().check(ctx)
    assert isinstance(result, Fail)
    assert "without ctx.gh" in result.reason


def test_pr_base_is_default_skip_when_gh_errors(runstate, repo):
    pr = _pr()
    gh = FakeGHClient()  # default_branch unconfigured
    ctx = _ctx(runstate, repo=repo, pr=pr, gh=gh)
    result = PrBaseIsDefaultInvariant().check(ctx)
    assert isinstance(result, Skip)
    assert "could not determine default branch" in result.reason


def test_pr_base_is_default_skip_when_base_diverges(runstate, repo):
    pr = _pr(base_ref="feature-branch")
    gh = FakeGHClient(default_branches={"dhh1128/gitbulk": "main"})
    ctx = _ctx(runstate, repo=repo, pr=pr, gh=gh)
    result = PrBaseIsDefaultInvariant().check(ctx)
    assert isinstance(result, Skip)
    assert "feature-branch" in result.reason
    assert "'main'" in result.reason


# ─── pr.author_known ───────────────────────────────────────────────────────


def test_pr_author_known_pass_human(runstate, isolated_cache):
    _save_fresh_cache("provenant-dev", ["dhh1128"])
    policy = Policy(humans=HumansConfig(org="provenant-dev"))
    pr = _pr(author="dhh1128")
    ctx = _ctx(runstate, policy=policy, pr=pr)
    assert PrAuthorKnownInvariant().check(ctx) == Pass()


def test_pr_author_known_pass_bot(runstate, isolated_cache):
    """Login on the bots list classifies as BOT — still a Pass."""
    policy = Policy(bots=("dependabot[bot]",))
    pr = _pr(author="dependabot[bot]")
    ctx = _ctx(runstate, policy=policy, pr=pr)
    assert PrAuthorKnownInvariant().check(ctx) == Pass()


def test_pr_author_known_pass_when_no_org_configured(runstate, isolated_cache):
    """No org → cache step skipped, unknown logins fall through to BOT
    (which is still a Pass for this invariant)."""
    policy = Policy(humans=HumansConfig(org=None))
    pr = _pr(author="some-rando")
    ctx = _ctx(runstate, policy=policy, pr=pr)
    assert PrAuthorKnownInvariant().check(ctx) == Pass()


def test_pr_author_known_pass_when_cache_missing(runstate, isolated_cache):
    """Org configured but cache file absent: classify_login is called
    with org_members=None and the unknown login falls through to BOT."""
    policy = Policy(humans=HumansConfig(org="provenant-dev"))
    pr = _pr(author="some-rando")
    ctx = _ctx(runstate, policy=policy, pr=pr)
    assert PrAuthorKnownInvariant().check(ctx) == Pass()


def test_pr_author_known_fail_no_pr(runstate):
    ctx = _ctx(runstate, pr=None)
    result = PrAuthorKnownInvariant().check(ctx)
    assert isinstance(result, Fail)
    assert "without ctx.pr" in result.reason


def test_pr_author_known_fail_when_classifier_returns_unknown(
    monkeypatch, runstate, isolated_cache
):
    """Defensive Fail branch: production code never sees UNKNOWN
    (org.members.fresh is run first), but if it ever does, the
    invariant aborts the run."""
    monkeypatch.setattr(
        catalog,
        "classify_login",
        lambda login, policy, org_members: Classification.UNKNOWN,
    )
    pr = _pr(author="ghost")
    ctx = _ctx(runstate, pr=pr)
    result = PrAuthorKnownInvariant().check(ctx)
    assert isinstance(result, Fail)
    assert "UNKNOWN" in result.reason
    assert "ghost" in result.reason


# ─── Phase 5 merge-only invariants ────────────────────────────────────────


def _real_pr(
    *,
    mergeable_state: str | None = "CLEAN",
    checks_status: str | None = "SUCCESS",
    review_decision: str | None = "APPROVED",
    last_pushed_at: datetime | None = None,
    base_ref: str = "main",
    slug: str = "dhh1128/gitbulk",
    number: int = 7,
    unresolved_thread_count: int = 0,
) -> PRInfo:
    """A real PRInfo (not the SimpleNamespace stand-in) because the
    merge invariants consult ``mergeable_state`` / ``checks_status`` /
    ``review_decision`` / ``last_pushed_at`` fields that the lightweight
    stand-in doesn't carry."""
    if last_pushed_at is None:
        # Default: pushed 10 business days ago so age_threshold passes
        # in tests that don't override min_business_days.
        last_pushed_at = datetime.now(timezone.utc) - timedelta(days=20)
    return PRInfo(
        slug=slug,
        number=number,
        title="t",
        url=f"https://github.com/{slug}/pull/{number}",
        author="dhh1128",
        base_ref=base_ref,
        head_ref="f",
        head_sha="a" * 40,
        state="OPEN",
        is_draft=False,
        mergeable_state=mergeable_state,
        created_at=datetime.now(timezone.utc) - timedelta(days=30),
        updated_at=datetime.now(timezone.utc),
        last_pushed_at=last_pushed_at,
        labels=(),
        review_decision=review_decision,
        checks_status=checks_status,
        unresolved_thread_count=unresolved_thread_count,
    )


# ─── pr.mergeable_state_clean ─────────────────────────────────────────────


def test_pr_mergeable_state_clean_pass(runstate, repo):
    pr = _real_pr(mergeable_state="CLEAN")
    ctx = _ctx(runstate, repo=repo, pr=pr)
    assert PrMergeableStateCleanInvariant().check(ctx) == Pass()


def test_pr_mergeable_state_clean_skip_when_dirty(runstate, repo):
    pr = _real_pr(mergeable_state="DIRTY")
    ctx = _ctx(runstate, repo=repo, pr=pr)
    result = PrMergeableStateCleanInvariant().check(ctx)
    assert isinstance(result, Skip)
    assert "DIRTY" in result.reason


def test_pr_mergeable_state_clean_skip_when_none(runstate, repo):
    pr = _real_pr(mergeable_state=None)
    ctx = _ctx(runstate, repo=repo, pr=pr)
    result = PrMergeableStateCleanInvariant().check(ctx)
    assert isinstance(result, Skip)


def test_pr_mergeable_state_clean_fail_no_pr(runstate, repo):
    ctx = _ctx(runstate, repo=repo, pr=None)
    result = PrMergeableStateCleanInvariant().check(ctx)
    assert isinstance(result, Fail)
    assert "without ctx.pr" in result.reason


# ─── pr.required_checks_green ─────────────────────────────────────────────


def test_pr_required_checks_green_pass(runstate, repo):
    pr = _real_pr(checks_status="SUCCESS")
    ctx = _ctx(runstate, repo=repo, pr=pr)
    assert PrRequiredChecksGreenInvariant().check(ctx) == Pass()


def test_pr_required_checks_green_skip_on_failure(runstate, repo):
    pr = _real_pr(checks_status="FAILURE")
    ctx = _ctx(runstate, repo=repo, pr=pr)
    result = PrRequiredChecksGreenInvariant().check(ctx)
    assert isinstance(result, Skip)
    assert "FAILURE" in result.reason


def test_pr_required_checks_green_skip_on_pending(runstate, repo):
    pr = _real_pr(checks_status="PENDING")
    ctx = _ctx(runstate, repo=repo, pr=pr)
    result = PrRequiredChecksGreenInvariant().check(ctx)
    assert isinstance(result, Skip)


def test_pr_required_checks_green_skip_on_none(runstate, repo):
    pr = _real_pr(checks_status=None)
    ctx = _ctx(runstate, repo=repo, pr=pr)
    result = PrRequiredChecksGreenInvariant().check(ctx)
    assert isinstance(result, Skip)


def test_pr_required_checks_green_fail_no_pr(runstate, repo):
    ctx = _ctx(runstate, repo=repo, pr=None)
    result = PrRequiredChecksGreenInvariant().check(ctx)
    assert isinstance(result, Fail)


# ─── pr.approved_per_policy ───────────────────────────────────────────────


def test_pr_approved_per_policy_strict_pass(runstate, repo):
    policy = Policy(defaults=Defaults(merge_policy="strict"))
    pr = _real_pr(review_decision="APPROVED")
    ctx = _ctx(runstate, policy=policy, repo=repo, pr=pr)
    assert PrApprovedPerPolicyInvariant().check(ctx) == Pass()


def test_pr_approved_per_policy_strict_skip_when_unreviewed(runstate, repo):
    policy = Policy(defaults=Defaults(merge_policy="strict"))
    pr = _real_pr(review_decision="REVIEW_REQUIRED")
    ctx = _ctx(runstate, policy=policy, repo=repo, pr=pr)
    result = PrApprovedPerPolicyInvariant().check(ctx)
    assert isinstance(result, Skip)
    assert "APPROVED" in result.reason
    assert "REVIEW_REQUIRED" in result.reason


def test_pr_approved_per_policy_ci_only_pass_regardless(runstate, repo):
    policy = Policy(defaults=Defaults(merge_policy="ci-only"))
    pr = _real_pr(review_decision="REVIEW_REQUIRED")
    ctx = _ctx(runstate, policy=policy, repo=repo, pr=pr)
    assert PrApprovedPerPolicyInvariant().check(ctx) == Pass()


def test_pr_approved_per_policy_never_always_skip(runstate, repo):
    policy = Policy(defaults=Defaults(merge_policy="never"))
    pr = _real_pr(review_decision="APPROVED")
    ctx = _ctx(runstate, policy=policy, repo=repo, pr=pr)
    result = PrApprovedPerPolicyInvariant().check(ctx)
    assert isinstance(result, Skip)
    assert "never" in result.reason


def test_pr_approved_per_policy_per_repo_override_wins(runstate, repo):
    """A per-repo merge_policy='never' override beats defaults.strict."""
    policy = Policy(
        defaults=Defaults(merge_policy="strict"),
        repos={"dhh1128/gitbulk": RepoOverride(merge_policy="never")},
    )
    pr = _real_pr(review_decision="APPROVED")
    ctx = _ctx(runstate, policy=policy, repo=repo, pr=pr)
    result = PrApprovedPerPolicyInvariant().check(ctx)
    assert isinstance(result, Skip)


def test_pr_approved_per_policy_fail_no_pr(runstate, repo):
    ctx = _ctx(runstate, repo=repo, pr=None)
    result = PrApprovedPerPolicyInvariant().check(ctx)
    assert isinstance(result, Fail)


def test_pr_approved_per_policy_fail_no_repo(runstate):
    pr = _real_pr()
    ctx = _ctx(runstate, repo=None, pr=pr)
    result = PrApprovedPerPolicyInvariant().check(ctx)
    assert isinstance(result, Fail)


# ─── pr.no_unresolved_threads ─────────────────────────────────────────────


def test_pr_no_unresolved_threads_pass_when_zero(runstate, repo):
    pr = _real_pr(unresolved_thread_count=0)
    ctx = _ctx(runstate, repo=repo, pr=pr)
    assert PrNoUnresolvedThreadsInvariant().check(ctx) == Pass()


def test_pr_no_unresolved_threads_skip_when_one(runstate, repo):
    pr = _real_pr(unresolved_thread_count=1)
    ctx = _ctx(runstate, repo=repo, pr=pr)
    result = PrNoUnresolvedThreadsInvariant().check(ctx)
    assert isinstance(result, Skip)
    assert "1 unresolved" in result.reason


def test_pr_no_unresolved_threads_skip_includes_count(runstate, repo):
    pr = _real_pr(unresolved_thread_count=4)
    ctx = _ctx(runstate, repo=repo, pr=pr)
    result = PrNoUnresolvedThreadsInvariant().check(ctx)
    assert isinstance(result, Skip)
    assert "4 unresolved" in result.reason


def test_pr_no_unresolved_threads_fail_no_pr(runstate, repo):
    ctx = _ctx(runstate, repo=repo, pr=None)
    result = PrNoUnresolvedThreadsInvariant().check(ctx)
    assert isinstance(result, Fail)
    assert "without ctx.pr" in result.reason


def test_pr_no_unresolved_threads_is_merge_only():
    inv = PrNoUnresolvedThreadsInvariant()
    assert inv.subcommands == frozenset({"merge"})
    assert inv.kind == InvariantKind.PER_PR


# ─── pr.age_threshold ─────────────────────────────────────────────────────


def _freeze_now(monkeypatch, when: datetime) -> None:
    monkeypatch.setattr(catalog, "_utc_now", lambda: when)


def test_pr_age_threshold_pass_when_old_enough(monkeypatch, runstate, repo):
    """min_business_days=3, ready_since=10 days ago, ci-only no review →
    eligible via the time path. Uses ci-only + review_decision=None to
    avoid the APPROVED short-circuit so the time math is what's tested.
    """
    policy = Policy(defaults=Defaults(merge_policy="ci-only", min_business_days=3))
    pushed = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    pr = _real_pr(last_pushed_at=pushed, review_decision=None)
    _freeze_now(monkeypatch, datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc))
    ctx = _ctx(runstate, policy=policy, repo=repo, pr=pr)
    assert PrAgeThresholdInvariant().check(ctx) == Pass()


def test_pr_age_threshold_skip_when_too_recent(monkeypatch, runstate, repo):
    """ci-only, no approval, ready_since = today, min_business_days=3
    → not yet eligible (time path)."""
    policy = Policy(defaults=Defaults(merge_policy="ci-only", min_business_days=3))
    # Friday 2026-05-22 push; "now" 2026-05-25 Monday → only 1 business day
    pushed = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
    pr = _real_pr(last_pushed_at=pushed, review_decision=None)
    _freeze_now(monkeypatch, datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc))
    ctx = _ctx(runstate, policy=policy, repo=repo, pr=pr)
    result = PrAgeThresholdInvariant().check(ctx)
    assert isinstance(result, Skip)
    assert "eligible at" in result.reason


def test_pr_age_threshold_skip_when_zero_days_still_now(
    monkeypatch, runstate, repo
):
    """min_business_days=0 → eligible immediately, even on the day of
    push. ci-only + no review to exercise the time path."""
    policy = Policy(defaults=Defaults(merge_policy="ci-only", min_business_days=0))
    pushed = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    pr = _real_pr(last_pushed_at=pushed, review_decision=None)
    _freeze_now(monkeypatch, pushed)
    ctx = _ctx(runstate, policy=policy, repo=repo, pr=pr)
    assert PrAgeThresholdInvariant().check(ctx) == Pass()


def test_pr_age_threshold_skip_when_not_ready(runstate, repo):
    """If compute_ready_since returns None (e.g. mergeable_state DIRTY)
    AND there's no approval bypass, Skip with a clear reason."""
    pr = _real_pr(mergeable_state="DIRTY", review_decision=None)
    ctx = _ctx(runstate, repo=repo, pr=pr)
    result = PrAgeThresholdInvariant().check(ctx)
    assert isinstance(result, Skip)
    assert "not currently ready" in result.reason


def test_pr_age_threshold_ci_only_ignores_review(monkeypatch, runstate, repo):
    """ci-only policy → review_decision irrelevant for ready_since.

    A non-approved PR with no review (REVIEW_REQUIRED-or-None) still
    Passes purely on the time path when min_business_days=0.
    """
    policy = Policy(defaults=Defaults(merge_policy="ci-only", min_business_days=0))
    pushed = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    pr = _real_pr(review_decision=None, last_pushed_at=pushed)
    _freeze_now(monkeypatch, pushed)
    ctx = _ctx(runstate, policy=policy, repo=repo, pr=pr)
    assert PrAgeThresholdInvariant().check(ctx) == Pass()


# ─── pr.age_threshold APPROVED short-circuit (zk3r4nqp) ───────────────────


def test_pr_age_threshold_approved_short_circuits_under_strict(
    monkeypatch, runstate, repo
):
    """Strict policy + APPROVED review_decision → Pass immediately,
    regardless of how recent last_pushed_at is.

    Encodes the user's rule that approval is the merge signal we wait
    for, not a clock-restart event. Without this short-circuit a
    just-pushed-and-approved PR would Skip on age and force a 3-day wait.
    """
    policy = Policy(defaults=Defaults(merge_policy="strict", min_business_days=3))
    pushed = datetime(2026, 5, 28, 11, 0, 0, tzinfo=timezone.utc)
    pr = _real_pr(last_pushed_at=pushed, review_decision="APPROVED")
    _freeze_now(monkeypatch, datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc))
    ctx = _ctx(runstate, policy=policy, repo=repo, pr=pr)
    assert PrAgeThresholdInvariant().check(ctx) == Pass()


def test_pr_age_threshold_approved_short_circuits_under_ci_only(
    monkeypatch, runstate, repo
):
    """Under ci-only, an approval is a bonus (not required) — but if it
    happens, it still short-circuits the age gate. A 1-hour-old PR with
    green CI and an approval is eligible immediately under ci-only.
    """
    policy = Policy(defaults=Defaults(merge_policy="ci-only", min_business_days=3))
    pushed = datetime(2026, 5, 28, 11, 0, 0, tzinfo=timezone.utc)
    pr = _real_pr(last_pushed_at=pushed, review_decision="APPROVED")
    _freeze_now(monkeypatch, datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc))
    ctx = _ctx(runstate, policy=policy, repo=repo, pr=pr)
    assert PrAgeThresholdInvariant().check(ctx) == Pass()


def test_pr_age_threshold_changes_requested_does_not_short_circuit(
    monkeypatch, runstate, repo
):
    """Only APPROVED bypasses. CHANGES_REQUESTED falls through to the
    time path (which then also Skips since compute_ready_since returns
    None on a non-CLEAN review)."""
    policy = Policy(defaults=Defaults(merge_policy="ci-only", min_business_days=3))
    pushed = datetime(2026, 5, 28, 11, 0, 0, tzinfo=timezone.utc)
    pr = _real_pr(last_pushed_at=pushed, review_decision="CHANGES_REQUESTED")
    _freeze_now(monkeypatch, datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc))
    ctx = _ctx(runstate, policy=policy, repo=repo, pr=pr)
    result = PrAgeThresholdInvariant().check(ctx)
    assert isinstance(result, Skip)


def test_pr_age_threshold_fail_no_pr(runstate, repo):
    ctx = _ctx(runstate, repo=repo, pr=None)
    result = PrAgeThresholdInvariant().check(ctx)
    assert isinstance(result, Fail)


def test_pr_age_threshold_fail_no_repo(runstate):
    pr = _real_pr()
    ctx = _ctx(runstate, repo=None, pr=pr)
    result = PrAgeThresholdInvariant().check(ctx)
    assert isinstance(result, Fail)


def test_utc_now_helper_returns_aware_datetime():
    """The clock indirection used by pr.age_threshold."""
    now = catalog._utc_now()
    assert now.tzinfo is not None


# ─── pr.inactive (close-stale-only) ───────────────────────────────────────


def test_pr_inactive_pass_when_past_cooloff_threshold(monkeypatch, runstate, repo):
    """A PR untouched past stale_cooloff_days is a close-stale candidate.

    Threshold is stale_cooloff_days (NOT stale_age_days) so that warned
    PRs in their cooloff window — whose updated_at is anchored at the
    warning timestamp — still pass this gate and reach the handler. The
    handler enforces stale_age_days for the warn decision specifically.
    """
    policy = Policy(defaults=Defaults(stale_cooloff_days=7))
    pr = _real_pr()
    old = datetime.now(timezone.utc) - timedelta(days=10)
    pr = PRInfo(**{**pr.__dict__, "updated_at": old})
    ctx = _ctx(runstate, policy=policy, repo=repo, pr=pr)
    _freeze_now(monkeypatch, datetime.now(timezone.utc))
    assert PrInactiveInvariant().check(ctx) == Pass()


def test_pr_inactive_skip_when_within_cooloff(monkeypatch, runstate, repo):
    """An active PR (updated within stale_cooloff_days) is Skipped."""
    policy = Policy(defaults=Defaults(stale_cooloff_days=7))
    recent = datetime.now(timezone.utc) - timedelta(days=3)
    pr = _real_pr()
    pr = PRInfo(**{**pr.__dict__, "updated_at": recent})
    ctx = _ctx(runstate, policy=policy, repo=repo, pr=pr)
    _freeze_now(monkeypatch, datetime.now(timezone.utc))
    result = PrInactiveInvariant().check(ctx)
    assert isinstance(result, Skip)
    assert "3 days ago" in result.reason


def test_pr_inactive_skip_when_stale_policy_never(monkeypatch, runstate, repo):
    """stale_policy=never opts the repo out entirely, even if inactive."""
    policy = Policy(
        defaults=Defaults(stale_age_days=60, stale_policy="never"),
    )
    old = datetime.now(timezone.utc) - timedelta(days=100)
    pr = _real_pr()
    pr = PRInfo(**{**pr.__dict__, "updated_at": old})
    ctx = _ctx(runstate, policy=policy, repo=repo, pr=pr)
    _freeze_now(monkeypatch, datetime.now(timezone.utc))
    result = PrInactiveInvariant().check(ctx)
    assert isinstance(result, Skip)
    assert "never" in result.reason


def test_pr_inactive_per_repo_never_override(monkeypatch, runstate, repo):
    """Per-repo stale_policy=never beats defaults.stale_policy."""
    policy = Policy(
        defaults=Defaults(stale_age_days=60, stale_policy="warn-and-close"),
        repos={"dhh1128/gitbulk": RepoOverride(stale_policy="never")},
    )
    old = datetime.now(timezone.utc) - timedelta(days=100)
    pr = _real_pr()
    pr = PRInfo(**{**pr.__dict__, "updated_at": old})
    ctx = _ctx(runstate, policy=policy, repo=repo, pr=pr)
    _freeze_now(monkeypatch, datetime.now(timezone.utc))
    result = PrInactiveInvariant().check(ctx)
    assert isinstance(result, Skip)


def test_pr_inactive_warn_only_still_passes_inactive_gate(
    monkeypatch, runstate, repo
):
    """warn-only doesn't disable close-stale processing — only ``never`` does.
    The handler treats warn-only by suppressing the close action, but the
    invariant still passes so the warn pathway can run.
    """
    policy = Policy(
        defaults=Defaults(stale_cooloff_days=7, stale_policy="warn-only"),
    )
    old = datetime.now(timezone.utc) - timedelta(days=100)
    pr = _real_pr()
    pr = PRInfo(**{**pr.__dict__, "updated_at": old})
    ctx = _ctx(runstate, policy=policy, repo=repo, pr=pr)
    _freeze_now(monkeypatch, datetime.now(timezone.utc))
    assert PrInactiveInvariant().check(ctx) == Pass()


def test_pr_inactive_fail_no_pr(runstate, repo):
    ctx = _ctx(runstate, repo=repo, pr=None)
    result = PrInactiveInvariant().check(ctx)
    assert isinstance(result, Fail)


def test_pr_inactive_fail_no_repo(runstate):
    pr = _real_pr()
    ctx = _ctx(runstate, repo=None, pr=pr)
    result = PrInactiveInvariant().check(ctx)
    assert isinstance(result, Fail)


def test_pr_inactive_is_close_stale_only():
    inv = PrInactiveInvariant()
    assert inv.subcommands == frozenset({"close-stale"})
    assert inv.kind == InvariantKind.PER_PR


# ─── pr.needs_rebase (rebase-pr-only) ──────────────────────────────────────


def test_pr_needs_rebase_pass_on_behind(runstate, repo):
    pr = _real_pr(mergeable_state="BEHIND")
    ctx = _ctx(runstate, repo=repo, pr=pr)
    assert PrNeedsRebaseInvariant().check(ctx) == Pass()


def test_pr_needs_rebase_pass_on_dirty(runstate, repo):
    pr = _real_pr(mergeable_state="DIRTY")
    ctx = _ctx(runstate, repo=repo, pr=pr)
    assert PrNeedsRebaseInvariant().check(ctx) == Pass()


@pytest.mark.parametrize("state", ["CLEAN", "BLOCKED", "UNKNOWN", "UNSTABLE", "HAS_HOOKS"])
def test_pr_needs_rebase_skip_on_other_states(runstate, repo, state):
    pr = _real_pr(mergeable_state=state)
    ctx = _ctx(runstate, repo=repo, pr=pr)
    result = PrNeedsRebaseInvariant().check(ctx)
    assert isinstance(result, Skip)
    assert "does not warrant a rebase" in result.reason


def test_pr_needs_rebase_skip_on_none_state(runstate, repo):
    pr = _real_pr(mergeable_state=None)
    ctx = _ctx(runstate, repo=repo, pr=pr)
    assert isinstance(PrNeedsRebaseInvariant().check(ctx), Skip)


def test_pr_needs_rebase_fail_no_pr(runstate, repo):
    ctx = _ctx(runstate, repo=repo, pr=None)
    assert isinstance(PrNeedsRebaseInvariant().check(ctx), Fail)


def test_pr_needs_rebase_is_rebase_pr_only():
    inv = PrNeedsRebaseInvariant()
    assert inv.subcommands == frozenset({"rebase-pr"})
    assert inv.kind == InvariantKind.PER_PR
