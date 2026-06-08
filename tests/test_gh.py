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


def test_my_open_prs_fires_on_progress_per_chunk(monkeypatch):
    """The fake mirrors production's per-chunk on_progress(done, total)
    firing so a caller's progress wiring is exercised (node 6bm7)."""
    import gitbulk.gh as gh_mod

    monkeypatch.setattr(gh_mod, "_OPEN_PRS_REPO_CHUNK", 2)
    fake = FakeGHClient(my_open_prs={})
    slugs = ["o/a", "o/b", "o/c", "o/d", "o/e"]  # 5 slugs, chunk 2 → 3 chunks
    calls: list[tuple[int, int]] = []
    fake.my_open_prs(
        slugs=slugs, on_progress=lambda done, total: calls.append((done, total))
    )
    assert calls == [(2, 5), (4, 5), (5, 5)]


def test_my_open_prs_no_slugs_fires_on_progress_once():
    """The None-slugs path (one search) fires on_progress(1, 1)."""
    fake = FakeGHClient(my_open_prs={"a/x": [_pr("a/x", 1)]})
    calls: list[tuple[int, int]] = []
    fake.my_open_prs(on_progress=lambda done, total: calls.append((done, total)))
    assert calls == [(1, 1)]


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


def test_merge_pr_default_method_is_merge_and_delete_branch_true():
    """Default merge method is `merge` (true merge commit) per gji4dyze.
    delete_branch defaults to True so the remote PR branch is cleaned up."""
    fake = FakeGHClient(
        merge_responses={("a/b", 1): {}}
    )
    fake.merge_pr("a/b", 1)
    assert fake.merge_calls[0]["method"] == "merge"
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


# ─── FakeGHClient.fetch_pr_comments ────────────────────────────────────────


def test_fetch_pr_comments_returns_configured_list():
    from datetime import datetime, timezone

    from gitbulk.pr_info import PRComment

    c = PRComment(
        author_login="alice",
        body="hello",
        at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    fake = FakeGHClient(pr_comments={("a/b", 1): [c]})
    result = fake.fetch_pr_comments("a/b", 1)
    assert result == [c]
    assert fake.call_count["fetch_pr_comments"] == 1


def test_fetch_pr_comments_unknown_pr_returns_empty():
    """Distinct from 'not configured at all': empty default means we set
    pr_comments but this specific PR has no comments."""
    fake = FakeGHClient(pr_comments={})
    assert fake.fetch_pr_comments("a/b", 1) == []


def test_fetch_pr_comments_raises_when_unconfigured():
    fake = FakeGHClient()
    with pytest.raises(GHError, match="fetch_pr_comments"):
        fake.fetch_pr_comments("a/b", 1)


# ─── FakeGHClient.post_comment ─────────────────────────────────────────────


def test_post_comment_records_call_and_returns_response():
    fake = FakeGHClient(
        post_comment_responses={("a/b", 1): {"url": "https://github.com/..."}}
    )
    result = fake.post_comment("a/b", 1, "hello world")
    assert result == {"url": "https://github.com/..."}
    assert fake.call_count["post_comment"] == 1
    assert fake.post_comment_calls[0]["slug"] == "a/b"
    assert fake.post_comment_calls[0]["number"] == 1
    assert fake.post_comment_calls[0]["body"] == "hello world"


def test_post_comment_raises_when_unconfigured():
    fake = FakeGHClient()
    with pytest.raises(GHError, match="post_comment"):
        fake.post_comment("a/b", 1, "x")


def test_post_comment_raises_when_pair_unknown():
    fake = FakeGHClient(post_comment_responses={("x/y", 1): {}})
    with pytest.raises(GHError, match="post_comment"):
        fake.post_comment("other/repo", 9, "x")


def test_post_comment_raises_configured_exception():
    fake = FakeGHClient(
        post_comment_responses={("a/b", 1): GHError("rate limited")}
    )
    with pytest.raises(GHError, match="rate limited"):
        fake.post_comment("a/b", 1, "x")


# ─── FakeGHClient.close_pr ─────────────────────────────────────────────────


def test_close_pr_records_call():
    fake = FakeGHClient(close_responses={("a/b", 1): {}})
    fake.close_pr("a/b", 1)
    assert fake.call_count["close_pr"] == 1
    assert fake.close_calls[0]["delete_branch"] is False


def test_close_pr_passes_delete_branch_flag():
    fake = FakeGHClient(close_responses={("a/b", 1): {}})
    fake.close_pr("a/b", 1, delete_branch=True)
    assert fake.close_calls[0]["delete_branch"] is True


def test_close_pr_raises_when_unconfigured():
    fake = FakeGHClient()
    with pytest.raises(GHError, match="close_pr"):
        fake.close_pr("a/b", 1)


def test_close_pr_raises_when_pair_unknown():
    fake = FakeGHClient(close_responses={("x/y", 1): {}})
    with pytest.raises(GHError, match="close_pr"):
        fake.close_pr("other/repo", 9)


def test_close_pr_raises_configured_exception():
    fake = FakeGHClient(close_responses={("a/b", 1): GHError("nope")})
    with pytest.raises(GHError, match="nope"):
        fake.close_pr("a/b", 1)


# ─── FakeGHClient.prefetch_default_branches ────────────────────────────────


def test_fake_prefetch_default_branches_counts_and_is_noop():
    """The fake's default_branches map already serves as the cache, so
    prefetch is a no-op beyond counting the call."""
    fake = FakeGHClient(default_branches={"a/b": "main"})
    fake.prefetch_default_branches(["a/b", "c/d"])
    assert fake.call_count["prefetch_default_branches"] == 1
    # Still resolves from the configured map.
    assert fake.default_branch("a/b") == "main"


def test_fake_prefetch_fires_on_progress_once_at_completion():
    """The fake invokes on_progress(total, total) once so a handler's
    progress wiring is exercised even against the fake."""
    fake = FakeGHClient(default_branches={"a/b": "main"})
    calls: list[tuple[int, int]] = []
    fake.prefetch_default_branches(
        ["a/b", "c/d", "e/f"],
        on_progress=lambda done, total: calls.append((done, total)),
    )
    assert calls == [(3, 3)]


def test_fake_prefetch_without_on_progress_is_silent():
    """on_progress=None path: no callback, no error."""
    fake = FakeGHClient(default_branches={"a/b": "main"})
    fake.prefetch_default_branches(["a/b"])  # no on_progress
    assert fake.call_count["prefetch_default_branches"] == 1


def test_fake_seed_creates_map_when_unset():
    """seed_default_branches into a fake with no default_branches map
    (the constructor default of None) creates the map."""
    fake = FakeGHClient()  # default_branches is None
    fake.seed_default_branches({"a/b": "main"})
    assert fake.default_branch("a/b") == "main"


def test_fake_seed_merges_into_existing_map():
    fake = FakeGHClient(default_branches={"a/b": "main"})
    fake.seed_default_branches({"c/d": "develop"})
    assert fake.default_branch("a/b") == "main"
    assert fake.default_branch("c/d") == "develop"


def test_fake_cached_default_branches_returns_copy():
    fake = FakeGHClient(default_branches={"a/b": "main"})
    snap = fake.cached_default_branches()
    assert snap == {"a/b": "main"}
    snap["a/b"] = "MUTATED"
    assert fake.default_branch("a/b") == "main"  # internal unaffected


def test_fake_cached_default_branches_empty_when_unset():
    fake = FakeGHClient()  # None
    assert fake.cached_default_branches() == {}


# ─── FakeGHClient.is_archived / archived cache ─────────────────────────────


def test_fake_is_archived_defaults_false_for_unconfigured():
    """Unlike default_branch (which raises when unconfigured), is_archived
    defaults to False so every chain test that doesn't care about archived
    status still passes the github.not_archived gate."""
    fake = FakeGHClient()  # archived unset
    assert fake.is_archived("a/b") is False
    assert fake.call_count["is_archived"] == 1


def test_fake_is_archived_returns_configured_true():
    fake = FakeGHClient(archived={"a/b": True, "c/d": False})
    assert fake.is_archived("a/b") is True
    assert fake.is_archived("c/d") is False
    # A slug absent from the map is treated as not-archived.
    assert fake.is_archived("e/f") is False


def test_fake_is_archived_raises_configured_exception():
    """An Exception value in the archived map is raised — mirrors the
    merge_responses 'value-or-Exception' pattern so the GHError→Skip
    branch of github.not_archived is testable."""
    fake = FakeGHClient(archived={"a/b": GHError("boom")})
    with pytest.raises(GHError):
        fake.is_archived("a/b")


def test_fake_seed_archived_merges():
    fake = FakeGHClient(archived={"a/b": True})
    fake.seed_archived({"c/d": True})
    assert fake.is_archived("a/b") is True
    assert fake.is_archived("c/d") is True


def test_fake_cached_archived_returns_bool_only_copy():
    fake = FakeGHClient(archived={"a/b": True, "c/d": False, "x/y": GHError("z")})
    snap = fake.cached_archived()
    # Exception entries are excluded so prime_default_branches only
    # persists real booleans.
    assert snap == {"a/b": True, "c/d": False}
    snap["a/b"] = False
    assert fake.is_archived("a/b") is True  # internal unaffected


def test_fake_cached_archived_empty_when_unset():
    assert FakeGHClient().cached_archived() == {}


# ─── FakeGHClient.fetch_merge_commit_sha ───────────────────────────────────


def test_fetch_merge_commit_sha_returns_configured_value():
    fake = FakeGHClient(merge_commit_shas={("a/b", 1): "deadbeef" * 5})
    assert fake.fetch_merge_commit_sha("a/b", 1) == "deadbeef" * 5
    assert fake.call_count["fetch_merge_commit_sha"] == 1


def test_fetch_merge_commit_sha_missing_key_returns_none():
    """A PR closed unmerged has no merge commit; configured-but-missing returns None."""
    fake = FakeGHClient(merge_commit_shas={})
    assert fake.fetch_merge_commit_sha("a/b", 1) is None


def test_fetch_merge_commit_sha_unconfigured_raises():
    fake = FakeGHClient()
    with pytest.raises(GHError, match="fetch_merge_commit_sha"):
        fake.fetch_merge_commit_sha("a/b", 1)


# ─── FakeGHClient.fetch_check_runs ─────────────────────────────────────────


def test_fetch_check_runs_returns_configured_value():
    from gitbulk.pr_info import CheckRun
    cr = CheckRun(
        name="test",
        status="completed",
        conclusion="success",
        details_url="u",
        completed_at=None,
    )
    fake = FakeGHClient(check_runs={("a/b", "sha1"): [cr]})
    result = fake.fetch_check_runs("a/b", "sha1")
    assert result == [cr]
    assert fake.call_count["fetch_check_runs"] == 1


def test_fetch_check_runs_missing_sha_returns_empty():
    fake = FakeGHClient(check_runs={})
    assert fake.fetch_check_runs("a/b", "sha1") == []


def test_fetch_check_runs_unconfigured_raises():
    fake = FakeGHClient()
    with pytest.raises(GHError, match="fetch_check_runs"):
        fake.fetch_check_runs("a/b", "sha1")


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


# ─── FakeGHClient.approve_pr (node aprmn5kq) ───────────────────────────────


def test_approve_pr_records_call_and_returns_response():
    fake = FakeGHClient(approve_responses={("a/b", 7): {"approved": True}})
    result = fake.approve_pr("a/b", 7)
    assert result == {"approved": True}
    assert fake.call_count["approve_pr"] == 1
    assert fake.approve_calls == [
        {"slug": "a/b", "number": 7, "body": None, "timeout": None}
    ]


def test_approve_pr_records_body_and_timeout():
    fake = FakeGHClient(approve_responses={("a/b", 7): {}})
    fake.approve_pr("a/b", 7, body="LGTM", timeout=5.0)
    assert fake.approve_calls[0]["body"] == "LGTM"
    assert fake.approve_calls[0]["timeout"] == 5.0


def test_approve_pr_response_is_a_copy():
    payload = {"approved": True}
    fake = FakeGHClient(approve_responses={("a/b", 7): payload})
    result = fake.approve_pr("a/b", 7)
    result["mutated"] = 1
    assert "mutated" not in payload


def test_approve_pr_raises_when_no_responses_dict():
    fake = FakeGHClient()
    with pytest.raises(GHError, match="approve_pr"):
        fake.approve_pr("a/b", 7)
    # Counter still bumped (mirrors merge_pr).
    assert fake.call_count["approve_pr"] == 1


def test_approve_pr_raises_when_pair_unknown():
    fake = FakeGHClient(approve_responses={("a/b", 1): {}})
    with pytest.raises(GHError, match="approve_pr"):
        fake.approve_pr("a/b", 99)


def test_approve_pr_propagates_configured_exception():
    boom = GHError("422 self-approval")
    fake = FakeGHClient(approve_responses={("a/b", 7): boom})
    with pytest.raises(GHError, match="self-approval"):
        fake.approve_pr("a/b", 7)


# ─── FakeGHClient.viewer_repo_permission (node aprmn5kq) ───────────────────


def test_viewer_repo_permission_returns_configured_value():
    fake = FakeGHClient(repo_permissions={"a/b": "maintain"})
    assert fake.viewer_repo_permission("a/b") == "maintain"
    assert fake.call_count["viewer_repo_permission"] == 1


def test_viewer_repo_permission_records_calls():
    fake = FakeGHClient(repo_permissions={"a/b": "admin"})
    fake.viewer_repo_permission("a/b", timeout=3.0)
    assert fake.viewer_repo_permission_calls == [
        {"slug": "a/b", "timeout": 3.0}
    ]


def test_viewer_repo_permission_raises_when_unconfigured():
    fake = FakeGHClient()
    with pytest.raises(GHError, match="viewer_repo_permission"):
        fake.viewer_repo_permission("a/b")
    assert fake.call_count["viewer_repo_permission"] == 1


def test_viewer_repo_permission_raises_when_slug_unknown():
    fake = FakeGHClient(repo_permissions={"a/b": "write"})
    with pytest.raises(GHError, match="viewer_repo_permission"):
        fake.viewer_repo_permission("c/d")


def test_fake_satisfies_protocol_with_new_methods():
    fake = FakeGHClient()
    assert isinstance(fake, GHClient)


# ─── prune surface: FakeGHClient (nodes prnbr4kq / prnwt5nq) ───────────────


def _branch(name="feat", sha="b" * 40, protected=False):
    from gitbulk.pr_info import BranchRef
    return BranchRef(name=name, sha=sha, protected=protected)


def _closed(number=1, merged=True, head_ref="feat", head_sha="c" * 40,
            head_repo_slug="dhh1128/gitbulk"):
    from gitbulk.pr_info import ClosedPRRef
    return ClosedPRRef(
        number=number, title=f"PR {number}", url="u", merged=merged,
        base_ref="main", head_ref=head_ref, head_sha=head_sha,
        head_repo_slug=head_repo_slug,
        closed_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )


def test_fake_list_branches_returns_configured():
    fake = FakeGHClient(branches={"o/r": [_branch("main", protected=True)]})
    out = fake.list_branches("o/r")
    assert [b.name for b in out] == ["main"]
    assert fake.call_count["list_branches"] == 1


def test_fake_list_branches_unconfigured_raises():
    with pytest.raises(GHError, match="list_branches"):
        FakeGHClient().list_branches("o/r")


def test_fake_list_branches_exception_value_raises():
    fake = FakeGHClient(branches={"o/r": GHError("boom")})
    with pytest.raises(GHError, match="boom"):
        fake.list_branches("o/r")


def test_fake_closed_prs_for_head_returns_configured():
    fake = FakeGHClient(closed_prs_for_head={("o/r", "feat"): [_closed()]})
    out = fake.closed_prs_for_head("o/r", "feat")
    assert out[0].number == 1
    assert fake.call_count["closed_prs_for_head"] == 1


def test_fake_closed_prs_for_head_unconfigured_raises():
    with pytest.raises(GHError, match="closed_prs_for_head"):
        FakeGHClient().closed_prs_for_head("o/r", "feat")


def test_fake_closed_prs_for_head_exception_value_raises():
    fake = FakeGHClient(closed_prs_for_head={("o/r", "feat"): GHError("x")})
    with pytest.raises(GHError, match="x"):
        fake.closed_prs_for_head("o/r", "feat")


def test_fake_branch_ahead_by_returns_configured():
    fake = FakeGHClient(branch_ahead_by={("o/r", "main", "feat"): 0})
    assert fake.branch_ahead_by("o/r", "main", "feat") == 0
    assert fake.call_count["branch_ahead_by"] == 1


def test_fake_branch_ahead_by_unconfigured_raises():
    with pytest.raises(GHError, match="branch_ahead_by"):
        FakeGHClient().branch_ahead_by("o/r", "main", "feat")


def test_fake_branch_ahead_by_exception_value_raises():
    fake = FakeGHClient(branch_ahead_by={("o/r", "m", "f"): GHError("e")})
    with pytest.raises(GHError, match="e"):
        fake.branch_ahead_by("o/r", "m", "f")


def test_fake_delete_branch_ref_default_success_records_call():
    fake = FakeGHClient()
    assert fake.delete_branch_ref("o/r", "feat") is None
    assert fake.delete_branch_calls == [{"slug": "o/r", "branch": "feat"}]
    assert fake.call_count["delete_branch_ref"] == 1


def test_fake_delete_branch_ref_configured_success():
    fake = FakeGHClient(delete_branch_responses={("o/r", "feat"): None})
    assert fake.delete_branch_ref("o/r", "feat") is None


def test_fake_delete_branch_ref_configured_exception_raises():
    fake = FakeGHClient(delete_branch_responses={("o/r", "feat"): GHError("nope")})
    with pytest.raises(GHError, match="nope"):
        fake.delete_branch_ref("o/r", "feat")
    # Call is still recorded before the raise.
    assert fake.delete_branch_calls == [{"slug": "o/r", "branch": "feat"}]


# ─── FakeGHClient.branch_ref_sha (node prnrv6kq) ───────────────────────────


def test_fake_branch_ref_sha_configured_value():
    fake = FakeGHClient(branch_ref_shas={("o/r", "feat"): "a" * 40})
    assert fake.branch_ref_sha("o/r", "feat") == "a" * 40


def test_fake_branch_ref_sha_configured_none():
    fake = FakeGHClient(branch_ref_shas={("o/r", "feat"): None})
    assert fake.branch_ref_sha("o/r", "feat") is None


def test_fake_branch_ref_sha_configured_exception_raises():
    fake = FakeGHClient(branch_ref_shas={("o/r", "feat"): GHError("boom")})
    with pytest.raises(GHError, match="boom"):
        fake.branch_ref_sha("o/r", "feat")


def test_fake_branch_ref_sha_derives_from_branches_map():
    fake = FakeGHClient(branches={"o/r": [_branch(name="feat", sha="d" * 40)]})
    assert fake.branch_ref_sha("o/r", "feat") == "d" * 40


def test_fake_branch_ref_sha_unknown_slug_is_none():
    fake = FakeGHClient(branches={"o/r": [_branch(name="feat")]})
    assert fake.branch_ref_sha("o/other", "feat") is None


def test_fake_branch_ref_sha_unknown_branch_is_none():
    fake = FakeGHClient(branches={"o/r": [_branch(name="feat")]})
    assert fake.branch_ref_sha("o/r", "missing") is None


def test_fake_branch_ref_sha_errored_branches_entry_is_none():
    fake = FakeGHClient(branches={"o/r": GHError("listing failed")})
    assert fake.branch_ref_sha("o/r", "feat") is None
