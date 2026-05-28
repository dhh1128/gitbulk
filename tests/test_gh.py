"""Tests for the gh client surface (Protocol + FakeGHClient).

ProductionGHClient is tested in its own file (`test_gh_production.py`)
with mocked subprocess. This file covers the contract.

See this.i node ``ghclmp7n``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from gitbulk.gh import FakeGHClient, GHClient, GHError, GHTimeoutError
from gitbulk.pr_info import PRInfo


def _pr(slug: str = "dhh1128/gitbulk", number: int = 1) -> PRInfo:
    return PRInfo(
        slug=slug,
        number=number,
        title=f"PR #{number}",
        url=f"https://github.com/{slug}/pull/{number}",
        author="dhh1128",
        base_ref="main",
        head_ref=f"feature/{number}",
        head_sha="a" * 40,
        state="OPEN",
        is_draft=False,
        mergeable_state="CLEAN",
        created_at=datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc),
        last_pushed_at=datetime(2026, 5, 28, 11, 0, 0, tzinfo=timezone.utc),
        labels=(),
        review_decision=None,
        checks_status=None,
    )


# ─── Protocol contract ─────────────────────────────────────────────────────


def test_fake_satisfies_ghclient_protocol():
    fake = FakeGHClient()
    assert isinstance(fake, GHClient)


# ─── FakeGHClient.authenticated_user ───────────────────────────────────────


def test_authenticated_user_returns_configured_value():
    fake = FakeGHClient(user={"login": "dhh1128", "id": 1234})
    result = fake.authenticated_user()
    assert result == {"login": "dhh1128", "id": 1234}
    assert fake.call_count["authenticated_user"] == 1


def test_authenticated_user_raises_when_unconfigured():
    fake = FakeGHClient()
    with pytest.raises(GHError, match="authenticated_user not configured"):
        fake.authenticated_user()


def test_authenticated_user_returns_copy_not_internal_reference():
    """Mutating the returned dict must not affect subsequent calls."""
    fake = FakeGHClient(user={"login": "dhh1128"})
    a = fake.authenticated_user()
    a["login"] = "MUTATED"
    b = fake.authenticated_user()
    assert b["login"] == "dhh1128"


# ─── FakeGHClient.org_members ──────────────────────────────────────────────


def test_org_members_returns_configured_list():
    fake = FakeGHClient(org_members={"provenant-dev": ["dhh1128", "alice"]})
    assert fake.org_members("provenant-dev") == ["dhh1128", "alice"]
    assert fake.call_count["org_members"] == 1


def test_org_members_returns_copy():
    fake = FakeGHClient(org_members={"provenant-dev": ["dhh1128"]})
    a = fake.org_members("provenant-dev")
    a.append("MUTATED")
    b = fake.org_members("provenant-dev")
    assert b == ["dhh1128"]


def test_org_members_raises_when_org_not_configured():
    fake = FakeGHClient(org_members={"other": ["bob"]})
    with pytest.raises(GHError, match="org_members.*not configured"):
        fake.org_members("missing-org")


def test_org_members_raises_when_no_org_members_dict():
    fake = FakeGHClient()
    with pytest.raises(GHError, match="org_members.*not configured"):
        fake.org_members("anything")


# ─── FakeGHClient.default_branch ───────────────────────────────────────────


def test_default_branch_returns_configured_value():
    fake = FakeGHClient(default_branches={"dhh1128/gitbulk": "main"})
    assert fake.default_branch("dhh1128/gitbulk") == "main"


def test_default_branch_raises_when_slug_missing():
    fake = FakeGHClient(default_branches={"dhh1128/gitbulk": "main"})
    with pytest.raises(GHError, match="default_branch.*not configured"):
        fake.default_branch("nonexistent/repo")


def test_default_branch_raises_when_no_defaults_dict():
    fake = FakeGHClient()
    with pytest.raises(GHError, match="default_branch.*not configured"):
        fake.default_branch("any/slug")


# ─── FakeGHClient.my_open_prs ──────────────────────────────────────────────


def test_my_open_prs_no_slugs_returns_all_configured():
    pr_a = _pr("a/x", 1)
    pr_b = _pr("b/y", 2)
    fake = FakeGHClient(my_open_prs={"a/x": [pr_a], "b/y": [pr_b]})
    result = fake.my_open_prs()
    assert result == {"a/x": [pr_a], "b/y": [pr_b]}


def test_my_open_prs_with_slug_filter_returns_subset():
    pr_a = _pr("a/x", 1)
    pr_b = _pr("b/y", 2)
    fake = FakeGHClient(my_open_prs={"a/x": [pr_a], "b/y": [pr_b]})
    result = fake.my_open_prs(slugs=["a/x"])
    assert result == {"a/x": [pr_a]}


def test_my_open_prs_with_unknown_slug_returns_empty_list_for_it():
    """Per the GHClient contract, slugs with no PRs map to an empty list,
    NOT to a missing key. Callers iterating over their input slug list
    don't need to handle KeyError."""
    pr_a = _pr("a/x", 1)
    fake = FakeGHClient(my_open_prs={"a/x": [pr_a]})
    result = fake.my_open_prs(slugs=["a/x", "nope/missing"])
    assert "nope/missing" in result
    assert result["nope/missing"] == []
    assert result["a/x"] == [pr_a]


def test_my_open_prs_raises_when_unconfigured():
    fake = FakeGHClient()
    with pytest.raises(GHError, match="my_open_prs not configured"):
        fake.my_open_prs()


def test_my_open_prs_call_count_tracks_coalescing():
    """call_count tracks invocations; coalescing means many slugs in one call
    increments by 1, not N. The Fake mirrors the Production contract."""
    fake = FakeGHClient(
        my_open_prs={"a/x": [_pr("a/x", 1)], "b/y": [_pr("b/y", 2)]}
    )
    fake.my_open_prs(slugs=["a/x", "b/y"])
    assert fake.call_count["my_open_prs"] == 1


# ─── FakeGHClient.merge_pr ─────────────────────────────────────────────────


def test_merge_pr_returns_configured_response():
    fake = FakeGHClient(
        merge_responses={("dhh1128/gitbulk", 42): {"merged": True}}
    )
    result = fake.merge_pr("dhh1128/gitbulk", 42)
    assert result == {"merged": True}
    assert fake.call_count["merge_pr"] == 1


def test_merge_pr_records_call_arguments():
    fake = FakeGHClient(
        merge_responses={("dhh1128/gitbulk", 7): {}}
    )
    fake.merge_pr(
        "dhh1128/gitbulk", 7, method="merge", delete_branch=False, timeout=12.0
    )
    assert fake.merge_calls == [
        {
            "slug": "dhh1128/gitbulk",
            "number": 7,
            "method": "merge",
            "delete_branch": False,
            "timeout": 12.0,
        }
    ]


def test_merge_pr_default_squash_and_delete_branch():
    fake = FakeGHClient(
        merge_responses={("a/b", 1): {}}
    )
    fake.merge_pr("a/b", 1)
    assert fake.merge_calls[0]["method"] == "squash"
    assert fake.merge_calls[0]["delete_branch"] is True


def test_merge_pr_raises_when_unconfigured_pair():
    fake = FakeGHClient(merge_responses={("x/y", 1): {}})
    with pytest.raises(GHError, match="merge_pr.*not configured"):
        fake.merge_pr("other/repo", 99)


def test_merge_pr_raises_when_no_responses_dict():
    fake = FakeGHClient()
    with pytest.raises(GHError, match="merge_pr.*not configured"):
        fake.merge_pr("a/b", 1)


def test_merge_pr_propagates_configured_exception():
    fake = FakeGHClient(
        merge_responses={("a/b", 1): GHError("not mergeable: DIRTY")}
    )
    with pytest.raises(GHError, match="not mergeable"):
        fake.merge_pr("a/b", 1)


def test_merge_pr_response_is_a_copy():
    """Mutating the returned dict must not affect later calls (defense-in-depth)."""
    fake = FakeGHClient(
        merge_responses={("a/b", 1): {"merged": True, "extra": "x"}}
    )
    a = fake.merge_pr("a/b", 1)
    a["merged"] = "MUTATED"
    b = fake.merge_pr("a/b", 1)
    assert b == {"merged": True, "extra": "x"}


def test_merge_pr_increments_counter_even_on_failure():
    """call_count tracks invocations regardless of whether the response
    was an exception — useful for assertions in tests that exercise
    failure handling."""
    fake = FakeGHClient(
        merge_responses={("a/b", 1): GHError("nope")}
    )
    with pytest.raises(GHError):
        fake.merge_pr("a/b", 1)
    assert fake.call_count["merge_pr"] == 1


# ─── Exception hierarchy ───────────────────────────────────────────────────


def test_ghtimeouterror_is_a_gherror():
    err = GHTimeoutError("timed out")
    assert isinstance(err, GHError)


def test_ghtimeouterror_is_a_timeouterror():
    err = GHTimeoutError("timed out")
    assert isinstance(err, TimeoutError)


def test_gherror_carries_command_attribute():
    err = GHError("oops", command=("gh", "api", "user"))
    assert err.command == ("gh", "api", "user")


def test_gherror_command_defaults_to_none():
    err = GHError("oops")
    assert err.command is None
