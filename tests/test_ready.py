"""Tests for ``gitbulk.ready.compute_ready_since``.

See this.i node ``zk3r4nqp`` and the module docstring for the
simplification rationale (Phase 5 MVP uses last_pushed_at as the
conservative anchor; precise timeline-aware computation is future work).
"""

from __future__ import annotations

from datetime import datetime, timezone

from gitbulk.pr_info import PRInfo
from gitbulk.ready import compute_ready_since


_LAST_PUSH = datetime(2026, 5, 25, 14, 0, 0, tzinfo=timezone.utc)


def _pr(
    *,
    mergeable_state: str | None = "CLEAN",
    checks_status: str | None = "SUCCESS",
    review_decision: str | None = "APPROVED",
    last_pushed_at: datetime | None = _LAST_PUSH,
) -> PRInfo:
    return PRInfo(
        slug="dhh1128/gitbulk",
        number=1,
        title="t",
        url="https://github.com/dhh1128/gitbulk/pull/1",
        author="dhh1128",
        base_ref="main",
        head_ref="f",
        head_sha="a" * 40,
        state="OPEN",
        is_draft=False,
        mergeable_state=mergeable_state,
        created_at=datetime(2026, 5, 20, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 25, tzinfo=timezone.utc),
        last_pushed_at=last_pushed_at,
        labels=(),
        review_decision=review_decision,
        checks_status=checks_status,
    )


def test_ready_when_all_fields_clean_returns_last_pushed_at():
    pr = _pr()
    assert compute_ready_since(pr) == _LAST_PUSH


def test_not_ready_when_mergeable_state_not_clean_returns_none():
    pr = _pr(mergeable_state="DIRTY")
    assert compute_ready_since(pr) is None


def test_not_ready_when_mergeable_state_none_returns_none():
    pr = _pr(mergeable_state=None)
    assert compute_ready_since(pr) is None


def test_not_ready_when_checks_not_success_returns_none():
    pr = _pr(checks_status="FAILURE")
    assert compute_ready_since(pr) is None


def test_not_ready_when_checks_pending_returns_none():
    pr = _pr(checks_status="PENDING")
    assert compute_ready_since(pr) is None


def test_not_ready_when_checks_none_returns_none():
    pr = _pr(checks_status=None)
    assert compute_ready_since(pr) is None


def test_not_ready_when_review_not_approved_strict_returns_none():
    pr = _pr(review_decision="REVIEW_REQUIRED")
    assert compute_ready_since(pr) is None


def test_not_ready_when_review_none_strict_returns_none():
    pr = _pr(review_decision=None)
    assert compute_ready_since(pr) is None


def test_ci_only_policy_ignores_review_decision_when_none():
    """With require_approval=False, a missing review_decision is OK."""
    pr = _pr(review_decision=None)
    assert compute_ready_since(pr, require_approval=False) == _LAST_PUSH


def test_ci_only_policy_ignores_review_decision_when_changes_requested():
    pr = _pr(review_decision="CHANGES_REQUESTED")
    assert compute_ready_since(pr, require_approval=False) == _LAST_PUSH


def test_ci_only_still_requires_clean_mergeable_state():
    pr = _pr(mergeable_state="DIRTY", review_decision=None)
    assert compute_ready_since(pr, require_approval=False) is None


def test_ci_only_still_requires_success_checks():
    pr = _pr(checks_status="FAILURE", review_decision=None)
    assert compute_ready_since(pr, require_approval=False) is None


def test_ready_but_no_last_pushed_at_returns_none():
    """If the PR appears ready by structured fields but we have no
    last_pushed_at anchor, we can't synthesize one — return None rather
    than guess."""
    pr = _pr(last_pushed_at=None)
    assert compute_ready_since(pr) is None


def test_returned_datetime_is_timezone_aware():
    pr = _pr()
    result = compute_ready_since(pr)
    assert result is not None
    assert result.tzinfo is not None
