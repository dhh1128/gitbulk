"""Tests for :class:`ProductionGHClient`.

Per AGENTS.md "no network in tests", every test in this file mocks
``subprocess.run`` so no actual ``gh`` invocation ever happens. The tests
cover:

  - argv shape for each of the four Protocol methods (the exact list of
    arguments that would be passed to ``gh``)
  - happy-path JSON parsing
  - retry behavior on transient stderr patterns
  - non-retryable error → immediate :class:`GHError`
  - timeout → :class:`GHTimeoutError`
  - ``my_open_prs`` slug semantics (None / empty / unknown-slug-empty)
  - GraphQL response shape → :class:`PRInfo` fixture-based parse
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest

from gitbulk.gh import (
    GHError,
    GHTimeoutError,
    ProductionGHClient,
    _is_retryable_stderr,
    _parse_iso8601,
    _pr_info_from_graphql_node,
)
from gitbulk.pr_info import PRInfo, TimelineEvent


@pytest.fixture(autouse=True)
def _mock_shutil_which(monkeypatch):
    """Make ``shutil.which`` resolve every name to itself.

    ProductionGHClient resolves ``gh_path`` through ``shutil.which`` at
    construction (security-hawk F2 fix, 2026-05-28). For unit tests we
    don't want the host's ``/usr/bin/gh`` presence to leak into argv
    assertions, so we stub the resolver to be the identity function.
    The dedicated F2-behavior tests below override this fixture per
    test as needed.
    """
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: name)


# ─── shared fakes ──────────────────────────────────────────────────────────


class _CompletedFake:
    """Stand-in for :class:`subprocess.CompletedProcess` returned by mocks."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_run_mock(*outcomes: Any):
    """Build a side_effect callable that yields one outcome per call.

    Each outcome may be a :class:`_CompletedFake` (returned) or an
    :class:`Exception` instance (raised). Past the configured list, the
    last outcome repeats — handy for "always fails" tests."""

    state = {"i": 0}

    def side_effect(*args, **kwargs):  # subprocess.run signature
        i = min(state["i"], len(outcomes) - 1)
        state["i"] += 1
        outcome = outcomes[i]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return side_effect


# ─── _is_retryable_stderr ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "stderr",
    [
        "API rate limit exceeded",
        "received 5xx from upstream",
        "context deadline exceeded: timeout",
        "could not resolve host: api.github.com",
        "unexpected EOF on connection",
        # gh emits these on bad-gateway / service-unavailable; the
        # "http 50" marker catches them — was silently un-retried until
        # 2026-05-29 when batched GraphQL exposed it.
        "gh: HTTP 502",
        "gh: HTTP 503",
        "gh: HTTP 504",
    ],
)
def test_is_retryable_stderr_matches_known_transient_patterns(stderr):
    assert _is_retryable_stderr(stderr) is True


def test_is_retryable_stderr_returns_false_for_unknown_errors():
    assert _is_retryable_stderr("404 Not Found") is False
    assert _is_retryable_stderr("HTTP 401: Bad credentials") is False
    assert _is_retryable_stderr("") is False


# ─── _parse_iso8601 ────────────────────────────────────────────────────────


def test_parse_iso8601_handles_trailing_z():
    parsed = _parse_iso8601("2026-05-28T12:34:56Z")
    assert parsed == datetime(2026, 5, 28, 12, 34, 56, tzinfo=timezone.utc)


def test_parse_iso8601_handles_explicit_offset():
    parsed = _parse_iso8601("2026-05-28T12:34:56+00:00")
    assert parsed == datetime(2026, 5, 28, 12, 34, 56, tzinfo=timezone.utc)


# ─── constructor ───────────────────────────────────────────────────────────


def test_constructor_defaults():
    client = ProductionGHClient()
    # Internal attributes are not part of the public API, but we assert on
    # them to lock in the documented defaults (30s timeout, 3 attempts,
    # "gh" on PATH) — see node ghclmp7n.d.
    assert client._gh_path == "gh"
    assert client._default_timeout == 30.0
    assert client._max_retries == 3


def test_constructor_overrides():
    client = ProductionGHClient(
        gh_path="/opt/gh/bin/gh", default_timeout=5.0, max_retries=7
    )
    assert client._gh_path == "/opt/gh/bin/gh"
    assert client._default_timeout == 5.0
    assert client._max_retries == 7


def test_constructor_resolves_bare_name_via_shutil_which(monkeypatch):
    """Security-hawk F2 (2026-05-28): bare-name gh_path is resolved to
    absolute via shutil.which at construction so a later PATH-prepend
    cannot substitute gh."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: f"/canonical/{name}")
    client = ProductionGHClient()
    assert client._gh_path == "/canonical/gh"


def test_constructor_absolute_path_skips_which_lookup(monkeypatch):
    """Absolute paths are taken as-is — shutil.which not called."""
    import shutil

    called = []
    monkeypatch.setattr(
        shutil, "which", lambda name: called.append(name) or "/should/not/use"
    )
    client = ProductionGHClient(gh_path="/explicit/path/to/gh")
    assert client._gh_path == "/explicit/path/to/gh"
    assert called == []


def test_constructor_raises_when_gh_not_found_on_path(monkeypatch):
    """Security-hawk F2: a bare name that doesn't resolve is a loud
    failure, not a deferred error at first invocation."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(GHError, match="could not find"):
        ProductionGHClient()


# ─── authenticated_user ────────────────────────────────────────────────────


def test_authenticated_user_argv_and_parse():
    payload = {"login": "dhh1128", "id": 42}
    side_effect = _make_run_mock(_CompletedFake(0, stdout=json.dumps(payload)))

    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        result = client.authenticated_user()

    assert result == payload
    args, kwargs = mock_run.call_args
    assert args[0] == ["gh", "api", "user"]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 30.0
    assert kwargs["check"] is False


def test_authenticated_user_respects_custom_timeout_and_gh_path():
    side_effect = _make_run_mock(_CompletedFake(0, stdout='{"login":"x"}'))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient(gh_path="/usr/local/bin/gh")
        client.authenticated_user(timeout=12.5)
    args, kwargs = mock_run.call_args
    assert args[0][0] == "/usr/local/bin/gh"
    assert kwargs["timeout"] == 12.5


# ─── org_members ───────────────────────────────────────────────────────────


def test_org_members_argv_and_parse_strips_blank_lines():
    stdout = "dhh1128\nalice\n\n  bob  \n"
    side_effect = _make_run_mock(_CompletedFake(0, stdout=stdout))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        members = client.org_members("provenant-dev")

    assert members == ["dhh1128", "alice", "bob"]
    args, _ = mock_run.call_args
    assert args[0] == [
        "gh",
        "api",
        "orgs/provenant-dev/members",
        "--paginate",
        "--jq",
        ".[].login",
    ]


def test_org_members_empty_response_returns_empty_list():
    side_effect = _make_run_mock(_CompletedFake(0, stdout="\n   \n"))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect):
        client = ProductionGHClient()
        assert client.org_members("empty-org") == []


# ─── default_branch ────────────────────────────────────────────────────────


def test_default_branch_argv_and_parse():
    side_effect = _make_run_mock(_CompletedFake(0, stdout="main\n"))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        branch = client.default_branch("dhh1128/gitbulk")

    assert branch == "main"
    args, _ = mock_run.call_args
    assert args[0] == [
        "gh",
        "api",
        "repos/dhh1128/gitbulk",
        "--jq",
        ".default_branch",
    ]


# ─── retry / timeout discipline ────────────────────────────────────────────


def test_retry_succeeds_after_one_transient_failure():
    """Stderr 'rate limit exceeded' is retryable; we expect a second attempt."""
    side_effect = _make_run_mock(
        _CompletedFake(1, stderr="API rate limit exceeded"),
        _CompletedFake(0, stdout='{"login":"x"}'),
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        with patch("gitbulk.gh.time.sleep") as mock_sleep:
            client = ProductionGHClient()
            result = client.authenticated_user()

    assert result == {"login": "x"}
    assert mock_run.call_count == 2
    # Exponential backoff: first retry waits 2**0 = 1s.
    mock_sleep.assert_called_once_with(1)


def test_retry_exponential_backoff_then_failure_raises_gherror():
    """Three consecutive retryable failures exhaust max_retries=3 attempts."""
    side_effect = _make_run_mock(
        _CompletedFake(1, stderr="rate limit"),
        _CompletedFake(1, stderr="rate limit"),
        _CompletedFake(1, stderr="rate limit final"),
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        with patch("gitbulk.gh.time.sleep") as mock_sleep:
            client = ProductionGHClient()
            with pytest.raises(GHError) as exc_info:
                client.authenticated_user()

    assert mock_run.call_count == 3
    # Backoff between attempts: 1s after first, 2s after second; no sleep
    # after the third (final) attempt.
    assert [c.args for c in mock_sleep.call_args_list] == [(1,), (2,)]
    assert "exhausted 3 attempts" in str(exc_info.value)
    assert "rate limit final" in str(exc_info.value)
    assert exc_info.value.command == ("gh", "api", "user")


def test_non_retryable_failure_raises_immediately():
    """Stderr not matching any retryable marker → immediate GHError, no sleep."""
    side_effect = _make_run_mock(
        _CompletedFake(1, stderr="HTTP 401: Bad credentials"),
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        with patch("gitbulk.gh.time.sleep") as mock_sleep:
            client = ProductionGHClient()
            with pytest.raises(GHError) as exc_info:
                client.authenticated_user()

    assert mock_run.call_count == 1
    mock_sleep.assert_not_called()
    assert "HTTP 401" in str(exc_info.value)
    assert "exhausted" not in str(exc_info.value)
    assert exc_info.value.command == ("gh", "api", "user")


def test_timeout_exhausted_raises_ghtimeouterror():
    """All attempts time out → GHTimeoutError (subclass of GHError)."""
    timeouts = [
        subprocess.TimeoutExpired(cmd=["gh", "api", "user"], timeout=30.0)
        for _ in range(3)
    ]
    side_effect = _make_run_mock(*timeouts)
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        with patch("gitbulk.gh.time.sleep"):
            client = ProductionGHClient()
            with pytest.raises(GHTimeoutError) as exc_info:
                client.authenticated_user()

    assert mock_run.call_count == 3
    assert "timeout" in str(exc_info.value).lower()
    assert exc_info.value.command == ("gh", "api", "user")


def test_timeout_then_success_does_not_raise():
    side_effect = _make_run_mock(
        subprocess.TimeoutExpired(cmd=["gh"], timeout=1.0),
        _CompletedFake(0, stdout='{"login":"x"}'),
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect):
        with patch("gitbulk.gh.time.sleep"):
            client = ProductionGHClient()
            assert client.authenticated_user() == {"login": "x"}


def test_max_retries_one_means_no_retry():
    """max_retries=1 means exactly one attempt, no backoff."""
    side_effect = _make_run_mock(_CompletedFake(1, stderr="rate limit"))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        with patch("gitbulk.gh.time.sleep") as mock_sleep:
            client = ProductionGHClient(max_retries=1)
            with pytest.raises(GHError):
                client.authenticated_user()

    assert mock_run.call_count == 1
    mock_sleep.assert_not_called()


# ─── my_open_prs: argv shape and slug semantics ────────────────────────────


_GRAPHQL_FIXTURE = {
    "data": {
        "search": {
            "nodes": [
                {
                    "number": 42,
                    "title": "Fix the thing",
                    "url": "https://github.com/dhh1128/gitbulk/pull/42",
                    "author": {"login": "dhh1128"},
                    "isDraft": False,
                    "state": "OPEN",
                    "baseRefName": "main",
                    "headRefName": "fix-thing",
                    "headRefOid": "abc123",
                    "createdAt": "2026-05-27T10:00:00Z",
                    "updatedAt": "2026-05-28T11:00:00Z",
                    "mergeStateStatus": "CLEAN",
                    "reviewDecision": "APPROVED",
                    "labels": {"nodes": [{"name": "bug"}, {"name": "ready"}]},
                    "commits": {
                        "nodes": [
                            {
                                "commit": {
                                    "committedDate": "2026-05-28T09:00:00Z",
                                    "statusCheckRollup": {"state": "SUCCESS"},
                                }
                            }
                        ]
                    },
                    "reviewThreads": {
                        "nodes": [
                            {"isResolved": True},
                            {"isResolved": True},
                        ]
                    },
                    "timelineItems": {
                        "nodes": [
                            {
                                "__typename": "PullRequestReview",
                                "submittedAt": "2026-05-28T10:30:00Z",
                                "state": "APPROVED",
                            },
                        ],
                        "pageInfo": {"hasPreviousPage": False},
                    },
                    "repository": {"nameWithOwner": "dhh1128/gitbulk"},
                }
            ]
        }
    }
}


def test_my_open_prs_argv_no_slugs_uses_open_search():
    side_effect = _make_run_mock(
        _CompletedFake(0, stdout=json.dumps({"data": {"search": {"nodes": []}}}))
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        result = client.my_open_prs()

    assert result == {}
    args, _ = mock_run.call_args
    argv = args[0]
    assert argv[0:3] == ["gh", "api", "graphql"]
    assert "-F" in argv
    q_index = argv.index("-F")
    assert argv[q_index + 1] == "q=author:@me is:open is:pr"
    # GraphQL document is passed via -f query=...
    assert "-f" in argv
    f_index = argv.index("-f")
    assert argv[f_index + 1].startswith("query=")
    assert "search(query: $q" in argv[f_index + 1]
    assert "mergeStateStatus" in argv[f_index + 1]
    assert "statusCheckRollup" in argv[f_index + 1]
    assert "reviewThreads" in argv[f_index + 1]
    assert "timelineItems" in argv[f_index + 1]


def test_my_open_prs_argv_custom_author():
    side_effect = _make_run_mock(
        _CompletedFake(0, stdout=json.dumps({"data": {"search": {"nodes": []}}}))
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        client.my_open_prs(author="octocat")

    args, _ = mock_run.call_args
    argv = args[0]
    assert argv[argv.index("-F") + 1] == "q=author:octocat is:open is:pr"


def test_my_open_prs_argv_author_none_omits_qualifier():
    """author=None → search any author (no author: qualifier)."""
    side_effect = _make_run_mock(
        _CompletedFake(0, stdout=json.dumps({"data": {"search": {"nodes": []}}}))
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        client.my_open_prs(author=None)

    args, _ = mock_run.call_args
    argv = args[0]
    q_value = argv[argv.index("-F") + 1]
    assert q_value == "q=is:open is:pr"
    assert "author:" not in q_value


def test_my_open_prs_argv_includes_repo_terms_for_each_slug():
    side_effect = _make_run_mock(
        _CompletedFake(0, stdout=json.dumps({"data": {"search": {"nodes": []}}}))
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        client.my_open_prs(slugs=["a/x", "b/y"])

    args, _ = mock_run.call_args
    argv = args[0]
    q_value = argv[argv.index("-F") + 1]
    assert q_value == "q=author:@me is:open is:pr repo:a/x repo:b/y"


def test_my_open_prs_with_slugs_emits_empty_list_for_unknown_slug():
    """Matches FakeGHClient: every requested slug appears in result."""
    side_effect = _make_run_mock(
        _CompletedFake(0, stdout=json.dumps(_GRAPHQL_FIXTURE))
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect):
        client = ProductionGHClient()
        result = client.my_open_prs(slugs=["dhh1128/gitbulk", "missing/repo"])

    assert set(result.keys()) == {"dhh1128/gitbulk", "missing/repo"}
    assert result["missing/repo"] == []
    assert len(result["dhh1128/gitbulk"]) == 1
    pr = result["dhh1128/gitbulk"][0]
    assert pr.number == 42
    assert pr.author == "dhh1128"
    assert pr.review_decision == "APPROVED"
    assert pr.checks_status == "SUCCESS"
    assert pr.labels == ("bug", "ready")
    assert pr.mergeable_state == "CLEAN"
    assert pr.last_pushed_at == datetime(2026, 5, 28, 9, 0, 0, tzinfo=timezone.utc)
    assert pr.created_at == datetime(2026, 5, 27, 10, 0, 0, tzinfo=timezone.utc)


def test_my_open_prs_with_empty_slug_list_makes_no_network_call():
    """Empty iterable → zero chunks → no search call at all, returns {}.

    The old single-query implementation wastefully searched all-my-PRs
    (no repo filter) then discarded everything for lack of slug keys.
    The chunked implementation correctly does nothing for an empty list."""
    with patch("gitbulk.gh.subprocess.run") as mock_run:
        client = ProductionGHClient()
        result = client.my_open_prs(slugs=[])

    assert result == {}
    assert mock_run.call_count == 0


def test_my_open_prs_groups_by_repository_name_with_owner():
    """When slugs is None, PRs are grouped by their actual repo.nameWithOwner."""
    payload = {
        "data": {
            "search": {
                "nodes": [
                    dict(
                        _GRAPHQL_FIXTURE["data"]["search"]["nodes"][0],
                        repository={"nameWithOwner": "a/x"},
                    ),
                    dict(
                        _GRAPHQL_FIXTURE["data"]["search"]["nodes"][0],
                        number=43,
                        repository={"nameWithOwner": "b/y"},
                    ),
                    dict(
                        _GRAPHQL_FIXTURE["data"]["search"]["nodes"][0],
                        number=44,
                        repository={"nameWithOwner": "a/x"},
                    ),
                ]
            }
        }
    }
    side_effect = _make_run_mock(_CompletedFake(0, stdout=json.dumps(payload)))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect):
        client = ProductionGHClient()
        result = client.my_open_prs()

    assert set(result.keys()) == {"a/x", "b/y"}
    assert [p.number for p in result["a/x"]] == [42, 44]
    assert [p.number for p in result["b/y"]] == [43]


def test_my_open_prs_skips_null_nodes_in_search_result():
    """GraphQL search returns null entries when an item isn't a PullRequest.
    Our query uses is:pr, but the field is still nullable; we defensively skip."""
    payload = {
        "data": {
            "search": {
                "nodes": [
                    None,
                    _GRAPHQL_FIXTURE["data"]["search"]["nodes"][0],
                ]
            }
        }
    }
    side_effect = _make_run_mock(_CompletedFake(0, stdout=json.dumps(payload)))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect):
        client = ProductionGHClient()
        result = client.my_open_prs()

    assert len(result["dhh1128/gitbulk"]) == 1


# ─── pagination + repo-chunking ────────────────────────────────────────────


def _search_page(nodes, *, has_next=False, end_cursor=None):
    """Build a search-page payload with explicit pageInfo."""
    return {
        "data": {
            "search": {
                "pageInfo": {
                    "hasNextPage": has_next,
                    "endCursor": end_cursor,
                },
                "nodes": nodes,
            }
        }
    }


def test_my_open_prs_paginates_until_no_next_page():
    """>100 results across pages: the loop follows endCursor until
    hasNextPage is false, accumulating all nodes."""
    n1 = dict(_GRAPHQL_FIXTURE["data"]["search"]["nodes"][0], number=1)
    n2 = dict(_GRAPHQL_FIXTURE["data"]["search"]["nodes"][0], number=2)
    n3 = dict(_GRAPHQL_FIXTURE["data"]["search"]["nodes"][0], number=3)
    side_effect = _make_run_mock(
        _CompletedFake(0, stdout=json.dumps(
            _search_page([n1], has_next=True, end_cursor="CURSOR1"))),
        _CompletedFake(0, stdout=json.dumps(
            _search_page([n2], has_next=True, end_cursor="CURSOR2"))),
        _CompletedFake(0, stdout=json.dumps(
            _search_page([n3], has_next=False))),
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        result = client.my_open_prs(slugs=["dhh1128/gitbulk"])

    assert mock_run.call_count == 3  # three pages
    assert [p.number for p in result["dhh1128/gitbulk"]] == [1, 2, 3]


def test_my_open_prs_passes_cursor_on_subsequent_pages():
    """Page 2's argv carries `-f after=<endCursor>` from page 1; page 1
    carries no `after`."""
    n1 = dict(_GRAPHQL_FIXTURE["data"]["search"]["nodes"][0], number=1)
    n2 = dict(_GRAPHQL_FIXTURE["data"]["search"]["nodes"][0], number=2)
    side_effect = _make_run_mock(
        _CompletedFake(0, stdout=json.dumps(
            _search_page([n1], has_next=True, end_cursor="ABC123"))),
        _CompletedFake(0, stdout=json.dumps(
            _search_page([n2], has_next=False))),
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        client.my_open_prs(slugs=["dhh1128/gitbulk"])

    call1_argv = mock_run.call_args_list[0][0][0]
    call2_argv = mock_run.call_args_list[1][0][0]
    assert "after=ABC123" not in " ".join(call1_argv)
    assert "after=ABC123" in call2_argv


def test_my_open_prs_stops_when_next_page_but_no_cursor():
    """Defensive: hasNextPage=true with an empty endCursor must not loop
    forever — we stop after that page."""
    n1 = dict(_GRAPHQL_FIXTURE["data"]["search"]["nodes"][0], number=1)
    side_effect = _make_run_mock(
        _CompletedFake(0, stdout=json.dumps(
            _search_page([n1], has_next=True, end_cursor=None))),
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        result = client.my_open_prs(slugs=["dhh1128/gitbulk"])
    assert mock_run.call_count == 1
    assert len(result["dhh1128/gitbulk"]) == 1


def test_my_open_prs_chunks_repo_qualifiers_at_50():
    """>50 slugs → multiple search calls, each with ≤50 repo: terms.
    GitHub silently caps qualifiers, so coalescing all into one query
    would drop repos."""
    slugs = [f"owner/repo{i}" for i in range(120)]  # → 3 chunks (50,50,20)
    side_effect = _make_run_mock(
        _CompletedFake(0, stdout=json.dumps(_search_page([]))),
        _CompletedFake(0, stdout=json.dumps(_search_page([]))),
        _CompletedFake(0, stdout=json.dumps(_search_page([]))),
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        result = client.my_open_prs(slugs=slugs)

    # 3 chunks → 3 search calls.
    assert mock_run.call_count == 3
    # Each chunk's q has at most 50 repo: terms.
    for call in mock_run.call_args_list:
        argv = call[0][0]
        q = argv[argv.index("-F") + 1]
        assert q.count("repo:") <= 50
    # All requested slugs are still pre-seeded as keys.
    assert len(result) == 120


def test_my_open_prs_chunk_results_merge_across_chunks():
    """A PR found in chunk 2 lands in the result alongside chunk-1 PRs."""
    slugs = [f"owner/repo{i}" for i in range(60)]  # 2 chunks (50 + 10)
    pr_in_chunk1 = dict(
        _GRAPHQL_FIXTURE["data"]["search"]["nodes"][0],
        number=1, repository={"nameWithOwner": "owner/repo0"},
    )
    pr_in_chunk2 = dict(
        _GRAPHQL_FIXTURE["data"]["search"]["nodes"][0],
        number=2, repository={"nameWithOwner": "owner/repo55"},
    )
    side_effect = _make_run_mock(
        _CompletedFake(0, stdout=json.dumps(_search_page([pr_in_chunk1]))),
        _CompletedFake(0, stdout=json.dumps(_search_page([pr_in_chunk2]))),
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect):
        client = ProductionGHClient()
        result = client.my_open_prs(slugs=slugs)
    assert [p.number for p in result["owner/repo0"]] == [1]
    assert [p.number for p in result["owner/repo55"]] == [2]


def test_my_open_prs_runaway_guard_caps_at_100_pages():
    """Pathological backend: every page says hasNextPage=true with a
    valid cursor. The hard 100-page cap stops the loop rather than
    spinning forever."""
    n = dict(_GRAPHQL_FIXTURE["data"]["search"]["nodes"][0], number=1)
    # _make_run_mock repeats its last outcome indefinitely, so this one
    # "always hasNext" page drives the loop to its 100-iteration cap.
    side_effect = _make_run_mock(
        _CompletedFake(0, stdout=json.dumps(
            _search_page([n], has_next=True, end_cursor="STUCK"))),
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        result = client.my_open_prs(slugs=["dhh1128/gitbulk"])
    # Exactly 100 page fetches, then the guard fires.
    assert mock_run.call_count == 100
    assert len(result["dhh1128/gitbulk"]) == 100


def test_my_open_prs_missing_pageinfo_treated_as_single_page():
    """A payload with no pageInfo (older backend / fixture) is one page."""
    payload = {"data": {"search": {"nodes": [
        _GRAPHQL_FIXTURE["data"]["search"]["nodes"][0],
    ]}}}
    side_effect = _make_run_mock(_CompletedFake(0, stdout=json.dumps(payload)))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        result = client.my_open_prs(slugs=["dhh1128/gitbulk"])
    assert mock_run.call_count == 1
    assert len(result["dhh1128/gitbulk"]) == 1


# ─── _pr_info_from_graphql_node: parser edge cases ─────────────────────────


def _node_template() -> dict[str, Any]:
    """Deep-ish copy of the fixture's PR node so each test can mutate it."""
    return json.loads(
        json.dumps(_GRAPHQL_FIXTURE["data"]["search"]["nodes"][0])
    )


def test_pr_info_parser_handles_full_node():
    pr = _pr_info_from_graphql_node(_node_template())
    assert isinstance(pr, PRInfo)
    assert pr.slug == "dhh1128/gitbulk"
    assert pr.number == 42
    assert pr.title == "Fix the thing"
    assert pr.author == "dhh1128"
    assert pr.head_sha == "abc123"
    assert pr.is_draft is False
    assert pr.state == "OPEN"
    assert pr.labels == ("bug", "ready")
    assert pr.last_pushed_at is not None
    assert pr.checks_status == "SUCCESS"


def test_pr_info_parser_handles_null_author():
    """GitHub returns null author for deleted accounts."""
    node = _node_template()
    node["author"] = None
    pr = _pr_info_from_graphql_node(node)
    assert pr.author == ""


def test_pr_info_parser_handles_null_status_check_rollup():
    """A PR with no checks at all has statusCheckRollup = null."""
    node = _node_template()
    node["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = None
    pr = _pr_info_from_graphql_node(node)
    assert pr.checks_status is None


def test_pr_info_parser_handles_no_commits():
    """Degenerate PRs (no head commits) have commits.nodes = []."""
    node = _node_template()
    node["commits"] = {"nodes": []}
    pr = _pr_info_from_graphql_node(node)
    assert pr.last_pushed_at is None
    assert pr.checks_status is None


def test_pr_info_parser_handles_missing_committed_date():
    """If the commit object exists but committedDate is null, we tolerate it."""
    node = _node_template()
    node["commits"]["nodes"][0]["commit"]["committedDate"] = None
    pr = _pr_info_from_graphql_node(node)
    assert pr.last_pushed_at is None


def test_pr_info_parser_handles_empty_labels():
    node = _node_template()
    node["labels"] = {"nodes": []}
    pr = _pr_info_from_graphql_node(node)
    assert pr.labels == ()


def test_pr_info_parser_handles_null_labels_container():
    node = _node_template()
    node["labels"] = None
    pr = _pr_info_from_graphql_node(node)
    assert pr.labels == ()


def test_pr_info_parser_handles_null_commits_container():
    node = _node_template()
    node["commits"] = None
    pr = _pr_info_from_graphql_node(node)
    assert pr.last_pushed_at is None
    assert pr.checks_status is None


def test_pr_info_parser_handles_null_commit_inside_node():
    """commits.nodes[0].commit can be null when the head ref was deleted."""
    node = _node_template()
    node["commits"]["nodes"][0]["commit"] = None
    pr = _pr_info_from_graphql_node(node)
    assert pr.last_pushed_at is None
    assert pr.checks_status is None


def test_pr_info_parser_handles_label_node_with_missing_name():
    """Defensive: a label node with no name field is silently dropped."""
    node = _node_template()
    node["labels"] = {"nodes": [{"name": "keeper"}, {}, None, {"name": ""}]}
    pr = _pr_info_from_graphql_node(node)
    assert pr.labels == ("keeper",)


def test_pr_info_parser_handles_missing_mergeable_state():
    node = _node_template()
    node.pop("mergeStateStatus")
    pr = _pr_info_from_graphql_node(node)
    assert pr.mergeable_state is None


# ─── reviewThreads + timelineItems parsing ──────────────────────────────────


def test_pr_info_parser_counts_unresolved_review_threads():
    node = _node_template()
    node["reviewThreads"] = {
        "nodes": [
            {"isResolved": True},
            {"isResolved": False},
            {"isResolved": False},
            {"isResolved": True},
        ]
    }
    pr = _pr_info_from_graphql_node(node)
    assert pr.unresolved_thread_count == 2


def test_pr_info_parser_unresolved_threads_zero_when_all_resolved():
    pr = _pr_info_from_graphql_node(_node_template())
    assert pr.unresolved_thread_count == 0


def test_pr_info_parser_tolerates_missing_reviewthreads_container():
    """An older fixture / a query that doesn't include reviewThreads."""
    node = _node_template()
    node.pop("reviewThreads", None)
    pr = _pr_info_from_graphql_node(node)
    assert pr.unresolved_thread_count == 0


def test_pr_info_parser_tolerates_null_thread_node():
    node = _node_template()
    node["reviewThreads"] = {"nodes": [None, {"isResolved": False}]}
    pr = _pr_info_from_graphql_node(node)
    assert pr.unresolved_thread_count == 1


def test_pr_info_parser_thread_missing_isresolved_treated_as_unresolved():
    """Defensive: ``isResolved`` field absent → assume unresolved
    (safer to over-skip than to merge an open thread)."""
    node = _node_template()
    node["reviewThreads"] = {"nodes": [{}]}
    pr = _pr_info_from_graphql_node(node)
    assert pr.unresolved_thread_count == 1


def test_pr_info_parser_extracts_approved_review_into_timeline():
    pr = _pr_info_from_graphql_node(_node_template())
    assert len(pr.timeline_events) == 1
    event = pr.timeline_events[0]
    assert event.kind == "approved"
    assert event.at == datetime(2026, 5, 28, 10, 30, 0, tzinfo=timezone.utc)


def test_pr_info_parser_extracts_changes_requested_review():
    node = _node_template()
    node["timelineItems"] = {
        "nodes": [
            {
                "__typename": "PullRequestReview",
                "submittedAt": "2026-05-28T09:30:00Z",
                "state": "CHANGES_REQUESTED",
            }
        ],
        "pageInfo": {"hasPreviousPage": False},
    }
    pr = _pr_info_from_graphql_node(node)
    assert len(pr.timeline_events) == 1
    assert pr.timeline_events[0].kind == "changes_requested"


def test_pr_info_parser_extracts_draft_and_ready_events():
    node = _node_template()
    node["timelineItems"] = {
        "nodes": [
            {
                "__typename": "ConvertToDraftEvent",
                "createdAt": "2026-05-28T08:00:00Z",
            },
            {
                "__typename": "ReadyForReviewEvent",
                "createdAt": "2026-05-28T09:00:00Z",
            },
        ],
        "pageInfo": {"hasPreviousPage": False},
    }
    pr = _pr_info_from_graphql_node(node)
    kinds = [e.kind for e in pr.timeline_events]
    assert kinds == ["draft", "ready"]


def test_pr_info_parser_ignores_non_gating_review_states():
    """COMMENTED, DISMISSED, PENDING reviews are not gates and should
    not produce timeline events."""
    node = _node_template()
    node["timelineItems"] = {
        "nodes": [
            {
                "__typename": "PullRequestReview",
                "submittedAt": "2026-05-28T09:30:00Z",
                "state": "COMMENTED",
            },
            {
                "__typename": "PullRequestReview",
                "submittedAt": "2026-05-28T09:31:00Z",
                "state": "DISMISSED",
            },
            {
                "__typename": "PullRequestReview",
                "submittedAt": None,
                "state": "PENDING",
            },
        ],
        "pageInfo": {"hasPreviousPage": False},
    }
    pr = _pr_info_from_graphql_node(node)
    assert pr.timeline_events == ()


def test_pr_info_parser_sets_timeline_capped_from_pageinfo():
    node = _node_template()
    node["timelineItems"]["pageInfo"]["hasPreviousPage"] = True
    pr = _pr_info_from_graphql_node(node)
    assert pr.timeline_capped is True


def test_pr_info_parser_timeline_capped_defaults_false():
    pr = _pr_info_from_graphql_node(_node_template())
    assert pr.timeline_capped is False


def test_pr_info_parser_tolerates_missing_timelineitems_container():
    node = _node_template()
    node.pop("timelineItems", None)
    pr = _pr_info_from_graphql_node(node)
    assert pr.timeline_events == ()
    assert pr.timeline_capped is False


def test_pr_info_parser_skips_ready_event_without_createdat():
    """Defensive: ReadyForReviewEvent without createdAt is dropped."""
    node = _node_template()
    node["timelineItems"] = {
        "nodes": [{"__typename": "ReadyForReviewEvent", "createdAt": None}],
        "pageInfo": {"hasPreviousPage": False},
    }
    pr = _pr_info_from_graphql_node(node)
    assert pr.timeline_events == ()


def test_pr_info_parser_skips_draft_event_without_createdat():
    node = _node_template()
    node["timelineItems"] = {
        "nodes": [{"__typename": "ConvertToDraftEvent", "createdAt": None}],
        "pageInfo": {"hasPreviousPage": False},
    }
    pr = _pr_info_from_graphql_node(node)
    assert pr.timeline_events == ()


def test_pr_info_parser_skips_unknown_typename():
    """Defensive: a __typename we don't recognize is ignored."""
    node = _node_template()
    node["timelineItems"] = {
        "nodes": [{"__typename": "SomeFutureEvent", "createdAt": "2026-05-28T09:00:00Z"}],
        "pageInfo": {"hasPreviousPage": False},
    }
    pr = _pr_info_from_graphql_node(node)
    assert pr.timeline_events == ()


def test_pr_info_parser_skips_null_timeline_node():
    node = _node_template()
    node["timelineItems"] = {
        "nodes": [
            None,
            {
                "__typename": "ReadyForReviewEvent",
                "createdAt": "2026-05-28T09:00:00Z",
            },
        ],
        "pageInfo": {"hasPreviousPage": False},
    }
    pr = _pr_info_from_graphql_node(node)
    assert len(pr.timeline_events) == 1
    assert pr.timeline_events[0].kind == "ready"


# ─── search payload missing keys (defensive) ───────────────────────────────


def test_my_open_prs_handles_missing_data_key_gracefully():
    """If gh returns ``{}`` (no data key), we should produce an empty dict
    rather than a KeyError. Defensive: the call still 'succeeded' from
    subprocess's POV (returncode 0), so we don't want to raise."""
    side_effect = _make_run_mock(_CompletedFake(0, stdout="{}"))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect):
        client = ProductionGHClient()
        result = client.my_open_prs()
    assert result == {}


# ─── merge_pr ──────────────────────────────────────────────────────────────


def test_merge_pr_default_argv_merge_delete_branch():
    """Default method is `merge` (true merge commit) per gji4dyze;
    delete_branch defaults to True so `--delete-branch` is on the argv."""
    side_effect = _make_run_mock(_CompletedFake(0, stdout=""))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        result = client.merge_pr("dhh1128/gitbulk", 42)

    assert result == {}
    args, _ = mock_run.call_args
    assert args[0] == [
        "gh",
        "pr",
        "merge",
        "42",
        "--repo",
        "dhh1128/gitbulk",
        "--merge",
        "--delete-branch",
    ]


def test_merge_pr_method_merge_uses_merge_flag():
    side_effect = _make_run_mock(_CompletedFake(0, stdout=""))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        client.merge_pr("a/b", 1, method="merge")
    args, _ = mock_run.call_args
    assert "--merge" in args[0]
    assert "--squash" not in args[0]


def test_merge_pr_method_rebase_uses_rebase_flag():
    side_effect = _make_run_mock(_CompletedFake(0, stdout=""))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        client.merge_pr("a/b", 1, method="rebase")
    args, _ = mock_run.call_args
    assert "--rebase" in args[0]


def test_merge_pr_no_delete_branch_omits_flag():
    side_effect = _make_run_mock(_CompletedFake(0, stdout=""))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        client.merge_pr("a/b", 1, delete_branch=False)
    args, _ = mock_run.call_args
    assert "--delete-branch" not in args[0]


def test_merge_pr_parses_json_stdout_when_present():
    """Future-compat: a gh version that emits JSON on stdout should be
    parsed and returned as a dict."""
    side_effect = _make_run_mock(
        _CompletedFake(0, stdout='{"merged": true, "sha": "abc"}')
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect):
        client = ProductionGHClient()
        result = client.merge_pr("a/b", 1)
    assert result == {"merged": True, "sha": "abc"}


def test_merge_pr_non_json_stdout_wrapped_in_stdout_key():
    """Current gh prints human text on success; we tolerate that and
    return it under a stable key rather than raising."""
    side_effect = _make_run_mock(
        _CompletedFake(0, stdout="Pull request #42 merged.\n")
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect):
        client = ProductionGHClient()
        result = client.merge_pr("a/b", 42)
    assert result == {"stdout": "Pull request #42 merged."}


def test_merge_pr_raises_on_non_clean_state():
    """Non-retryable stderr (e.g. 'Pull request is not mergeable') →
    immediate GHError without retries."""
    side_effect = _make_run_mock(
        _CompletedFake(1, stderr="Pull request is not mergeable: dirty branch")
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        with patch("gitbulk.gh.time.sleep") as mock_sleep:
            client = ProductionGHClient()
            with pytest.raises(GHError) as exc_info:
                client.merge_pr("a/b", 1)

    assert mock_run.call_count == 1
    mock_sleep.assert_not_called()
    assert "not mergeable" in str(exc_info.value)


def test_merge_pr_respects_timeout_kwarg():
    side_effect = _make_run_mock(_CompletedFake(0, stdout=""))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        client.merge_pr("a/b", 1, timeout=7.5)
    _, kwargs = mock_run.call_args
    assert kwargs["timeout"] == 7.5


# ─── fetch_pr_comments ─────────────────────────────────────────────────────


_PR_COMMENTS_FIXTURE = {
    "data": {
        "repository": {
            "pullRequest": {
                "comments": {
                    "nodes": [
                        {
                            "author": {"login": "alice"},
                            "body": "early review note",
                            "createdAt": "2026-05-20T12:00:00Z",
                        },
                        {
                            "author": {"login": "gitbulk-bot"},
                            "body": "stale heads-up <!-- gitbulk: stale-warning v1 -->",
                            "createdAt": "2026-05-25T08:00:00Z",
                        },
                    ]
                }
            }
        }
    }
}


def test_fetch_pr_comments_argv_and_parse():
    side_effect = _make_run_mock(
        _CompletedFake(0, stdout=json.dumps(_PR_COMMENTS_FIXTURE))
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        result = client.fetch_pr_comments("dhh1128/gitbulk", 42)

    assert len(result) == 2
    assert result[0].author_login == "alice"
    assert result[1].body.startswith("stale heads-up")
    assert result[1].at == datetime(2026, 5, 25, 8, 0, 0, tzinfo=timezone.utc)
    args, _ = mock_run.call_args
    argv = args[0]
    assert argv[0:3] == ["gh", "api", "graphql"]
    # Verify the three -F vars
    f_indices = [i for i, a in enumerate(argv) if a == "-F"]
    f_values = {argv[i + 1] for i in f_indices}
    assert "owner=dhh1128" in f_values
    assert "name=gitbulk" in f_values
    assert "number=42" in f_values


def test_fetch_pr_comments_empty_when_no_pr():
    """Missing pullRequest node → empty list (defensive)."""
    payload = {"data": {"repository": {"pullRequest": None}}}
    side_effect = _make_run_mock(_CompletedFake(0, stdout=json.dumps(payload)))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect):
        client = ProductionGHClient()
        assert client.fetch_pr_comments("a/b", 1) == []


def test_fetch_pr_comments_skips_null_node_and_no_createdat():
    payload = {
        "data": {
            "repository": {
                "pullRequest": {
                    "comments": {
                        "nodes": [
                            None,
                            {
                                "author": {"login": "bob"},
                                "body": "hi",
                                # createdAt missing
                            },
                            {
                                "author": None,
                                "body": "anon comment",
                                "createdAt": "2026-05-20T12:00:00Z",
                            },
                        ]
                    }
                }
            }
        }
    }
    side_effect = _make_run_mock(_CompletedFake(0, stdout=json.dumps(payload)))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect):
        client = ProductionGHClient()
        result = client.fetch_pr_comments("a/b", 1)
    # Null and missing-createdAt are dropped; null-author becomes "".
    assert len(result) == 1
    assert result[0].author_login == ""
    assert result[0].body == "anon comment"


def test_fetch_pr_comments_handles_empty_data():
    """When gh returns {} (no data key)."""
    side_effect = _make_run_mock(_CompletedFake(0, stdout="{}"))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect):
        client = ProductionGHClient()
        assert client.fetch_pr_comments("a/b", 1) == []


# ─── post_comment ──────────────────────────────────────────────────────────


def test_post_comment_argv():
    side_effect = _make_run_mock(_CompletedFake(0, stdout=""))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        result = client.post_comment("dhh1128/gitbulk", 42, "hello world")
    assert result == {}
    args, _ = mock_run.call_args
    assert args[0] == [
        "gh",
        "pr",
        "comment",
        "42",
        "--repo",
        "dhh1128/gitbulk",
        "--body",
        "hello world",
    ]


def test_post_comment_parses_json_when_provided():
    side_effect = _make_run_mock(
        _CompletedFake(0, stdout='{"url":"https://github.com/x/y/issues/1#c"}')
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect):
        client = ProductionGHClient()
        result = client.post_comment("a/b", 1, "x")
    assert result == {"url": "https://github.com/x/y/issues/1#c"}


def test_post_comment_returns_stdout_on_unparseable():
    side_effect = _make_run_mock(
        _CompletedFake(0, stdout="https://github.com/a/b/issues/1\n")
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect):
        client = ProductionGHClient()
        result = client.post_comment("a/b", 1, "x")
    assert "stdout" in result


# ─── close_pr ──────────────────────────────────────────────────────────────


def test_close_pr_argv_default_no_delete_branch():
    """Stale-close keeps the branch per design (warn-and-close decision)."""
    side_effect = _make_run_mock(_CompletedFake(0, stdout=""))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        result = client.close_pr("dhh1128/gitbulk", 42)
    assert result == {}
    args, _ = mock_run.call_args
    assert args[0] == [
        "gh",
        "pr",
        "close",
        "42",
        "--repo",
        "dhh1128/gitbulk",
    ]


def test_close_pr_argv_with_delete_branch():
    side_effect = _make_run_mock(_CompletedFake(0, stdout=""))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        client.close_pr("a/b", 1, delete_branch=True)
    args, _ = mock_run.call_args
    assert "--delete-branch" in args[0]


def test_close_pr_parses_json_when_provided():
    side_effect = _make_run_mock(
        _CompletedFake(0, stdout='{"state":"CLOSED"}')
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect):
        client = ProductionGHClient()
        result = client.close_pr("a/b", 1)
    assert result == {"state": "CLOSED"}


def test_close_pr_returns_stdout_on_unparseable():
    side_effect = _make_run_mock(_CompletedFake(0, stdout="closed!\n"))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect):
        client = ProductionGHClient()
        result = client.close_pr("a/b", 1)
    assert "stdout" in result


# ─── fetch_merge_commit_sha ────────────────────────────────────────────────


def test_fetch_merge_commit_sha_argv_and_parse():
    payload = {"mergeCommit": {"oid": "f" * 40}}
    side_effect = _make_run_mock(_CompletedFake(0, stdout=json.dumps(payload)))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        result = client.fetch_merge_commit_sha("dhh1128/gitbulk", 42)
    assert result == "f" * 40
    args, _ = mock_run.call_args
    assert args[0] == [
        "gh", "pr", "view", "42", "--repo", "dhh1128/gitbulk", "--json", "mergeCommit",
    ]


def test_fetch_merge_commit_sha_null_merge_returns_none():
    """PR not merged → mergeCommit is null."""
    payload = {"mergeCommit": None}
    side_effect = _make_run_mock(_CompletedFake(0, stdout=json.dumps(payload)))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect):
        client = ProductionGHClient()
        assert client.fetch_merge_commit_sha("a/b", 1) is None


def test_fetch_merge_commit_sha_unparseable_json_returns_none():
    side_effect = _make_run_mock(_CompletedFake(0, stdout="not json"))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect):
        client = ProductionGHClient()
        assert client.fetch_merge_commit_sha("a/b", 1) is None


def test_fetch_merge_commit_sha_missing_oid_returns_none():
    """mergeCommit present but no oid (defensive)."""
    payload = {"mergeCommit": {}}
    side_effect = _make_run_mock(_CompletedFake(0, stdout=json.dumps(payload)))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect):
        client = ProductionGHClient()
        assert client.fetch_merge_commit_sha("a/b", 1) is None


# ─── fetch_check_runs ──────────────────────────────────────────────────────


def test_fetch_check_runs_argv_and_parse_ndjson():
    """gh --jq emits one JSON object per line (ndjson). Parser handles
    blank lines and tolerates JSON errors."""
    lines = [
        json.dumps({
            "name": "test", "status": "completed", "conclusion": "success",
            "details_url": "https://x", "completed_at": "2026-05-28T10:00:00Z",
        }),
        "",  # blank line
        "{not-json",  # malformed
        json.dumps({
            "name": "deploy", "status": "completed", "conclusion": "failure",
            "details_url": "https://y", "completed_at": None,
        }),
    ]
    stdout = "\n".join(lines) + "\n"
    side_effect = _make_run_mock(_CompletedFake(0, stdout=stdout))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        result = client.fetch_check_runs("dhh1128/gitbulk", "abc123")
    args, _ = mock_run.call_args
    assert args[0][:3] == ["gh", "api", "repos/dhh1128/gitbulk/commits/abc123/check-runs"]
    assert "--jq" in args[0]
    # Two valid rows; the malformed line is skipped.
    assert len(result) == 2
    assert result[0].name == "test"
    assert result[0].conclusion == "success"
    assert result[0].completed_at == datetime(2026, 5, 28, 10, 0, 0, tzinfo=timezone.utc)
    assert result[1].name == "deploy"
    assert result[1].conclusion == "failure"
    assert result[1].completed_at is None


def test_fetch_check_runs_empty_response_returns_empty():
    side_effect = _make_run_mock(_CompletedFake(0, stdout=""))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect):
        client = ProductionGHClient()
        assert client.fetch_check_runs("a/b", "sha1") == []


def test_fetch_check_runs_handles_missing_fields():
    """Defensive: a row with no name / no status fields gets empty strings."""
    row = json.dumps({"conclusion": "success"})
    side_effect = _make_run_mock(_CompletedFake(0, stdout=row + "\n"))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect):
        client = ProductionGHClient()
        result = client.fetch_check_runs("a/b", "sha1")
    assert len(result) == 1
    assert result[0].name == ""
    assert result[0].status == ""
    assert result[0].details_url == ""


# ─── stateless guarantee ───────────────────────────────────────────────────


def test_client_is_stateless_between_calls():
    """Two independent calls reissue the subprocess; nothing is cached."""
    side_effect = _make_run_mock(
        _CompletedFake(0, stdout='{"login":"a"}'),
        _CompletedFake(0, stdout='{"login":"b"}'),
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        a = client.authenticated_user()
        b = client.authenticated_user()
    assert mock_run.call_count == 2
    assert a == {"login": "a"}
    assert b == {"login": "b"}


# ─── prefetch_default_branches + cache ─────────────────────────────────────


def test_prefetch_default_branches_builds_aliased_query():
    """Each slug becomes a `rN: repository(owner: ..., name: ...) { ... }`
    aliased node in a single GraphQL query."""
    payload = {
        "data": {
            "r0": {"defaultBranchRef": {"name": "main"}},
            "r1": {"defaultBranchRef": {"name": "develop"}},
        }
    }
    side_effect = _make_run_mock(_CompletedFake(0, stdout=json.dumps(payload)))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        client.prefetch_default_branches(["dhh1128/alpha", "provenant-dev/beta"])
    # One subprocess call regardless of slug count.
    assert mock_run.call_count == 1
    args, _ = mock_run.call_args
    argv = args[0]
    assert argv[0:3] == ["gh", "api", "graphql"]
    f_index = argv.index("-f")
    query = argv[f_index + 1]
    assert "query=query {" in query
    assert 'r0: repository(owner: "dhh1128", name: "alpha")' in query
    assert 'r1: repository(owner: "provenant-dev", name: "beta")' in query
    assert "defaultBranchRef { name }" in query


def test_prefetch_populates_cache_so_default_branch_hits_memory():
    """After prefetch, default_branch(slug) returns the cached value
    without issuing another subprocess call."""
    payload = {
        "data": {"r0": {"defaultBranchRef": {"name": "main"}}}
    }
    side_effect = _make_run_mock(_CompletedFake(0, stdout=json.dumps(payload)))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        client.prefetch_default_branches(["dhh1128/alpha"])
        result = client.default_branch("dhh1128/alpha")
    assert result == "main"
    # Exactly one subprocess call (the prefetch), default_branch hit cache.
    assert mock_run.call_count == 1


def test_seed_default_branches_populates_in_process_cache():
    """seed_default_branches lets default_branch hit memory with no
    network call (the warm-cache path from default_branch_cache)."""
    with patch("gitbulk.gh.subprocess.run") as mock_run:
        client = ProductionGHClient()
        client.seed_default_branches({"a/b": "main", "c/d": "develop"})
        assert client.default_branch("a/b") == "main"
        assert client.default_branch("c/d") == "develop"
    # No subprocess calls — both were seeded.
    assert mock_run.call_count == 0


def test_cached_default_branches_returns_copy():
    with patch("gitbulk.gh.subprocess.run"):
        client = ProductionGHClient()
        client.seed_default_branches({"a/b": "main"})
        snap = client.cached_default_branches()
    assert snap == {"a/b": "main"}
    # Mutating the snapshot doesn't affect the client's cache.
    snap["a/b"] = "MUTATED"
    assert client._default_branch_cache["a/b"] == "main"


def test_prefetch_reports_progress_per_chunk():
    """on_progress is called after each chunk with (done, total). With
    3 slugs and chunk size 100 there's one chunk → one callback at
    (3, 3). The contract is cumulative-completed, so the final call
    always equals (total, total)."""
    payload = {
        "data": {
            "r0": {"defaultBranchRef": {"name": "main"}},
            "r1": {"defaultBranchRef": {"name": "main"}},
            "r2": {"defaultBranchRef": {"name": "main"}},
        }
    }
    side_effect = _make_run_mock(_CompletedFake(0, stdout=json.dumps(payload)))
    calls: list[tuple[int, int]] = []
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect):
        client = ProductionGHClient()
        client.prefetch_default_branches(
            ["a/one", "a/two", "a/three"],
            on_progress=lambda done, total: calls.append((done, total)),
        )
    assert calls == [(3, 3)]


def test_default_branch_cache_miss_falls_back_to_rest():
    """A slug NOT in the cache falls through to the per-slug REST call."""
    payload = {"data": {"r0": {"defaultBranchRef": {"name": "main"}}}}
    rest_response = "develop\n"
    side_effect = _make_run_mock(
        _CompletedFake(0, stdout=json.dumps(payload)),  # prefetch
        _CompletedFake(0, stdout=rest_response),  # default_branch REST
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        client.prefetch_default_branches(["dhh1128/alpha"])
        # alpha is cached, beta is not
        cached = client.default_branch("dhh1128/alpha")
        uncached = client.default_branch("dhh1128/beta")
    assert cached == "main"
    assert uncached == "develop"
    assert mock_run.call_count == 2  # prefetch + REST for beta


def test_prefetch_empty_slugs_is_noop():
    """Empty input → no network call, no exception."""
    with patch("gitbulk.gh.subprocess.run") as mock_run:
        client = ProductionGHClient()
        client.prefetch_default_branches([])
    assert mock_run.call_count == 0


def test_prefetch_skips_malformed_slugs_defensively():
    """A slug without '/' is defensively skipped (should never happen
    post-load_repos, but the guard is cheap)."""
    payload = {"data": {"r1": {"defaultBranchRef": {"name": "main"}}}}
    side_effect = _make_run_mock(_CompletedFake(0, stdout=json.dumps(payload)))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        client.prefetch_default_branches(["malformed-slug", "good/repo"])
    # One graphql call; the malformed entry was skipped before query build.
    assert mock_run.call_count == 1
    args, _ = mock_run.call_args
    query = args[0][args[0].index("-f") + 1]
    assert "malformed-slug" not in query
    # Aliases are numbered within the post-filter chunk, so "good/repo"
    # is r0 (not r1 — the malformed one was filtered before chunking).
    assert 'r0: repository(owner: "good", name: "repo")' in query


def test_prefetch_all_malformed_skips_network():
    """If every input slug is malformed (no '/'), no query is built and
    no network call is made."""
    with patch("gitbulk.gh.subprocess.run") as mock_run:
        client = ProductionGHClient()
        client.prefetch_default_branches(["malformed", "also-malformed"])
    assert mock_run.call_count == 0


def test_prefetch_gracefully_degrades_on_gh_error():
    """If the GraphQL call fails (gh exits non-zero), cache stays empty
    and subsequent default_branch calls fall back to REST."""
    side_effect = _make_run_mock(
        _CompletedFake(1, stderr="API rate limit exceeded"),  # prefetch fails
        _CompletedFake(0, stdout="main\n"),  # REST fallback
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        # No exception raised by prefetch even though gh returned non-zero.
        client.prefetch_default_branches(["dhh1128/alpha"])
        result = client.default_branch("dhh1128/alpha")
    assert result == "main"
    # 1 attempt failed (non-retryable) + 1 REST = 2
    assert mock_run.call_count >= 2


def test_prefetch_gracefully_degrades_on_bad_json():
    """If gh returns non-JSON stdout, cache stays empty."""
    side_effect = _make_run_mock(
        _CompletedFake(0, stdout="not valid json"),
        _CompletedFake(0, stdout="main\n"),
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect):
        client = ProductionGHClient()
        client.prefetch_default_branches(["dhh1128/alpha"])
        result = client.default_branch("dhh1128/alpha")
    assert result == "main"


def test_prefetch_tolerates_null_default_branch_ref():
    """A repo with no commits has defaultBranchRef=null; just skip it
    (the per-slug REST fallback returns 'null' string for that case,
    matching pre-prefetch behavior)."""
    payload = {
        "data": {
            "r0": {"defaultBranchRef": None},
            "r1": {"defaultBranchRef": {"name": "main"}},
        }
    }
    side_effect = _make_run_mock(_CompletedFake(0, stdout=json.dumps(payload)))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect):
        client = ProductionGHClient()
        client.prefetch_default_branches(["a/empty", "a/normal"])
    # Only the populated one made it into the cache.
    assert client._default_branch_cache == {"a/normal": "main"}


def test_prefetch_tolerates_missing_alias_in_response():
    """If GitHub returns fewer aliases than we requested (unusual),
    skip the absent ones — they'll fall back to REST on demand."""
    payload = {"data": {"r0": {"defaultBranchRef": {"name": "main"}}}}
    side_effect = _make_run_mock(_CompletedFake(0, stdout=json.dumps(payload)))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect):
        client = ProductionGHClient()
        client.prefetch_default_branches(["a/has-it", "a/missing"])
    assert client._default_branch_cache == {"a/has-it": "main"}


def test_prefetch_tolerates_null_data_field():
    """Some GraphQL error shapes return data=null with an errors array.
    Cache stays empty; no exception."""
    payload = {"data": None, "errors": [{"message": "something"}]}
    side_effect = _make_run_mock(_CompletedFake(0, stdout=json.dumps(payload)))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect):
        client = ProductionGHClient()
        client.prefetch_default_branches(["a/b"])
    assert client._default_branch_cache == {}


def test_prefetch_tolerates_non_dict_node():
    """Defense in depth: a non-dict shape under an alias is skipped."""
    payload = {"data": {"r0": "unexpected-string-shape"}}
    side_effect = _make_run_mock(_CompletedFake(0, stdout=json.dumps(payload)))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect):
        client = ProductionGHClient()
        client.prefetch_default_branches(["a/b"])
    assert client._default_branch_cache == {}


def test_prefetch_accepts_partial_success_on_nonzero_exit():
    """Real-world case (discovered 2026-05-29 with a 205-repo fleet):
    one bad repo (deleted, renamed) makes `gh api graphql` return rc=1
    AND populated `data` for the other aliases. We must accept the
    partial data, not discard the whole chunk."""
    payload = {
        "data": {
            "r0": {"defaultBranchRef": {"name": "main"}},
            "r1": None,  # repository couldn't be resolved
            "r2": {"defaultBranchRef": {"name": "develop"}},
        },
        "errors": [{"message": "Could not resolve to a Repository ..."}],
    }
    side_effect = _make_run_mock(
        _CompletedFake(
            1,
            stdout=json.dumps(payload),
            stderr="Could not resolve to a Repository with the name 'x/y'.",
        )
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        client.prefetch_default_branches(["a/good1", "x/missing", "a/good2"])
    # Single subprocess call (no retry — we got usable stdout).
    assert mock_run.call_count == 1
    # The two resolvable repos made it into the cache; the missing one
    # didn't, and will fall back to per-slug REST on demand.
    assert client._default_branch_cache == {
        "a/good1": "main",
        "a/good2": "develop",
    }


def test_prefetch_retries_on_transient_stderr_when_no_stdout(monkeypatch):
    """If gh returns rc=1 with no stdout AND retryable stderr (HTTP 502),
    the chunk retries up to _max_retries before giving up."""
    side_effect = _make_run_mock(
        _CompletedFake(1, stdout="", stderr="gh: HTTP 502"),
        _CompletedFake(1, stdout="", stderr="gh: HTTP 502"),
        _CompletedFake(0, stdout=json.dumps({
            "data": {"r0": {"defaultBranchRef": {"name": "main"}}}
        })),
    )
    monkeypatch.setattr("gitbulk.gh.time.sleep", lambda _: None)
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        client.prefetch_default_branches(["a/b"])
    # 2 failed attempts + 1 success
    assert mock_run.call_count == 3
    assert client._default_branch_cache == {"a/b": "main"}


def test_prefetch_gives_up_on_non_retryable_stderr_with_no_stdout():
    """rc=1, empty stdout, non-retryable stderr (e.g. auth error) →
    abort the chunk silently. Per-slug REST fallback handles it."""
    side_effect = _make_run_mock(
        _CompletedFake(1, stdout="", stderr="HTTP 401: Bad credentials"),
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        client.prefetch_default_branches(["a/b"])
    # One attempt, no retry (non-retryable), cache stays empty.
    assert mock_run.call_count == 1
    assert client._default_branch_cache == {}


def test_prefetch_handles_timeout_with_retry(monkeypatch):
    """If subprocess.run raises TimeoutExpired on attempt 1, we retry."""
    payload = {"data": {"r0": {"defaultBranchRef": {"name": "main"}}}}
    side_effect = _make_run_mock(
        subprocess.TimeoutExpired(cmd=["gh"], timeout=30.0),
        _CompletedFake(0, stdout=json.dumps(payload)),
    )
    monkeypatch.setattr("gitbulk.gh.time.sleep", lambda _: None)
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        client.prefetch_default_branches(["a/b"])
    assert mock_run.call_count == 2
    assert client._default_branch_cache == {"a/b": "main"}


def test_prefetch_exhausts_retries_on_persistent_timeout(monkeypatch):
    """All attempts time out → stdout stays empty → give up silently."""
    side_effect = _make_run_mock(
        subprocess.TimeoutExpired(cmd=["gh"], timeout=30.0),
        subprocess.TimeoutExpired(cmd=["gh"], timeout=30.0),
        subprocess.TimeoutExpired(cmd=["gh"], timeout=30.0),
    )
    monkeypatch.setattr("gitbulk.gh.time.sleep", lambda _: None)
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        client.prefetch_default_branches(["a/b"])
    assert mock_run.call_count == 3
    assert client._default_branch_cache == {}


def test_prefetch_tolerates_non_string_branch_name():
    """Defensive: defaultBranchRef.name being null or empty is rejected."""
    payload = {
        "data": {
            "r0": {"defaultBranchRef": {"name": None}},
            "r1": {"defaultBranchRef": {"name": ""}},
        }
    }
    side_effect = _make_run_mock(_CompletedFake(0, stdout=json.dumps(payload)))
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect):
        client = ProductionGHClient()
        client.prefetch_default_branches(["a/x", "a/y"])
    assert client._default_branch_cache == {}
