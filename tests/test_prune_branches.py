"""End-to-end tests for ``gitbulk prune-branches`` (node prnbr4kq).

Every gh call goes through :class:`FakeGHClient`; no network. The handler
is clone-free, so we only need a config + repos.txt + fresh org cache,
mirroring the merge test fixtures.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from gitbulk import paths, sentinel
from gitbulk.commands import prune_branches as pb
from gitbulk.commands.prune_branches import (
    EXIT_ATTENTION_NEEDED,
    EXIT_INVARIANT_SKIPPED,
    EXIT_OK,
    EXIT_OVERRIDES_APPLIED,
    EXIT_STRUCTURAL_FAILURE,
    _classify_branch,
    prune_branches_handler,
)
from gitbulk.gh import FakeGHClient, GHError
from gitbulk.org_members_cache import CachedMembers, save_cache
from gitbulk.pr_info import BranchRef, ClosedPRRef, PRInfo


# ─── fixtures (mirror test_merge) ──────────────────────────────────────────


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


@pytest.fixture
def write_config(isolated_xdg, code_root):
    def _write(*, repos_slugs, defaults_extra=None, repo_overrides=None):
        cfg_dir = paths.config_dir()
        cfg_dir.mkdir(parents=True, exist_ok=True)
        defaults = {"retain_runs": 5, "prune_min_age_days": 7}
        if defaults_extra:
            defaults.update(defaults_extra)
        policy_yaml: dict = {"defaults": defaults}
        policy_yaml["humans"] = {"org": "provenant-dev", "cache_ttl_hours": 24}
        if repo_overrides:
            policy_yaml["repos"] = repo_overrides
        (cfg_dir / "gitbulk.yaml").write_text(yaml.safe_dump(policy_yaml))
        (cfg_dir / "repos.txt").write_text("\n".join(repos_slugs) + "\n")
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


def _args(*, apply=False, code_root=None, skip_check=None,
          refresh_org_members=False, org=None, repo=None, base=None,
          mergeable_state=None, author=None, filter=None, concurrency=1):
    # concurrency defaults to 1 here so handler tests run the deterministic
    # inline scan path; parallel-path tests pass concurrency>1 explicitly.
    return argparse.Namespace(
        subcommand="prune-branches", apply=apply,
        code_root=str(code_root) if code_root else None,
        skip_check=list(skip_check) if skip_check else None,
        refresh_org_members=refresh_org_members,
        org=org, repo=repo, base=base, mergeable_state=mergeable_state,
        author=author, filter=filter, concurrency=concurrency,
    )


def _open_pr(slug, number, *, head_ref, base_ref="main"):
    return PRInfo(
        slug=slug, number=number, title=f"open {number}",
        url="u", author="dhh1128", base_ref=base_ref, head_ref=head_ref,
        head_sha="f" * 40, state="OPEN", is_draft=False,
        mergeable_state="CLEAN",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        last_pushed_at=None, labels=(), review_decision=None,
        checks_status=None,
    )


def _closed(slug, number, *, head_ref, head_sha, merged=True, days_ago=30):
    return ClosedPRRef(
        number=number, title=f"closed {number}", url="u", merged=merged,
        base_ref="main", head_ref=head_ref, head_sha=head_sha,
        head_repo_slug=slug,
        closed_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )


def _install(monkeypatch, fake):
    monkeypatch.setattr(
        "gitbulk.commands.prune_branches.ProductionGHClient", lambda: fake
    )


def _latest_summary():
    return (
        paths.latest_run_symlink("prune-branches").resolve() / "summary.md"
    ).read_text()


# ─── _classify_branch unit tests (the guardrails) ──────────────────────────


def _br(name="feat", sha="a" * 40, protected=False):
    return BranchRef(name=name, sha=sha, protected=protected)


def _policy():
    from gitbulk.config.policy import Policy
    return Policy()


NOW = datetime(2026, 6, 3, tzinfo=timezone.utc)


def test_classify_skips_default_branch():
    out = _classify_branch(
        FakeGHClient(), _policy(), "o/r", "main", _br(name="main"),
        set(), set(), NOW,
    )
    assert out["decision"] == "skip" and "default" in out["reason"]


def test_classify_skips_protected():
    out = _classify_branch(
        FakeGHClient(), _policy(), "o/r", "main", _br(protected=True),
        set(), set(), NOW,
    )
    assert out["decision"] == "skip" and "protected" in out["reason"]


def test_classify_skips_open_head():
    out = _classify_branch(
        FakeGHClient(), _policy(), "o/r", "main", _br(name="feat"),
        {"feat"}, set(), NOW,
    )
    assert out["decision"] == "skip" and "head of an open PR" in out["reason"]


def test_classify_skips_open_base_stacked():
    out = _classify_branch(
        FakeGHClient(), _policy(), "o/r", "main", _br(name="feat"),
        set(), {"feat"}, NOW,
    )
    assert out["decision"] == "skip" and "base of an open PR" in out["reason"]


def test_classify_skips_when_closed_lookup_errors():
    fake = FakeGHClient(closed_prs_for_head={("o/r", "feat"): GHError("boom")})
    out = _classify_branch(fake, _policy(), "o/r", "main", _br(), set(), set(), NOW)
    assert out["decision"] == "skip" and "could not list closed" in out["reason"]


def test_classify_skips_when_no_upstream_closed_pr():
    # closed PR exists but on a fork (head_repo_slug != slug) → not eligible.
    fork_pr = ClosedPRRef(
        number=1, title="t", url="u", merged=True, base_ref="main",
        head_ref="feat", head_sha="a" * 40, head_repo_slug="someone/fork",
        closed_at=NOW - timedelta(days=30),
    )
    fake = FakeGHClient(closed_prs_for_head={("o/r", "feat"): [fork_pr]})
    out = _classify_branch(fake, _policy(), "o/r", "main", _br(), set(), set(), NOW)
    assert out["decision"] == "skip" and "no merged/closed PR" in out["reason"]


def test_classify_skips_within_grace_period():
    pr = _closed("o/r", 1, head_ref="feat", head_sha="a" * 40, days_ago=2)
    fake = FakeGHClient(closed_prs_for_head={("o/r", "feat"): [pr]})
    out = _classify_branch(fake, _policy(), "o/r", "main", _br(), set(), set(), NOW)
    assert out["decision"] == "skip" and "grace period" in out["reason"]


def test_classify_deletes_merged_with_matching_head_sha():
    # merged PR whose recorded head SHA == branch tip → no post-merge pushes.
    pr = _closed("o/r", 1, head_ref="feat", head_sha="a" * 40, days_ago=30)
    fake = FakeGHClient(closed_prs_for_head={("o/r", "feat"): [pr]})
    out = _classify_branch(
        fake, _policy(), "o/r", "main", _br(sha="a" * 40), set(), set(), NOW
    )
    assert out["decision"] == "delete"
    assert fake.call_count["branch_ahead_by"] == 0  # short-circuited


def test_classify_deletes_when_fully_merged_into_default():
    # tip differs from PR head (squash merge) but ahead_by == 0.
    pr = _closed("o/r", 1, head_ref="feat", head_sha="z" * 40, days_ago=30)
    fake = FakeGHClient(
        closed_prs_for_head={("o/r", "feat"): [pr]},
        branch_ahead_by={("o/r", "main", "feat"): 0},
    )
    out = _classify_branch(
        fake, _policy(), "o/r", "main", _br(sha="a" * 40), set(), set(), NOW
    )
    assert out["decision"] == "delete" and "fully merged" in out["reason"]


def test_classify_skips_when_branch_has_unmerged_commits():
    pr = _closed("o/r", 1, head_ref="feat", head_sha="z" * 40, days_ago=30)
    fake = FakeGHClient(
        closed_prs_for_head={("o/r", "feat"): [pr]},
        branch_ahead_by={("o/r", "main", "feat"): 3},
    )
    out = _classify_branch(
        fake, _policy(), "o/r", "main", _br(sha="a" * 40), set(), set(), NOW
    )
    assert out["decision"] == "skip" and "would lose work" in out["reason"]


def test_classify_skips_when_ahead_by_errors():
    pr = _closed("o/r", 1, head_ref="feat", head_sha="z" * 40, days_ago=30)
    fake = FakeGHClient(
        closed_prs_for_head={("o/r", "feat"): [pr]},
        branch_ahead_by={("o/r", "main", "feat"): GHError("compare failed")},
    )
    out = _classify_branch(
        fake, _policy(), "o/r", "main", _br(sha="a" * 40), set(), set(), NOW
    )
    assert out["decision"] == "skip" and "could not verify merge state" in out["reason"]


def test_classify_closed_unmerged_fully_merged_is_deletable():
    # A closed-but-unmerged PR whose branch turns out fully merged anyway.
    pr = _closed("o/r", 1, head_ref="feat", head_sha="z" * 40, merged=False, days_ago=30)
    fake = FakeGHClient(
        closed_prs_for_head={("o/r", "feat"): [pr]},
        branch_ahead_by={("o/r", "main", "feat"): 0},
    )
    out = _classify_branch(
        fake, _policy(), "o/r", "main", _br(sha="a" * 40), set(), set(), NOW
    )
    assert out["decision"] == "delete"


# ─── handler: dry-run ──────────────────────────────────────────────────────


def test_dry_run_lists_candidate_no_delete_calls(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
        branches={"dhh1128/alpha": [
            _br(name="main", protected=True),
            _br(name="merged-feat", sha="a" * 40),
        ]},
        closed_prs_for_head={
            ("dhh1128/alpha", "merged-feat"): [
                _closed("dhh1128/alpha", 7, head_ref="merged-feat",
                        head_sha="a" * 40, days_ago=30)
            ],
        },
    )
    _install(monkeypatch, fake)
    rc = prune_branches_handler(_args(code_root=code_root))
    assert rc == EXIT_OK
    assert fake.call_count["delete_branch_ref"] == 0
    assert not sentinel.has_attention()
    summary = _latest_summary()
    assert "DRY-RUN" in summary
    assert "Would delete" in summary
    assert "merged-feat" in summary


def test_apply_deletes_candidate(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
        branches={"dhh1128/alpha": [_br(name="merged-feat", sha="a" * 40)]},
        closed_prs_for_head={
            ("dhh1128/alpha", "merged-feat"): [
                _closed("dhh1128/alpha", 7, head_ref="merged-feat",
                        head_sha="a" * 40, days_ago=30)
            ],
        },
    )
    _install(monkeypatch, fake)
    rc = prune_branches_handler(_args(apply=True, code_root=code_root))
    assert rc == EXIT_OK
    assert fake.delete_branch_calls == [
        {"slug": "dhh1128/alpha", "branch": "merged-feat"}
    ]
    assert not sentinel.has_attention()  # quiet successful cleanup
    assert "Deleted" in _latest_summary()


def test_apply_delete_failure_raises_attention(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
        branches={"dhh1128/alpha": [_br(name="merged-feat", sha="a" * 40)]},
        closed_prs_for_head={
            ("dhh1128/alpha", "merged-feat"): [
                _closed("dhh1128/alpha", 7, head_ref="merged-feat",
                        head_sha="a" * 40, days_ago=30)
            ],
        },
        delete_branch_responses={
            ("dhh1128/alpha", "merged-feat"): GHError("403 protected")
        },
    )
    _install(monkeypatch, fake)
    rc = prune_branches_handler(_args(apply=True, code_root=code_root))
    assert rc == EXIT_ATTENTION_NEEDED
    assert sentinel.has_attention()
    assert "FAILED" in _latest_summary()


def test_open_head_branch_is_kept(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [
            _open_pr("dhh1128/alpha", 9, head_ref="active")
        ]},
        branches={"dhh1128/alpha": [_br(name="active", sha="a" * 40)]},
    )
    _install(monkeypatch, fake)
    rc = prune_branches_handler(_args(apply=True, code_root=code_root))
    assert rc == EXIT_OK
    assert fake.call_count["delete_branch_ref"] == 0
    # No closed-PR lookup needed — open-head short-circuits before it.
    assert fake.call_count["closed_prs_for_head"] == 0


def test_scan_gh_error_records_error_result(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
        branches={"dhh1128/alpha": GHError("branches API down")},
    )
    _install(monkeypatch, fake)
    rc = prune_branches_handler(_args(apply=True, code_root=code_root))
    # Scan error is recorded but not fatal; no skipped repos → exit OK.
    assert rc == EXIT_OK
    assert "Errors" in _latest_summary()


# ─── parallel scan (node prnpf8nq) ─────────────────────────────────────────


def test_branch_with_no_closed_pr_is_not_surfaced(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """A branch that clears the cheap guards but has no upstream closed PR
    is dropped from the report (deep skip, no pr_number) — it isn't
    interesting. Exercises the drop arm of the surface filter."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
        branches={"dhh1128/alpha": [_br(name="orphan", sha="a" * 40)]},
        closed_prs_for_head={("dhh1128/alpha", "orphan"): []},
    )
    _install(monkeypatch, fake)
    rc = prune_branches_handler(_args(code_root=code_root))
    assert rc == EXIT_OK
    summary = _latest_summary()
    assert "orphan" not in summary
    assert "no branches matched" in summary
    assert fake.call_count["closed_prs_for_head"] == 1


def test_resolve_concurrency_prefers_arg_over_policy():
    from gitbulk.config.policy import Policy
    policy = Policy()  # default prune_scan_concurrency == 12
    assert pb._resolve_concurrency(_args(concurrency=4), policy) == 4


def test_resolve_concurrency_falls_back_to_policy_when_unset():
    from dataclasses import replace
    from gitbulk.config.policy import Policy, Defaults
    policy = Policy(defaults=replace(Defaults(), prune_scan_concurrency=7))
    assert pb._resolve_concurrency(_args(concurrency=None), policy) == 7


def test_resolve_concurrency_floors_at_one():
    from gitbulk.config.policy import Policy
    assert pb._resolve_concurrency(_args(concurrency=0), Policy()) == 1
    assert pb._resolve_concurrency(_args(concurrency=-5), Policy()) == 1


def test_parallel_scan_surfaces_all_candidates_in_repo_order(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """With concurrency>1 the two-pass scan must still surface every
    candidate and lay them out in passing-repos order — byte-for-byte the
    sequential result."""
    slugs = [f"dhh1128/r{i}" for i in range(4)]
    write_config(repos_slugs=slugs)
    fresh_org_cache("provenant-dev", ["dhh1128"])
    branches = {s: [_br(name="merged-feat", sha="a" * 40)] for s in slugs}
    closed = {
        (s, "merged-feat"): [
            _closed(s, 7, head_ref="merged-feat", head_sha="a" * 40, days_ago=30)
        ]
        for s in slugs
    }
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={s: "main" for s in slugs},
        my_open_prs={s: [] for s in slugs},
        branches=branches,
        closed_prs_for_head=closed,
    )
    _install(monkeypatch, fake)
    rc = prune_branches_handler(_args(code_root=code_root, concurrency=4))
    assert rc == EXIT_OK
    summary = _latest_summary()
    # Every repo's candidate is present, listed in repos.txt order.
    positions = [summary.index(s) for s in slugs]
    assert positions == sorted(positions)
    assert fake.call_count["closed_prs_for_head"] == 4


def test_parallel_apply_deletes_every_candidate(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    slugs = [f"dhh1128/r{i}" for i in range(3)]
    write_config(repos_slugs=slugs)
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={s: "main" for s in slugs},
        my_open_prs={s: [] for s in slugs},
        branches={s: [_br(name="merged-feat", sha="a" * 40)] for s in slugs},
        closed_prs_for_head={
            (s, "merged-feat"): [
                _closed(s, 7, head_ref="merged-feat", head_sha="a" * 40,
                        days_ago=30)
            ]
            for s in slugs
        },
    )
    _install(monkeypatch, fake)
    rc = prune_branches_handler(_args(apply=True, code_root=code_root, concurrency=3))
    assert rc == EXIT_OK
    # delete_branch_calls.append is atomic in CPython, so the count is safe
    # to assert even under the thread pool.
    assert {c["slug"] for c in fake.delete_branch_calls} == set(slugs)


def test_org_refresh_failure_aborts(
    monkeypatch, isolated_xdg, code_root, write_config,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fake = FakeGHClient(user={"login": "dhh1128"})  # no org_members → refresh fails
    _install(monkeypatch, fake)
    rc = prune_branches_handler(_args(code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE


def test_skip_check_yields_exit_4(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
        branches={"dhh1128/alpha": []},
    )
    _install(monkeypatch, fake)
    rc = prune_branches_handler(
        _args(apply=True, code_root=code_root, skip_check=["github.not_archived"])
    )
    assert rc == EXIT_OVERRIDES_APPLIED


def test_lock_timeout_returns_structural_failure(
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
    # default_branches_lock (resource #3) times out — the first resource lock
    # the pipeline reaches (node rsclk7nq); the handler surfaces it as exit 1.
    monkeypatch.setattr(
        "gitbulk.default_branch_cache.default_branches_lock",
        lambda *a, **k: _BoomLock(),
    )
    rc = prune_branches_handler(_args(code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE


# ─── structural / skip branches via a target-aware fake run_chain ──────────

from types import SimpleNamespace  # noqa: E402


def _fake_run_chain(*, fail=(), skip=()):
    def fake(chain, ctx, *, skip_set, target):
        if target in fail:
            return SimpleNamespace(passed=False, fail_reason="boom", skips=[])
        if target in skip:
            return SimpleNamespace(
                passed=True, fail_reason=None,
                skips=[("github.not_archived", "repo is archived")],
            )
        return SimpleNamespace(passed=True, fail_reason=None, skips=[])
    return fake


def _base_fake():
    return FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
        branches={"dhh1128/alpha": []},
    )


def test_universal_preflight_failure_exits_structural(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    _install(monkeypatch, _base_fake())
    monkeypatch.setattr(pb, "run_chain", _fake_run_chain(fail={"global"}))
    rc = prune_branches_handler(_args(code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE


def test_per_repo_failure_exits_structural(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    _install(monkeypatch, _base_fake())
    monkeypatch.setattr(pb, "run_chain", _fake_run_chain(fail={"dhh1128/alpha"}))
    rc = prune_branches_handler(_args(code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE


def test_skipped_repo_dry_run_exits_3_with_filter(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    _install(monkeypatch, _base_fake())
    monkeypatch.setattr(pb, "run_chain", _fake_run_chain(skip={"dhh1128/alpha"}))
    rc = prune_branches_handler(_args(code_root=code_root, org=["dhh1128"]))
    assert rc == EXIT_INVARIANT_SKIPPED
    assert "Skipped repos" in _latest_summary()


def test_skipped_repo_apply_exits_3(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    _install(monkeypatch, _base_fake())
    monkeypatch.setattr(pb, "run_chain", _fake_run_chain(skip={"dhh1128/alpha"}))
    rc = prune_branches_handler(_args(apply=True, code_root=code_root))
    assert rc == EXIT_INVARIANT_SKIPPED


def test_skipped_repos_txt_entry_in_summary(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    # Append a junk line so load_repos yields a SkippedEntry.
    repos_txt = paths.repos_file()
    repos_txt.write_text(repos_txt.read_text() + "this is not a slug or path\n")
    _install(monkeypatch, _base_fake())
    monkeypatch.setattr(pb, "run_chain", _fake_run_chain())
    rc = prune_branches_handler(_args(code_root=code_root))
    assert rc == EXIT_INVARIANT_SKIPPED
    assert "Skipped repos.txt entries" in _latest_summary()


def test_runid_from_run_dir_fallback():
    from pathlib import Path
    assert pb._runid_from_run_dir(Path("20260603-prune-branches")) == "20260603"
    # Name without the expected suffix → rpartition fallback.
    assert pb._runid_from_run_dir(Path("weird-name")) == "weird"


def test_cli_wrapper_delegates(monkeypatch):
    import gitbulk.cli as cli
    called = {}
    monkeypatch.setattr(
        "gitbulk.commands.prune_branches.prune_branches_handler",
        lambda args: called.setdefault("rc", 0) or 0,
    )
    assert cli._prune_branches_handler(argparse.Namespace()) == 0
    assert called["rc"] == 0


def test_utc_now_returns_aware():
    assert pb._utc_now().tzinfo is not None


def test_dry_run_skip_check_exits_4(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    _install(monkeypatch, _base_fake())
    monkeypatch.setattr(pb, "run_chain", _fake_run_chain())
    rc = prune_branches_handler(
        _args(code_root=code_root, skip_check=["github.not_archived"])
    )
    assert rc == EXIT_OVERRIDES_APPLIED


def test_branch_kept_within_grace_shown_in_summary(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
        branches={"dhh1128/alpha": [_br(name="recent", sha="a" * 40)]},
        closed_prs_for_head={
            ("dhh1128/alpha", "recent"): [
                _closed("dhh1128/alpha", 8, head_ref="recent",
                        head_sha="a" * 40, days_ago=2)  # within 7d grace
            ],
        },
    )
    _install(monkeypatch, fake)
    rc = prune_branches_handler(_args(code_root=code_root))
    assert rc == EXIT_OK
    summary = _latest_summary()
    assert "Kept (guardrail)" in summary
    assert "grace period" in summary
