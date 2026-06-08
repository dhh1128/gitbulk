"""Unit tests for the recovery core (:mod:`gitbulk.recover`).

The core is pure plus a single injected gh boundary, so every path is
exercised with a hand-built ``state.yaml`` ``repos`` map and a
:class:`FakeGHClient` — no network and no real pruned branch (tick 6lui).
"""

from __future__ import annotations

from gitbulk.gh import FakeGHClient, GHError
from gitbulk.recover import (
    DeletedBranch,
    RecoverOutcome,
    collect_deleted,
    recover_one,
)


def _repos(*, extra_branches=None):
    """A prune-branches state.yaml ``repos`` map with two deleted branches
    across two repos, plus a kept branch that must never be recovered."""
    repos = {
        "o/alpha": {
            "default_branch": "main",
            "branches": [
                {"branch": "feat-a", "sha": "a" * 40, "disposition": "deleted",
                 "pr_number": 11, "reason": "PR #11 merged"},
                {"branch": "keep-me", "sha": "c" * 40, "disposition": "kept",
                 "reason": "no merged/closed PR"},
            ],
        },
        "o/beta": {
            "default_branch": "trunk",
            "branches": [
                {"branch": "feat-b", "sha": "b" * 40, "disposition": "deleted",
                 "pr_number": 22, "reason": "PR #22 merged"},
            ],
        },
    }
    if extra_branches is not None:
        repos["o/alpha"]["branches"].extend(extra_branches)
    return repos


# ─── collect_deleted ───────────────────────────────────────────────────────


def test_collect_deleted_returns_only_deleted_rows():
    out = collect_deleted(_repos())
    assert [(d.slug, d.branch, d.sha) for d in out] == [
        ("o/alpha", "feat-a", "a" * 40),
        ("o/beta", "feat-b", "b" * 40),
    ]
    # Carried-through context for the audit trail / reporting.
    assert out[0].pr_number == 11 and out[0].reason == "PR #11 merged"


def test_collect_deleted_sorted_by_slug_for_stable_output():
    repos = {"o/zeta": _repos()["o/beta"], "o/alpha": _repos()["o/alpha"]}
    out = collect_deleted(repos)
    assert [d.slug for d in out] == ["o/alpha", "o/zeta"]


def test_collect_deleted_narrows_by_slug():
    out = collect_deleted(_repos(), slug="o/beta")
    assert [d.branch for d in out] == ["feat-b"]


def test_collect_deleted_narrows_by_slug_and_branch():
    out = collect_deleted(_repos(), slug="o/alpha", branch="feat-a")
    assert [d.branch for d in out] == ["feat-a"]


def test_collect_deleted_branch_filter_requires_match():
    assert collect_deleted(_repos(), slug="o/alpha", branch="ghost") == []


def test_collect_deleted_skips_rows_missing_sha_or_branch():
    out = collect_deleted(_repos(extra_branches=[
        {"branch": "no-sha", "disposition": "deleted"},
        {"sha": "d" * 40, "disposition": "deleted"},
    ]))
    assert "no-sha" not in [d.branch for d in out]
    assert len(out) == 2


def test_collect_deleted_tolerates_malformed_repo_entry():
    repos = {"o/bad": ["not", "a", "dict"], "o/alpha": _repos()["o/alpha"]}
    out = collect_deleted(repos)
    assert [d.slug for d in out] == ["o/alpha"]


def test_collect_deleted_skips_non_dict_branch_row():
    out = collect_deleted(_repos(extra_branches=["a bare string, not a row"]))
    assert [d.branch for d in out] == ["feat-a", "feat-b"]


def test_collect_deleted_empty_map():
    assert collect_deleted({}) == []


# ─── recover_one ───────────────────────────────────────────────────────────


def _db(slug="o/alpha", branch="feat-a", sha="a" * 40):
    return DeletedBranch(slug=slug, branch=branch, sha=sha)


def test_recover_one_creates_absent_ref():
    fake = FakeGHClient()  # branch_ref_sha defaults to None (absent)
    out = recover_one(fake, _db())
    assert out == RecoverOutcome(
        slug="o/alpha", branch="feat-a", sha="a" * 40,
        status="recovered", detail="",
    )
    assert fake.create_branch_calls == [
        {"slug": "o/alpha", "branch": "feat-a", "sha": "a" * 40}
    ]


def test_recover_one_idempotent_when_present_at_same_sha():
    fake = FakeGHClient(branch_ref_shas={("o/alpha", "feat-a"): "a" * 40})
    out = recover_one(fake, _db())
    assert out.status == "already-present"
    assert "recorded SHA" in out.detail
    # Must NOT attempt to re-create an existing ref.
    assert fake.create_branch_calls == []


def test_recover_one_present_at_different_sha_is_reported_not_overwritten():
    fake = FakeGHClient(branch_ref_shas={("o/alpha", "feat-a"): "e" * 40})
    out = recover_one(fake, _db())
    assert out.status == "already-present"
    assert "different SHA" in out.detail
    assert fake.create_branch_calls == []


def test_recover_one_with_explicitly_configured_success():
    # A configured (non-exception) response is the success path distinct from
    # the unconfigured default — both leave the ref created.
    fake = FakeGHClient(create_branch_responses={("o/alpha", "feat-a"): None})
    out = recover_one(fake, _db())
    assert out.status == "recovered"
    assert fake.create_branch_calls == [
        {"slug": "o/alpha", "branch": "feat-a", "sha": "a" * 40}
    ]


def test_recover_one_maps_gh_failure_to_failed_outcome():
    fake = FakeGHClient(
        create_branch_responses={("o/alpha", "feat-a"): GHError("boom")},
    )
    out = recover_one(fake, _db())
    assert out.status == "failed" and "boom" in out.detail
    assert fake.create_branch_calls == [
        {"slug": "o/alpha", "branch": "feat-a", "sha": "a" * 40}
    ]
