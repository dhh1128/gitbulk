"""End-to-end tests for ``gitbulk merge`` (Phase 5).

Pipeline: invariants → eligible-PR filtering → (dry-run gate) →
``gh.merge_pr`` calls → result recording → exit code. Every gh call
goes through :class:`FakeGHClient`; no network in tests.

The merge handler does NOT touch local clones, so we don't need the
fake-clones machinery dispatch tests use. We DO need a fresh org cache
(consumed by ``org.members.fresh``) and a fresh local policy / repos
config — same as the dispatch fixtures.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from gitbulk import paths, sentinel
from gitbulk.cli import main
from gitbulk.commands import merge as merge_mod
from gitbulk.commands.merge import (
    EXIT_ATTENTION_NEEDED,
    EXIT_INVARIANT_SKIPPED,
    EXIT_OK,
    EXIT_OVERRIDES_APPLIED,
    EXIT_STRUCTURAL_FAILURE,
    _build_summary_md,
    _runid_from_run_dir,
    merge_handler,
)
from gitbulk.gh import FakeGHClient, GHError
from gitbulk.invariants import catalog as _catalog
from gitbulk.locks import LockTimeoutError
from gitbulk.org_members_cache import CachedMembers, save_cache
from gitbulk.pr_info import PRInfo


# ─── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_xdg(monkeypatch, tmp_path):
    cfg = tmp_path / "config"
    cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    paths.ensure_directories()
    return tmp_path


@pytest.fixture
def code_root(tmp_path):
    root = tmp_path / "code"
    root.mkdir()
    return root


@pytest.fixture
def write_config(isolated_xdg, code_root):
    """Write gitbulk.yaml + repos.txt. min_business_days=0 by default so
    the age_threshold invariant doesn't require a freeze-time monkeypatch
    in every test."""

    def _write(*, repos_slugs, defaults_extra=None, repo_overrides=None, bots=None):
        cfg_dir = paths.config_dir()
        cfg_dir.mkdir(parents=True, exist_ok=True)
        defaults = {
            "retain_runs": 5,
            "min_business_days": 0,
        }
        if defaults_extra:
            defaults.update(defaults_extra)
        policy_yaml: dict = {"defaults": defaults}
        policy_yaml["humans"] = {"org": "provenant-dev", "cache_ttl_hours": 24}
        if bots:
            policy_yaml["bots"] = list(bots)
        if repo_overrides:
            policy_yaml["repos"] = repo_overrides
        (cfg_dir / "gitbulk.yaml").write_text(yaml.safe_dump(policy_yaml))
        repos_txt = "\n".join(repos_slugs) + ("\n" if repos_slugs else "")
        (cfg_dir / "repos.txt").write_text(repos_txt)
        # Materialize the clone directories so RepoEntry validation passes
        # (load_repos checks local_path existence). These directories are
        # EMPTY — merge never touches them, so that's enough.
        for slug in repos_slugs:
            _, name = slug.split("/", 1)
            (code_root / name).mkdir(parents=True, exist_ok=True)
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


def _make_pr(
    *,
    slug: str,
    number: int,
    author: str = "dhh1128",
    base_ref: str = "main",
    title: str | None = None,
    head_sha: str = "a" * 40,
    mergeable_state: str | None = "CLEAN",
    checks_status: str | None = "SUCCESS",
    review_decision: str | None = "APPROVED",
    last_pushed_at: datetime | None = None,
) -> PRInfo:
    if last_pushed_at is None:
        last_pushed_at = datetime.now(timezone.utc) - timedelta(days=14)
    return PRInfo(
        slug=slug,
        number=number,
        title=title or f"PR #{number}",
        url=f"https://github.com/{slug}/pull/{number}",
        author=author,
        base_ref=base_ref,
        head_ref=f"feature/{number}",
        head_sha=head_sha,
        state="OPEN",
        is_draft=False,
        mergeable_state=mergeable_state,
        created_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc),
        last_pushed_at=last_pushed_at,
        labels=(),
        review_decision=review_decision,
        checks_status=checks_status,
    )


def _make_args(*, apply=False, code_root=None, skip_check=None, refresh_org_members=False,
               org=None, repo=None, base=None, mergeable_state=None, author=None,
               filter=None, approve=False, approve_author=None):
    return argparse.Namespace(
        subcommand="merge",
        apply=apply,
        approve=approve,
        approve_author=list(approve_author) if approve_author else None,
        code_root=str(code_root) if code_root else None,
        skip_check=list(skip_check) if skip_check else None,
        refresh_org_members=refresh_org_members,
        org=org,
        repo=repo,
        base=base,
        mergeable_state=mergeable_state,
        author=author,
        filter=filter,
    )


# ─── Dry-run path ──────────────────────────────────────────────────────────


def test_dry_run_two_eligible_prs_lists_them_no_merge_calls(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha", "dhh1128/beta"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr1 = _make_pr(slug="dhh1128/alpha", number=1)
    pr2 = _make_pr(slug="dhh1128/beta", number=2)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={
            "dhh1128/alpha": "main",
            "dhh1128/beta": "main",
        },
        my_open_prs={
            "dhh1128/alpha": [pr1],
            "dhh1128/beta": [pr2],
        },
    )
    monkeypatch.setattr(
        "gitbulk.commands.merge.ProductionGHClient", lambda: fake
    )

    rc = merge_handler(_make_args(code_root=code_root))
    assert rc == EXIT_OK
    assert fake.call_count["merge_pr"] == 0
    assert not sentinel.has_attention()
    latest = paths.latest_run_symlink("merge").resolve()
    summary = (latest / "summary.md").read_text()
    assert "DRY-RUN" in summary
    assert "Would merge" in summary
    assert "dhh1128/alpha" in summary
    assert "dhh1128/beta" in summary
    manifest = yaml.safe_load((latest / "manifest.yaml").read_text())
    assert manifest["config_snapshot"]["apply"] is False
    assert manifest["config_snapshot"]["merge_method_default"] == "rebase"


def test_dry_run_no_eligible_prs_exit_ok(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.merge.ProductionGHClient", lambda: fake
    )
    rc = merge_handler(_make_args(code_root=code_root))
    assert rc == EXIT_OK
    summary = (paths.latest_run_symlink("merge").resolve() / "summary.md").read_text()
    assert "no eligible PRs" in summary


def test_dry_run_org_filter_prunes_and_reports_filter_line(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["provenant-dev/alpha", "dhh1128/beta"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr1 = _make_pr(slug="provenant-dev/alpha", number=1)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={
            "provenant-dev/alpha": "main",
            "dhh1128/beta": "main",
        },
        my_open_prs={"provenant-dev/alpha": [pr1]},
    )
    monkeypatch.setattr(
        "gitbulk.commands.merge.ProductionGHClient", lambda: fake
    )

    rc = merge_handler(_make_args(code_root=code_root, org=["provenant-dev"]))
    assert rc == EXIT_OK
    summary = (paths.latest_run_symlink("merge").resolve() / "summary.md").read_text()
    assert "Filtered" in summary
    assert "org=provenant-dev" in summary
    assert "1 repos" in summary
    # The excluded repo is absent from the run.
    assert "dhh1128/beta" not in summary


# ─── --apply happy path ────────────────────────────────────────────────────


def test_apply_two_eligible_prs_both_merged(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha", "dhh1128/beta"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr1 = _make_pr(slug="dhh1128/alpha", number=1)
    pr2 = _make_pr(slug="dhh1128/beta", number=2)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={
            "dhh1128/alpha": "main",
            "dhh1128/beta": "main",
        },
        my_open_prs={
            "dhh1128/alpha": [pr1],
            "dhh1128/beta": [pr2],
        },
        merge_responses={
            ("dhh1128/alpha", 1): {"merged": True},
            ("dhh1128/beta", 2): {"merged": True},
        },
    )
    monkeypatch.setattr(
        "gitbulk.commands.merge.ProductionGHClient", lambda: fake
    )

    rc = merge_handler(_make_args(apply=True, code_root=code_root))
    assert rc == EXIT_OK
    assert fake.call_count["merge_pr"] == 2
    assert not sentinel.has_attention()
    # Both calls used the default merge method (`rebase` per gji4dyze)
    # and delete_branch=True (remote PR branch cleanup).
    for call in fake.merge_calls:
        assert call["method"] == "rebase"
        assert call["delete_branch"] is True
    latest = paths.latest_run_symlink("merge").resolve()
    summary = (latest / "summary.md").read_text()
    assert "APPLY" in summary
    assert "merged" in summary
    state = yaml.safe_load((latest / "state.yaml").read_text())
    assert set(state["repos"].keys()) == {"dhh1128/alpha", "dhh1128/beta"}
    for slug in state["repos"]:
        pr_states = state["repos"][slug]["prs"]
        assert len(pr_states) == 1
        assert pr_states[0]["merged"] is True


# ─── --apply with one not-ready PR → skipped by invariants ─────────────────


def test_apply_one_not_ready_skipped_by_invariants(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """A PR with mergeable_state=DIRTY is filtered out by the
    pr.mergeable_state_clean invariant; the other ready PR is merged."""
    write_config(repos_slugs=["dhh1128/alpha", "dhh1128/beta"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr_ready = _make_pr(slug="dhh1128/alpha", number=1)
    pr_dirty = _make_pr(
        slug="dhh1128/beta", number=2, mergeable_state="DIRTY"
    )
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={
            "dhh1128/alpha": "main",
            "dhh1128/beta": "main",
        },
        my_open_prs={
            "dhh1128/alpha": [pr_ready],
            "dhh1128/beta": [pr_dirty],
        },
        merge_responses={("dhh1128/alpha", 1): {"merged": True}},
    )
    monkeypatch.setattr(
        "gitbulk.commands.merge.ProductionGHClient", lambda: fake
    )
    rc = merge_handler(_make_args(apply=True, code_root=code_root))
    assert rc == EXIT_OK
    assert fake.call_count["merge_pr"] == 1
    assert fake.merge_calls[0]["slug"] == "dhh1128/alpha"
    state = yaml.safe_load(
        (paths.latest_run_symlink("merge").resolve() / "state.yaml").read_text()
    )
    beta_prs = state["repos"]["dhh1128/beta"]["prs"]
    assert beta_prs[0]["eligible"] is False


# ─── --apply with one merge_pr failure → exit 2 ────────────────────────────


def test_apply_one_merge_failure_exit_attention(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """When one merge_pr raises GHError, the other PR still attempts to
    merge and the run exits 2 (attention)."""
    write_config(repos_slugs=["dhh1128/alpha", "dhh1128/beta"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr1 = _make_pr(slug="dhh1128/alpha", number=1)
    pr2 = _make_pr(slug="dhh1128/beta", number=2)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={
            "dhh1128/alpha": "main",
            "dhh1128/beta": "main",
        },
        my_open_prs={
            "dhh1128/alpha": [pr1],
            "dhh1128/beta": [pr2],
        },
        merge_responses={
            ("dhh1128/alpha", 1): GHError("branch protection blocked merge"),
            ("dhh1128/beta", 2): {"merged": True},
        },
    )
    monkeypatch.setattr(
        "gitbulk.commands.merge.ProductionGHClient", lambda: fake
    )
    rc = merge_handler(_make_args(apply=True, code_root=code_root))
    assert rc == EXIT_ATTENTION_NEEDED
    # Both PRs were attempted (the failure didn't short-circuit beta).
    assert fake.call_count["merge_pr"] == 2
    parsed = sentinel.parse_attention()
    assert parsed is not None
    assert parsed["exit_code"] == EXIT_ATTENTION_NEEDED
    latest = paths.latest_run_symlink("merge").resolve()
    summary = (latest / "summary.md").read_text()
    assert "FAILED" in summary
    # errors.log captured the failure.
    errors = [
        json.loads(line)
        for line in (latest / "errors.log").read_text().splitlines()
        if line.strip()
    ]
    assert any("merge_pr failed" in e["message"] for e in errors)
    state = yaml.safe_load((latest / "state.yaml").read_text())
    alpha_prs = state["repos"]["dhh1128/alpha"]["prs"]
    assert alpha_prs[0]["merged"] is False
    assert "branch protection" in alpha_prs[0]["error"]


# ─── Lock timeout → exit 1, no sentinel ────────────────────────────────────


def test_lock_timeout_exit_structural(
    monkeypatch, isolated_xdg, code_root, write_config, capsys, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])

    class _BoomLock:
        def __enter__(self):
            raise LockTimeoutError(
                paths.global_lock_file(),
                {
                    "pid": 999,
                    "started_at": "1970-01-01T00:00:00+00:00",
                    "subcommand": "merge",
                    "alive": False,
                },
            )

        def __exit__(self, *a):  # pragma: no cover — never reached
            return False

    monkeypatch.setattr(
        "gitbulk.commands.merge.global_lock", lambda *a, **kw: _BoomLock()
    )
    fake = FakeGHClient(user={"login": "dhh1128"})
    monkeypatch.setattr(
        "gitbulk.commands.merge.ProductionGHClient", lambda: fake
    )
    rc = merge_handler(_make_args(code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE
    assert not sentinel.has_attention()
    err = capsys.readouterr().err
    assert "timed out" in err


# ─── merge_policy=never → no PR merges ─────────────────────────────────────


def test_merge_policy_never_drops_all_prs(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """When defaults.merge_policy=never, the pr.approved_per_policy
    invariant Skips every PR; nothing enters eligible_prs."""
    write_config(
        repos_slugs=["dhh1128/alpha"],
        defaults_extra={"merge_policy": "never"},
    )
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr = _make_pr(slug="dhh1128/alpha", number=1)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
    )
    monkeypatch.setattr(
        "gitbulk.commands.merge.ProductionGHClient", lambda: fake
    )
    rc = merge_handler(_make_args(apply=True, code_root=code_root))
    assert rc == EXIT_OK
    assert fake.call_count["merge_pr"] == 0
    state = yaml.safe_load(
        (paths.latest_run_symlink("merge").resolve() / "state.yaml").read_text()
    )
    pr_state = state["repos"]["dhh1128/alpha"]["prs"][0]
    assert pr_state["eligible"] is False


# ─── Universal Fail (gh not authenticated) → exit 1 ────────────────────────


def test_universal_fail_exit_structural(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient()  # no user → authenticated_user raises
    monkeypatch.setattr(
        "gitbulk.commands.merge.ProductionGHClient", lambda: fake
    )
    rc = merge_handler(_make_args(code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE
    latest = paths.latest_run_symlink("merge").resolve()
    summary = (latest / "summary.md").read_text()
    assert "FAILED" in summary


# ─── Per-repo Skip (github.reachable) drops repo → exit 3 ─────────────────


def test_per_repo_skip_drops_repo_exit_skipped(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha", "dhh1128/beta"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        # default_branches missing beta → github.reachable Skip
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.merge.ProductionGHClient", lambda: fake
    )
    rc = merge_handler(_make_args(code_root=code_root))
    assert rc == EXIT_INVARIANT_SKIPPED
    parsed = sentinel.parse_attention()
    assert parsed is not None
    assert parsed["exit_code"] == EXIT_INVARIANT_SKIPPED


# ─── --skip-check applied, no failures, no skips → exit 4 ─────────────────


def test_skip_check_audit_signal_exit_overrides(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.merge.ProductionGHClient", lambda: fake
    )
    rc = merge_handler(
        _make_args(code_root=code_root, skip_check=["pr.age_threshold"])
    )
    assert rc == EXIT_OVERRIDES_APPLIED
    assert not sentinel.has_attention()
    latest = paths.latest_run_symlink("merge").resolve()
    errors = (latest / "errors.log").read_text().splitlines()
    assert any(
        json.loads(line).get("level") == "WARNING" for line in errors
    )


# ─── gh.my_open_prs raises → exit 1 ────────────────────────────────────────


def test_gh_pr_fetch_error_exit_structural(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        # my_open_prs NOT configured → raises GHError
    )
    monkeypatch.setattr(
        "gitbulk.commands.merge.ProductionGHClient", lambda: fake
    )
    rc = merge_handler(_make_args(code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE


# ─── Per-repo Fail (forced) → exit 1 ───────────────────────────────────────


def test_per_repo_fail_aborts(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    from gitbulk.invariants.base import Fail as _Fail

    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.merge.ProductionGHClient", lambda: fake
    )
    monkeypatch.setattr(
        _catalog.GithubReachableInvariant,
        "check",
        lambda self, ctx: _Fail("forced fail"),
    )
    rc = merge_handler(_make_args(code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE


# ─── --apply with skipped repos & no failures → exit 3 ─────────────────────


def test_apply_with_skipped_repo_no_failures_exit_skipped(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha", "dhh1128/beta"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr = _make_pr(slug="dhh1128/alpha", number=1)
    # default_branches missing beta → skip.
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        merge_responses={("dhh1128/alpha", 1): {"merged": True}},
    )
    monkeypatch.setattr(
        "gitbulk.commands.merge.ProductionGHClient", lambda: fake
    )
    rc = merge_handler(_make_args(apply=True, code_root=code_root))
    assert rc == EXIT_INVARIANT_SKIPPED
    assert sentinel.has_attention()


# ─── Dry-run with skipped repo → exit 3 ────────────────────────────────────


def test_dry_run_skipped_repo_exit_skipped(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha", "dhh1128/beta"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.merge.ProductionGHClient", lambda: fake
    )
    rc = merge_handler(_make_args(code_root=code_root))
    assert rc == EXIT_INVARIANT_SKIPPED


# ─── PR not eligible due to age_threshold → not in eligible_prs ───────────


def test_age_threshold_filters_recent_pr(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """A PR pushed today with min_business_days=3 is not eligible.

    Uses ci-only + review_decision=None to exercise the time path of
    age_threshold rather than the APPROVED short-circuit (which would
    fast-track an approved PR straight through the gate per zk3r4nqp).
    """
    write_config(
        repos_slugs=["dhh1128/alpha"],
        defaults_extra={"merge_policy": "ci-only", "min_business_days": 3},
    )
    fresh_org_cache("provenant-dev", ["dhh1128"])
    # Pin "now" so the age check is deterministic.
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    pushed = datetime(2026, 5, 25, 9, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(_catalog, "_utc_now", lambda: now)
    pr = _make_pr(
        slug="dhh1128/alpha",
        number=1,
        last_pushed_at=pushed,
        review_decision=None,
    )
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
    )
    monkeypatch.setattr(
        "gitbulk.commands.merge.ProductionGHClient", lambda: fake
    )
    rc = merge_handler(_make_args(apply=True, code_root=code_root))
    assert rc == EXIT_OK
    assert fake.call_count["merge_pr"] == 0


# ─── per-repo merge_method override ───────────────────────────────────────


def test_per_repo_merge_method_override_passed_to_gh(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """If the policy has repos.<slug>.merge_method = "squash" and the
    default is "merge", the override must flow to gh.merge_pr(method=...).
    """
    write_config(
        repos_slugs=["dhh1128/alpha", "dhh1128/beta"],
        defaults_extra={"merge_method": "merge"},
        repo_overrides={"dhh1128/beta": {"merge_method": "squash"}},
    )
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr_alpha = _make_pr(slug="dhh1128/alpha", number=1)
    pr_beta = _make_pr(slug="dhh1128/beta", number=2)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={
            "dhh1128/alpha": "main",
            "dhh1128/beta": "main",
        },
        my_open_prs={
            "dhh1128/alpha": [pr_alpha],
            "dhh1128/beta": [pr_beta],
        },
        merge_responses={
            ("dhh1128/alpha", 1): {"merged": True},
            ("dhh1128/beta", 2): {"merged": True},
        },
    )
    monkeypatch.setattr(
        "gitbulk.commands.merge.ProductionGHClient", lambda: fake
    )

    rc = merge_handler(_make_args(apply=True, code_root=code_root))
    assert rc == EXIT_OK
    by_slug = {(c["slug"], c["number"]): c["method"] for c in fake.merge_calls}
    assert by_slug[("dhh1128/alpha", 1)] == "merge"  # default
    assert by_slug[("dhh1128/beta", 2)] == "squash"  # override


def test_dry_run_shows_per_repo_method_when_different_from_default(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """Dry-run summary should annotate a PR with [method=`X`] when its
    repo's effective method differs from the default, so the user can
    eyeball overrides before flipping --apply."""
    write_config(
        repos_slugs=["dhh1128/alpha"],
        defaults_extra={"merge_method": "merge"},
        repo_overrides={"dhh1128/alpha": {"merge_method": "rebase"}},
    )
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr = _make_pr(slug="dhh1128/alpha", number=1)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
    )
    monkeypatch.setattr(
        "gitbulk.commands.merge.ProductionGHClient", lambda: fake
    )

    merge_handler(_make_args(code_root=code_root))
    summary = (paths.latest_run_symlink("merge").resolve() / "summary.md").read_text()
    assert "Default merge method: `merge`" in summary
    assert "[method=`rebase`]" in summary


# ─── one-merge-per-repo-per-run guardrail (G1) ────────────────────────────


def test_apply_two_same_repo_prs_merges_one_defers_other(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """Two PRs in the same repo, both eligible → merge the lower-numbered,
    defer the higher-numbered. The merged one gets a merge_commit_sha;
    the deferred one is recorded with deferred=...
    """
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr_low = _make_pr(slug="dhh1128/alpha", number=3, head_sha="a" * 40)
    pr_high = _make_pr(slug="dhh1128/alpha", number=7, head_sha="b" * 40)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr_high, pr_low]},  # order shouldn't matter
        merge_responses={("dhh1128/alpha", 3): {"merged": True}},
        merge_commit_shas={("dhh1128/alpha", 3): "merge" + "0" * 35},
    )
    monkeypatch.setattr(
        "gitbulk.commands.merge.ProductionGHClient", lambda: fake
    )
    rc = merge_handler(_make_args(apply=True, code_root=code_root))
    assert rc == EXIT_OK
    # Only ONE merge_pr call, for the lower-numbered PR.
    assert fake.call_count["merge_pr"] == 1
    assert fake.merge_calls[0]["number"] == 3
    # State + summary reflect both PRs.
    latest = paths.latest_run_symlink("merge").resolve()
    state = yaml.safe_load((latest / "state.yaml").read_text())
    prs = state["repos"]["dhh1128/alpha"]["prs"]
    assert len(prs) == 2
    by_num = {p["number"]: p for p in prs}
    assert by_num[3]["merged"] is True
    assert by_num[3]["merge_commit_sha"].startswith("merge")
    assert "deferred" in by_num[7]
    summary = (latest / "summary.md").read_text()
    assert "Deferred to next run" in summary
    assert "#7" in summary


def test_dry_run_two_same_repo_prs_lists_deferred(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """Dry-run mirrors the apply-time guardrail so the user can preview
    what will actually fire."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr1 = _make_pr(slug="dhh1128/alpha", number=1)
    pr2 = _make_pr(slug="dhh1128/alpha", number=2, head_sha="b" * 40)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr1, pr2]},
    )
    monkeypatch.setattr(
        "gitbulk.commands.merge.ProductionGHClient", lambda: fake
    )
    rc = merge_handler(_make_args(code_root=code_root))
    assert rc == EXIT_OK
    summary = (paths.latest_run_symlink("merge").resolve() / "summary.md").read_text()
    # Both sections present: would-merge for #1, deferred for #2.
    assert "Would merge" in summary
    assert "#1" in summary
    assert "Deferred to next run" in summary
    assert "#2" in summary


def test_apply_two_repos_one_eligible_each_both_merged(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """Cross-repo: two repos with one eligible PR each → both merge in
    a single run (guardrail is per-repo, not per-run)."""
    write_config(repos_slugs=["dhh1128/alpha", "dhh1128/beta"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr_a = _make_pr(slug="dhh1128/alpha", number=1)
    pr_b = _make_pr(slug="dhh1128/beta", number=1)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main", "dhh1128/beta": "main"},
        my_open_prs={"dhh1128/alpha": [pr_a], "dhh1128/beta": [pr_b]},
        merge_responses={
            ("dhh1128/alpha", 1): {"merged": True},
            ("dhh1128/beta", 1): {"merged": True},
        },
        merge_commit_shas={
            ("dhh1128/alpha", 1): "a" + "1" * 39,
            ("dhh1128/beta", 1): "b" + "1" * 39,
        },
    )
    monkeypatch.setattr(
        "gitbulk.commands.merge.ProductionGHClient", lambda: fake
    )
    rc = merge_handler(_make_args(apply=True, code_root=code_root))
    assert rc == EXIT_OK
    assert fake.call_count["merge_pr"] == 2


# ─── CLI smoke through main() ──────────────────────────────────────────────


def test_main_merge_default_is_dry_run(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """``gitbulk merge`` without --apply must default to dry-run."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.merge.ProductionGHClient", lambda: fake
    )
    rc = main(["merge", "--code-root", str(code_root)])
    assert rc == EXIT_OK
    latest = paths.latest_run_symlink("merge").resolve()
    manifest = yaml.safe_load((latest / "manifest.yaml").read_text())
    assert manifest["config_snapshot"]["apply"] is False


def test_main_merge_apply_flag_passes_through(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr = _make_pr(slug="dhh1128/alpha", number=1)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        merge_responses={("dhh1128/alpha", 1): {"merged": True}},
    )
    monkeypatch.setattr(
        "gitbulk.commands.merge.ProductionGHClient", lambda: fake
    )
    rc = main(["merge", "--apply", "--code-root", str(code_root)])
    assert rc == EXIT_OK
    assert fake.call_count["merge_pr"] == 1


# ─── Helper unit tests ─────────────────────────────────────────────────────


def test_runid_from_run_dir_simple(tmp_path):
    d = tmp_path / "20260528T010203Z-merge"
    assert _runid_from_run_dir(d) == "20260528T010203Z"


def test_runid_from_run_dir_fallback(tmp_path):
    d = tmp_path / "20260528T010203Z-something-else"
    assert _runid_from_run_dir(d) == "20260528T010203Z-something"


def test_build_summary_md_dry_run_eligible(isolated_xdg, code_root, write_config):
    from gitbulk.config.policy import load_policy
    from gitbulk.config.repos import RepoEntry

    write_config(repos_slugs=["x/a"])
    policy = load_policy()
    repo = RepoEntry(
        slug="x/a", owner="x", name="a", local_path=code_root / "a",
        source_line=1,
    )
    pr = _make_pr(slug="x/a", number=3)
    md = _build_summary_md(
        policy,
        all_repos=[repo],
        passing_repos=[repo],
        skipped_repos=[],
        eligible_prs=[("x/a", pr)],
        merge_results=None,
        apply=False,
    )
    assert "DRY-RUN" in md
    assert "Would merge" in md
    assert "#3" in md


def test_build_summary_md_apply_with_results(isolated_xdg, code_root, write_config):
    from gitbulk.config.policy import load_policy
    from gitbulk.config.repos import RepoEntry

    write_config(repos_slugs=["x/a"])
    policy = load_policy()
    repo = RepoEntry(
        slug="x/a", owner="x", name="a", local_path=code_root / "a",
        source_line=1,
    )
    pr = _make_pr(slug="x/a", number=3)
    md = _build_summary_md(
        policy,
        all_repos=[repo],
        passing_repos=[repo],
        skipped_repos=[],
        eligible_prs=[("x/a", pr)],
        merge_results=[
            {"slug": "x/a", "number": 3, "merged": True}
        ],
        apply=True,
    )
    assert "APPLY" in md
    assert "Merge results" in md
    assert "merged" in md


def test_build_summary_md_apply_with_failure(isolated_xdg, code_root, write_config):
    from gitbulk.config.policy import load_policy
    from gitbulk.config.repos import RepoEntry

    write_config(repos_slugs=["x/a"])
    policy = load_policy()
    repo = RepoEntry(
        slug="x/a", owner="x", name="a", local_path=code_root / "a",
        source_line=1,
    )
    pr = _make_pr(slug="x/a", number=3)
    md = _build_summary_md(
        policy,
        all_repos=[repo],
        passing_repos=[repo],
        skipped_repos=[],
        eligible_prs=[("x/a", pr)],
        merge_results=[
            {"slug": "x/a", "number": 3, "merged": False, "error": "boom"}
        ],
        apply=True,
    )
    assert "FAILED" in md
    assert "boom" in md


def test_build_summary_md_apply_missing_result(isolated_xdg, code_root, write_config):
    """Eligible PR with no matching result → 'no result recorded' line."""
    from gitbulk.config.policy import load_policy
    from gitbulk.config.repos import RepoEntry

    write_config(repos_slugs=["x/a"])
    policy = load_policy()
    repo = RepoEntry(
        slug="x/a", owner="x", name="a", local_path=code_root / "a",
        source_line=1,
    )
    pr = _make_pr(slug="x/a", number=3)
    md = _build_summary_md(
        policy,
        all_repos=[repo],
        passing_repos=[repo],
        skipped_repos=[],
        eligible_prs=[("x/a", pr)],
        merge_results=[],
        apply=True,
    )
    assert "no result recorded" in md


def test_state_for_repo_apply_with_missing_match_branch():
    """Exercise the ``match is None`` branch of _state_for_repo.

    In normal apply-mode execution every eligible PR yields a merge
    result; this defensive branch only fires if a caller mismatches
    the eligible/result lists. We exercise it directly so coverage
    sees both arms of the conditional.
    """
    from gitbulk.commands.merge import _state_for_repo
    from gitbulk.pr_info import PRInfo

    pr = PRInfo(
        slug="x/a", number=99, title="t",
        url="u", author="a", base_ref="main", head_ref="h",
        head_sha="s", state="OPEN", is_draft=False,
        mergeable_state="CLEAN",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_pushed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        labels=(), review_decision="APPROVED",
        checks_status="SUCCESS",
    )
    out = _state_for_repo(
        "x/a",
        eligible_prs=[("x/a", pr)],
        pr_skips_by_repo={},
        repo_merge_results=[],  # no match for pr.number=99
        apply=True,
    )
    # pr appears as eligible but without a merged key (defensive branch).
    assert out["prs"][0]["eligible"] is True
    assert "merged" not in out["prs"][0]


def test_apply_all_repos_skipped_no_pr_fetch(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """Every repo skipped → passing_repos empty → prs_by_repo = {} branch."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    # default_branches missing alpha → github.reachable Skip
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={},
    )
    monkeypatch.setattr(
        "gitbulk.commands.merge.ProductionGHClient", lambda: fake
    )
    rc = merge_handler(_make_args(apply=True, code_root=code_root))
    assert rc == EXIT_INVARIANT_SKIPPED
    # my_open_prs was NEVER called.
    assert fake.call_count["my_open_prs"] == 0


def test_apply_skip_check_no_failures_no_skips_exit_overrides(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """--apply with --skip-check used, no failures, no skipped repos →
    exit 4. Exercises the apply-mode skip_list exit-code branch."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr = _make_pr(slug="dhh1128/alpha", number=1)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        merge_responses={("dhh1128/alpha", 1): {"merged": True}},
    )
    monkeypatch.setattr(
        "gitbulk.commands.merge.ProductionGHClient", lambda: fake
    )
    rc = merge_handler(
        _make_args(
            apply=True,
            code_root=code_root,
            skip_check=["pr.author_known"],
        )
    )
    assert rc == EXIT_OVERRIDES_APPLIED
    assert not sentinel.has_attention()


def test_apply_passing_repo_with_no_prs_skips_state_record(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """--apply with a passing repo that has no PRs at all → state.yaml
    does NOT record an entry for that repo (no eligible, no skipped).
    Covers the ``if repo_results or repo_skips`` False branch."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.merge.ProductionGHClient", lambda: fake
    )
    rc = merge_handler(_make_args(apply=True, code_root=code_root))
    assert rc == EXIT_OK
    state = yaml.safe_load(
        (paths.latest_run_symlink("merge").resolve() / "state.yaml").read_text()
    )
    assert state.get("repos", {}) == {}


def test_build_summary_md_lists_skipped_repos(isolated_xdg, code_root, write_config):
    from gitbulk.config.policy import load_policy

    write_config(repos_slugs=["x/a"])
    policy = load_policy()
    md = _build_summary_md(
        policy,
        all_repos=[],
        passing_repos=[],
        skipped_repos=[("x/a", "github not reachable")],
        eligible_prs=[],
        merge_results=None,
        apply=False,
    )
    assert "Skipped repos" in md
    assert "x/a" in md
    assert "github not reachable" in md


# ─── Skipped repos.txt entries surfaced in merge ──────────────────────────


def test_merge_skipped_entries_surface_in_summary(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """A bad entry in repos.txt appears in merge summary.md."""
    cfg_dir = paths.config_dir()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "gitbulk.yaml").write_text(yaml.safe_dump({
        "defaults": {"retain_runs": 5, "min_business_days": 0},
        "humans": {"org": "provenant-dev", "cache_ttl_hours": 24},
    }))
    (cfg_dir / "repos.txt").write_text(
        "dhh1128/alpha\n"
        "/nonexistent/bad-entry\n"
    )
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.merge.ProductionGHClient", lambda: fake
    )
    rc = merge_handler(_make_args(code_root=code_root))
    assert rc == EXIT_INVARIANT_SKIPPED
    summary = (paths.latest_run_symlink("merge").resolve() / "summary.md").read_text()
    assert "Skipped repos.txt entries" in summary
    assert "line 2" in summary


# ─── --approve auto-approve (node aprmn5kq) ────────────────────────────────


def _bot_pr(slug, number, *, author="dependabot[bot]", review_decision="REVIEW_REQUIRED",
            last_pushed_at=None):
    return _make_pr(
        slug=slug,
        number=number,
        author=author,
        review_decision=review_decision,
        last_pushed_at=last_pushed_at,
    )


def test_dry_run_approve_green_unapproved_bot_pr_reports_would_auto_approve(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"], bots=["dependabot[bot]"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr = _bot_pr("dhh1128/alpha", 15)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        repo_permissions={"dhh1128/alpha": "maintain"},
    )
    monkeypatch.setattr("gitbulk.commands.merge.ProductionGHClient", lambda: fake)

    rc = merge_handler(_make_args(code_root=code_root, approve=True))
    assert rc == EXIT_OK
    # DRY-RUN posts NOTHING.
    assert fake.call_count["approve_pr"] == 0
    assert fake.call_count["merge_pr"] == 0
    summary = (paths.latest_run_symlink("merge").resolve() / "summary.md").read_text()
    assert "would auto-approve + merge" in summary
    assert "dhh1128/alpha" in summary


def test_apply_approve_bot_pr_approves_then_merges_in_order(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"], bots=["dependabot[bot]"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr = _bot_pr("dhh1128/alpha", 15)

    order: list[str] = []

    class _RecordingFake(FakeGHClient):
        def approve_pr(self, slug, number, *, body=None, timeout=None):
            order.append("approve")
            return super().approve_pr(slug, number, body=body, timeout=timeout)

        def merge_pr(self, slug, number, *, method="merge", delete_branch=True, timeout=None):
            order.append("merge")
            return super().merge_pr(
                slug, number, method=method, delete_branch=delete_branch, timeout=timeout
            )

    fake = _RecordingFake(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        repo_permissions={"dhh1128/alpha": "maintain"},
        approve_responses={("dhh1128/alpha", 15): {}},
        merge_responses={("dhh1128/alpha", 15): {"merged": True}},
        merge_commit_shas={("dhh1128/alpha", 15): "deadbeef"},
    )
    monkeypatch.setattr("gitbulk.commands.merge.ProductionGHClient", lambda: fake)

    rc = merge_handler(_make_args(code_root=code_root, apply=True, approve=True))
    assert rc == EXIT_OK
    assert order == ["approve", "merge"]
    assert fake.approve_calls[0]["slug"] == "dhh1128/alpha"
    assert fake.approve_calls[0]["number"] == 15
    summary = (paths.latest_run_symlink("merge").resolve() / "summary.md").read_text()
    assert "auto-approved + merged" in summary


def test_apply_approve_records_audit_warning(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"], bots=["dependabot[bot]"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr = _bot_pr("dhh1128/alpha", 15)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        repo_permissions={"dhh1128/alpha": "write"},
        approve_responses={("dhh1128/alpha", 15): {}},
        merge_responses={("dhh1128/alpha", 15): {"merged": True}},
        merge_commit_shas={("dhh1128/alpha", 15): "abc"},
    )
    monkeypatch.setattr("gitbulk.commands.merge.ProductionGHClient", lambda: fake)
    rc = merge_handler(_make_args(code_root=code_root, apply=True, approve=True))
    assert rc == EXIT_OK
    # Audit trail: invariants.log carries an auto-approved WARNING line.
    err_log = (paths.latest_run_symlink("merge").resolve() / "errors.log").read_text()
    assert "auto-approved" in err_log
    assert "dhh1128/alpha#15" in err_log


def test_apply_approve_non_bot_without_whitelist_not_approved(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"], bots=["dependabot[bot]"])
    fresh_org_cache("provenant-dev", ["dhh1128", "alice"])
    pr = _bot_pr("dhh1128/alpha", 15, author="alice")
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128", "alice"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        repo_permissions={"dhh1128/alpha": "maintain"},
    )
    monkeypatch.setattr("gitbulk.commands.merge.ProductionGHClient", lambda: fake)
    rc = merge_handler(_make_args(code_root=code_root, apply=True, approve=True))
    # Nothing approved or merged; the unapproved PR is just a skip → exit 0.
    assert fake.call_count["approve_pr"] == 0
    assert fake.call_count["merge_pr"] == 0
    assert rc == EXIT_OK


def test_apply_approve_non_bot_with_whitelist_is_approved(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"], bots=["dependabot[bot]"])
    fresh_org_cache("provenant-dev", ["dhh1128", "alice"])
    pr = _bot_pr("dhh1128/alpha", 15, author="alice")
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128", "alice"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        repo_permissions={"dhh1128/alpha": "maintain"},
        approve_responses={("dhh1128/alpha", 15): {}},
        merge_responses={("dhh1128/alpha", 15): {"merged": True}},
        merge_commit_shas={("dhh1128/alpha", 15): "abc"},
    )
    monkeypatch.setattr("gitbulk.commands.merge.ProductionGHClient", lambda: fake)
    rc = merge_handler(
        _make_args(code_root=code_root, apply=True, approve=True, approve_author=["alice"])
    )
    assert rc == EXIT_OK
    assert fake.call_count["approve_pr"] == 1
    assert fake.call_count["merge_pr"] == 1


def test_apply_approve_self_authored_never_approved(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"], bots=["dhh1128"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    # viewer == author (dhh1128); even though dhh1128 is in bots, NOT-SELF wins.
    pr = _bot_pr("dhh1128/alpha", 15, author="dhh1128")
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        repo_permissions={"dhh1128/alpha": "maintain"},
    )
    monkeypatch.setattr("gitbulk.commands.merge.ProductionGHClient", lambda: fake)
    rc = merge_handler(_make_args(code_root=code_root, apply=True, approve=True))
    assert fake.call_count["approve_pr"] == 0
    assert fake.call_count["merge_pr"] == 0
    assert rc == EXIT_OK


@pytest.mark.parametrize("perm", ["read", "none", "triage"])
def test_apply_approve_insufficient_permission_not_approved(
    perm, monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"], bots=["dependabot[bot]"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr = _bot_pr("dhh1128/alpha", 15)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        repo_permissions={"dhh1128/alpha": perm},
    )
    monkeypatch.setattr("gitbulk.commands.merge.ProductionGHClient", lambda: fake)
    rc = merge_handler(_make_args(code_root=code_root, apply=True, approve=True))
    assert fake.call_count["approve_pr"] == 0
    assert fake.call_count["merge_pr"] == 0
    assert rc == EXIT_OK
    err_log = (paths.latest_run_symlink("merge").resolve() / "errors.log").read_text()
    assert "insufficient" in err_log


@pytest.mark.parametrize("perm", ["write", "maintain", "admin"])
def test_apply_approve_sufficient_permission_is_approved(
    perm, monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"], bots=["dependabot[bot]"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr = _bot_pr("dhh1128/alpha", 15)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        repo_permissions={"dhh1128/alpha": perm},
        approve_responses={("dhh1128/alpha", 15): {}},
        merge_responses={("dhh1128/alpha", 15): {"merged": True}},
        merge_commit_shas={("dhh1128/alpha", 15): "abc"},
    )
    monkeypatch.setattr("gitbulk.commands.merge.ProductionGHClient", lambda: fake)
    rc = merge_handler(_make_args(code_root=code_root, apply=True, approve=True))
    assert rc == EXIT_OK
    assert fake.call_count["approve_pr"] == 1
    assert fake.call_count["merge_pr"] == 1


def test_apply_approve_never_repo_not_approved(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(
        repos_slugs=["dhh1128/alpha"],
        bots=["dependabot[bot]"],
        repo_overrides={"dhh1128/alpha": {"merge_policy": "never"}},
    )
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr = _bot_pr("dhh1128/alpha", 15)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        repo_permissions={"dhh1128/alpha": "admin"},
    )
    monkeypatch.setattr("gitbulk.commands.merge.ProductionGHClient", lambda: fake)
    rc = merge_handler(_make_args(code_root=code_root, apply=True, approve=True))
    assert fake.call_count["approve_pr"] == 0
    assert fake.call_count["merge_pr"] == 0
    # never-repo PR is a skip → exit 0 (no repo skip / no failure).
    assert rc == EXIT_OK


def test_apply_approve_non_approval_blocker_not_auto_approvable(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"], bots=["dependabot[bot]"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    # checks PENDING → required_checks_green Skips → NOT sole-gate.
    pr = _make_pr(
        slug="dhh1128/alpha", number=15, author="dependabot[bot]",
        review_decision="REVIEW_REQUIRED", checks_status="PENDING",
    )
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        repo_permissions={"dhh1128/alpha": "admin"},
    )
    monkeypatch.setattr("gitbulk.commands.merge.ProductionGHClient", lambda: fake)
    rc = merge_handler(_make_args(code_root=code_root, apply=True, approve=True))
    assert fake.call_count["approve_pr"] == 0
    assert fake.call_count["merge_pr"] == 0
    assert rc == EXIT_OK


def test_apply_approve_age_below_threshold_still_merges_after_approval(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    # Strict + min_business_days=3 + just-pushed bot PR. approved_per_policy
    # Skips (not APPROVED) AND age_threshold Skips (too young). Both are the
    # ONLY blockers → auto-approvable; approval merges it.
    write_config(
        repos_slugs=["dhh1128/alpha"],
        bots=["dependabot[bot]"],
        defaults_extra={"min_business_days": 3},
    )
    fresh_org_cache("provenant-dev", ["dhh1128"])
    now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(_catalog, "_utc_now", lambda: now)
    pr = _bot_pr("dhh1128/alpha", 15, last_pushed_at=now - timedelta(hours=2))
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        repo_permissions={"dhh1128/alpha": "maintain"},
        approve_responses={("dhh1128/alpha", 15): {}},
        merge_responses={("dhh1128/alpha", 15): {"merged": True}},
        merge_commit_shas={("dhh1128/alpha", 15): "abc"},
    )
    monkeypatch.setattr("gitbulk.commands.merge.ProductionGHClient", lambda: fake)
    rc = merge_handler(_make_args(code_root=code_root, apply=True, approve=True))
    assert rc == EXIT_OK
    assert fake.call_count["approve_pr"] == 1
    assert fake.call_count["merge_pr"] == 1


def test_apply_approve_one_merge_per_repo_only_primary_approved(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"], bots=["dependabot[bot]"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr1 = _bot_pr("dhh1128/alpha", 15)
    pr2 = _bot_pr("dhh1128/alpha", 16)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr2, pr1]},
        repo_permissions={"dhh1128/alpha": "maintain"},
        approve_responses={("dhh1128/alpha", 15): {}},
        merge_responses={("dhh1128/alpha", 15): {"merged": True}},
        merge_commit_shas={("dhh1128/alpha", 15): "abc"},
    )
    monkeypatch.setattr("gitbulk.commands.merge.ProductionGHClient", lambda: fake)
    rc = merge_handler(_make_args(code_root=code_root, apply=True, approve=True))
    # Lowest number (15) is the primary; 16 is deferred and NOT approved.
    assert fake.call_count["approve_pr"] == 1
    assert fake.approve_calls[0]["number"] == 15
    assert fake.call_count["merge_pr"] == 1
    assert fake.merge_calls[0]["number"] == 15
    summary = (paths.latest_run_symlink("merge").resolve() / "summary.md").read_text()
    assert "Deferred" in summary


def test_approve_without_apply_posts_nothing(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"], bots=["dependabot[bot]"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr = _bot_pr("dhh1128/alpha", 15)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        repo_permissions={"dhh1128/alpha": "maintain"},
    )
    monkeypatch.setattr("gitbulk.commands.merge.ProductionGHClient", lambda: fake)
    rc = merge_handler(_make_args(code_root=code_root, approve=True))
    assert rc == EXIT_OK
    assert fake.call_count["approve_pr"] == 0
    assert fake.call_count["merge_pr"] == 0


def test_apply_approve_failure_records_error_and_does_not_merge(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"], bots=["dependabot[bot]"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr = _bot_pr("dhh1128/alpha", 15)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        repo_permissions={"dhh1128/alpha": "maintain"},
        approve_responses={("dhh1128/alpha", 15): GHError("422 boom")},
    )
    monkeypatch.setattr("gitbulk.commands.merge.ProductionGHClient", lambda: fake)
    rc = merge_handler(_make_args(code_root=code_root, apply=True, approve=True))
    assert fake.call_count["approve_pr"] == 1
    assert fake.call_count["merge_pr"] == 0
    assert rc == EXIT_ATTENTION_NEEDED


def test_apply_approve_viewer_lookup_failure_disables_auto_approve(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"], bots=["dependabot[bot]"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr = _bot_pr("dhh1128/alpha", 15)

    class _NoUserFake(FakeGHClient):
        def authenticated_user(self, *, timeout=None):
            # gh.authenticated preflight already passed (it uses a different
            # path); simulate the --approve viewer lookup failing.
            raise GHError("cannot resolve user")

    # The gh.authenticated preflight calls authenticated_user too, so to
    # isolate the --approve lookup we let preflight pass via a normal fake
    # and only fail the SECOND call. Simpler: count calls.
    calls = {"n": 0}

    class _SecondCallFails(FakeGHClient):
        def authenticated_user(self, *, timeout=None):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise GHError("cannot resolve user")
            return super().authenticated_user(timeout=timeout)

    fake = _SecondCallFails(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        repo_permissions={"dhh1128/alpha": "maintain"},
    )
    monkeypatch.setattr("gitbulk.commands.merge.ProductionGHClient", lambda: fake)
    rc = merge_handler(_make_args(code_root=code_root, apply=True, approve=True))
    # Viewer login unresolved → auto-approval disabled; nothing approved.
    assert fake.call_count["approve_pr"] == 0
    assert fake.call_count["merge_pr"] == 0
    assert rc == EXIT_OK


def test_no_approve_flag_behavior_unchanged(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    # Same green-but-unapproved bot PR, but WITHOUT --approve: not merged,
    # not approved, and no viewer/permission calls happen.
    write_config(repos_slugs=["dhh1128/alpha"], bots=["dependabot[bot]"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr = _bot_pr("dhh1128/alpha", 15)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
    )
    monkeypatch.setattr("gitbulk.commands.merge.ProductionGHClient", lambda: fake)
    rc = merge_handler(_make_args(code_root=code_root, apply=True))
    assert fake.call_count["approve_pr"] == 0
    assert fake.call_count["merge_pr"] == 0
    assert fake.call_count["viewer_repo_permission"] == 0
    assert rc == EXIT_OK


def test_approve_author_without_approve_has_no_effect(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"], bots=["dependabot[bot]"])
    fresh_org_cache("provenant-dev", ["dhh1128", "alice"])
    pr = _bot_pr("dhh1128/alpha", 15, author="alice")
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128", "alice"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
    )
    monkeypatch.setattr("gitbulk.commands.merge.ProductionGHClient", lambda: fake)
    rc = merge_handler(
        _make_args(code_root=code_root, apply=True, approve_author=["alice"])
    )
    assert fake.call_count["approve_pr"] == 0
    assert fake.call_count["merge_pr"] == 0
    assert rc == EXIT_OK


# ─── _classify_auto_approvable unit tests (defensive branches) ─────────────


def _policy_with_bots(*bots):
    from gitbulk.config.policy import Defaults, Policy

    return Policy(defaults=Defaults(merge_policy="strict"), bots=tuple(bots))


def test_classify_auto_approvable_fail_blocker_returns_false(isolated_xdg):
    """A PR whose chain Failed (passed=False) is never auto-approvable."""
    from gitbulk.invariants.runner import ChainResult
    from gitbulk.runstate import RunState

    rs = RunState.begin("merge", argv=["x"], config_snapshot={})
    pr = _bot_pr("a/b", 1)
    result = ChainResult(passed=False, fail_reason="boom", skips=())
    assert (
        merge_mod._classify_auto_approvable(
            _policy_with_bots("dependabot[bot]"),
            "a/b", pr, result, [], "dhh1128",
            frozenset(), FakeGHClient(), rs,
        )
        is False
    )
    rs.complete(EXIT_OK, retain_runs=5)


def test_classify_auto_approvable_no_skips_returns_false(isolated_xdg):
    """Defensive: a passed PR with no intrinsic skips is not auto-approvable
    (it would already be eligible)."""
    from gitbulk.invariants.runner import ChainResult
    from gitbulk.runstate import RunState

    rs = RunState.begin("merge", argv=["x"], config_snapshot={})
    pr = _bot_pr("a/b", 1)
    result = ChainResult(passed=True, fail_reason=None, skips=())
    assert (
        merge_mod._classify_auto_approvable(
            _policy_with_bots("dependabot[bot]"),
            "a/b", pr, result, [], "dhh1128",
            frozenset(), FakeGHClient(), rs,
        )
        is False
    )
    rs.complete(EXIT_OK, retain_runs=5)


def test_classify_auto_approvable_permission_lookup_error_returns_false(isolated_xdg):
    """A GHError from viewer_repo_permission disables auto-approval for that
    PR and records a WARNING."""
    from gitbulk.invariants.runner import ChainResult
    from gitbulk.runstate import RunState

    rs = RunState.begin("merge", argv=["x"], config_snapshot={})
    pr = _bot_pr("a/b", 1)
    result = ChainResult(
        passed=True, fail_reason=None,
        skips=(("pr.approved_per_policy", "needs APPROVED"),),
    )
    fake = FakeGHClient()  # viewer_repo_permission unconfigured → GHError
    assert (
        merge_mod._classify_auto_approvable(
            _policy_with_bots("dependabot[bot]"),
            "a/b", pr, result, [("pr.approved_per_policy", "needs APPROVED")],
            "dhh1128", frozenset(), fake, rs,
        )
        is False
    )
    rs.complete(EXIT_OK, retain_runs=5)
    err_log = (paths.latest_run_symlink("merge").resolve() / "errors.log").read_text()
    assert "permission check failed" in err_log
