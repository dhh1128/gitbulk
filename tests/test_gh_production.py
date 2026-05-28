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


def test_my_open_prs_with_empty_slug_list_returns_empty_dict():
    """Empty iterable → still issues a query with no repo: terms, and
    the resulting dict has no slug keys (because none were requested) and
    no PR rows would land outside that set... but per FakeGHClient
    semantics, when slugs is given as []  the result dict is empty."""
    side_effect = _make_run_mock(
        _CompletedFake(0, stdout=json.dumps({"data": {"search": {"nodes": []}}}))
    )
    with patch("gitbulk.gh.subprocess.run", side_effect=side_effect) as mock_run:
        client = ProductionGHClient()
        result = client.my_open_prs(slugs=[])

    assert result == {}
    args, _ = mock_run.call_args
    argv = args[0]
    q_value = argv[argv.index("-F") + 1]
    assert q_value == "q=author:@me is:open is:pr"


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
