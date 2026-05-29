"""Tests for gitbulk.filters (fleet-subset selection; node flt7arg2)."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gitbulk.config.policy import Policy
from gitbulk.config.repos import ConfigError, RepoEntry
from gitbulk.filters import (
    FilterSpec,
    apply_pr_filters,
    fetch_author,
    filter_summary_line,
    resolve_filter_spec,
    select_prs,
    select_repos,
)
from gitbulk.pr_info import PRInfo


def _repo(slug: str) -> RepoEntry:
    owner, name = slug.split("/", 1)
    return RepoEntry(slug=slug, owner=owner, name=name,
                     local_path=Path("/tmp") / name, source_line=1)


def _pr(slug="o/r", number=1, base_ref="main", mergeable_state="CLEAN") -> PRInfo:
    return PRInfo(
        slug=slug, number=number, title="t", url=f"https://github.com/{slug}/pull/{number}",
        author="dhh1128", base_ref=base_ref, head_ref="f", head_sha="a" * 40,
        state="OPEN", is_draft=False, mergeable_state=mergeable_state,
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
        last_pushed_at=None, labels=(), review_decision=None, checks_status=None,
    )


def _args(**kw):
    base = dict(org=None, repo=None, base=None, mergeable_state=None,
                author=None, filter=None)
    base.update(kw)
    return argparse.Namespace(**base)


# ─── FilterSpec basics ─────────────────────────────────────────────────────


def test_empty_spec_is_empty():
    s = FilterSpec()
    assert s.is_empty
    assert not s.constrains_repos
    assert not s.constrains_prs


def test_spec_constrains_repos_and_prs_flags():
    assert FilterSpec(orgs=("x",)).constrains_repos
    assert FilterSpec(repo_globs=("x/*",)).constrains_repos
    assert FilterSpec(bases=("dev",)).constrains_prs
    assert FilterSpec(mergeable_states=("DIRTY",)).constrains_prs
    # author alone constrains neither repos nor PRs (handled at fetch)
    s = FilterSpec(authors=("bob",))
    assert not s.constrains_repos
    assert not s.constrains_prs
    assert not s.is_empty


# ─── select_repos ──────────────────────────────────────────────────────────


def test_select_repos_no_constraint_returns_all():
    repos = [_repo("a/b"), _repo("c/d")]
    kept, excluded = select_repos(repos, FilterSpec())
    assert kept == repos
    assert excluded == 0


def test_select_repos_by_org():
    repos = [_repo("provenant-dev/x"), _repo("dhh1128/y"), _repo("provenant-dev/z")]
    kept, excluded = select_repos(repos, FilterSpec(orgs=("provenant-dev",)))
    assert [r.slug for r in kept] == ["provenant-dev/x", "provenant-dev/z"]
    assert excluded == 1


def test_select_repos_by_glob_on_slug():
    repos = [_repo("p/origin-a"), _repo("p/vvp-b"), _repo("q/origin-c")]
    kept, _ = select_repos(repos, FilterSpec(repo_globs=("*/origin-*",)))
    assert [r.slug for r in kept] == ["p/origin-a", "q/origin-c"]


def test_select_repos_glob_can_pin_owner():
    repos = [_repo("p/origin-a"), _repo("q/origin-c")]
    kept, _ = select_repos(repos, FilterSpec(repo_globs=("p/origin-*",)))
    assert [r.slug for r in kept] == ["p/origin-a"]


def test_select_repos_multiple_globs_or():
    repos = [_repo("p/origin-a"), _repo("p/vvp-b"), _repo("p/other")]
    kept, _ = select_repos(repos, FilterSpec(repo_globs=("*/origin-*", "*/vvp-*")))
    assert [r.slug for r in kept] == ["p/origin-a", "p/vvp-b"]


def test_select_repos_org_and_glob_and_together():
    repos = [_repo("p/origin-a"), _repo("q/origin-b")]
    kept, _ = select_repos(repos, FilterSpec(orgs=("p",), repo_globs=("*/origin-*",)))
    assert [r.slug for r in kept] == ["p/origin-a"]


# ─── select_prs ────────────────────────────────────────────────────────────


def test_select_prs_no_constraint_returns_all():
    prs = [_pr(number=1), _pr(number=2)]
    kept, excluded = select_prs(prs, FilterSpec())
    assert kept == prs
    assert excluded == 0


def test_select_prs_by_base():
    prs = [_pr(number=1, base_ref="main"), _pr(number=2, base_ref="dev")]
    kept, excluded = select_prs(prs, FilterSpec(bases=("dev",)))
    assert [p.number for p in kept] == [2]
    assert excluded == 1


def test_select_prs_by_mergeable_state():
    prs = [_pr(number=1, mergeable_state="CLEAN"), _pr(number=2, mergeable_state="DIRTY")]
    kept, _ = select_prs(prs, FilterSpec(mergeable_states=("DIRTY",)))
    assert [p.number for p in kept] == [2]


def test_select_prs_mergeable_state_none_excluded_when_filtering():
    prs = [_pr(number=1, mergeable_state=None)]
    kept, excluded = select_prs(prs, FilterSpec(mergeable_states=("CLEAN",)))
    assert kept == []
    assert excluded == 1


# ─── apply_pr_filters ──────────────────────────────────────────────────────


def test_apply_pr_filters_no_constraint_passthrough():
    by_repo = {"a/b": [_pr(number=1)]}
    out, excluded = apply_pr_filters(by_repo, FilterSpec())
    assert out == by_repo
    assert excluded == 0


def test_apply_pr_filters_per_repo_and_total_count():
    by_repo = {
        "a/b": [_pr(slug="a/b", number=1, base_ref="dev"),
                _pr(slug="a/b", number=2, base_ref="main")],
        "c/d": [_pr(slug="c/d", number=3, base_ref="main")],
    }
    out, excluded = apply_pr_filters(by_repo, FilterSpec(bases=("dev",)))
    assert [p.number for p in out["a/b"]] == [1]
    assert out["c/d"] == []
    assert excluded == 2


# ─── fetch_author ──────────────────────────────────────────────────────────


def test_fetch_author_default_when_unset():
    assert fetch_author(FilterSpec()) == "@me"


def test_fetch_author_uses_first_author():
    assert fetch_author(FilterSpec(authors=("bob", "alice"))) == "bob"


def test_fetch_author_custom_default():
    assert fetch_author(FilterSpec(), default=None) is None


# ─── filter_summary_line ───────────────────────────────────────────────────


def test_filter_summary_line_none_when_empty():
    assert filter_summary_line(FilterSpec(), 0, 0) is None


def test_filter_summary_line_single_dim_omits_others():
    """Only the active dimension appears (exercises each absent branch).
    Uses ``base`` (a non-first dim) so the orgs/repo_globs branches take
    their false path too."""
    line = filter_summary_line(FilterSpec(bases=("dev",)), 0, 2)
    assert "base=dev" in line
    assert "org=" not in line
    assert "repo=" not in line
    assert "author=" not in line
    assert "mergeable_state=" not in line


def test_filter_summary_line_renders_active_dims():
    spec = FilterSpec(orgs=("p",), bases=("dev",), authors=("bob",),
                      repo_globs=("*/x-*",), mergeable_states=("DIRTY",))
    line = filter_summary_line(spec, 5, 3)
    assert "org=p" in line
    assert "base=dev" in line
    assert "author=bob" in line
    assert "repo=*/x-*" in line
    assert "mergeable_state=DIRTY" in line
    assert "5 repos, 3 PRs excluded" in line


# ─── resolve_filter_spec ───────────────────────────────────────────────────


def test_resolve_empty_args_empty_spec():
    spec = resolve_filter_spec(_args(), Policy())
    assert spec.is_empty


def test_resolve_cli_flags():
    spec = resolve_filter_spec(
        _args(org=["p"], repo=["*/x-*"], base=["dev"],
              mergeable_state=["DIRTY"], author=["bob"]),
        Policy(),
    )
    assert spec.orgs == ("p",)
    assert spec.repo_globs == ("*/x-*",)
    assert spec.bases == ("dev",)
    assert spec.mergeable_states == ("DIRTY",)
    assert spec.authors == ("bob",)


def test_resolve_named_set_from_policy():
    policy = Policy(filters={"svc": FilterSpec(orgs=("provenant-dev",),
                                               repo_globs=("*/origin-*",))})
    spec = resolve_filter_spec(_args(filter="svc"), policy)
    assert spec.orgs == ("provenant-dev",)
    assert spec.repo_globs == ("*/origin-*",)


def test_resolve_cli_narrows_named_set():
    """CLI value on a dimension replaces the named set's value there;
    other dimensions of the named set survive."""
    policy = Policy(filters={"svc": FilterSpec(orgs=("provenant-dev",),
                                               bases=("dev",))})
    spec = resolve_filter_spec(_args(filter="svc", base=["main"]), policy)
    assert spec.orgs == ("provenant-dev",)   # from named set
    assert spec.bases == ("main",)            # CLI narrowed


def test_resolve_unknown_filter_name_raises():
    with pytest.raises(ConfigError, match="not found"):
        resolve_filter_spec(_args(filter="nope"), Policy())
