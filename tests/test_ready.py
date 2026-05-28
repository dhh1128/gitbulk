"""Tests for ``gitbulk.ready.compute_ready_since``.

See this.i node ``zk3r4nqp`` and the module docstring for the
simplification rationale (Phase 5 MVP uses last_pushed_at as the
conservative anchor; precise timeline-aware computation is future work).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from gitbulk.pr_info import PRInfo, TimelineEvent
from gitbulk.ready import compute_ready_since


_LAST_PUSH = datetime(2026, 5, 25, 14, 0, 0, tzinfo=timezone.utc)


def _pr(
    *,
    mergeable_state: str | None = "CLEAN",
    checks_status: str | None = "SUCCESS",
    review_decision: str | None = "APPROVED",
    last_pushed_at: datetime | None = _LAST_PUSH,
    unresolved_thread_count: int = 0,
    timeline_events: tuple[TimelineEvent, ...] = (),
    timeline_capped: bool = False,
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
        unresolved_thread_count=unresolved_thread_count,
        timeline_events=timeline_events,
        timeline_capped=timeline_capped,
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


# ─── unresolved-thread gate ────────────────────────────────────────────────


def test_not_ready_when_unresolved_threads_present():
    """A currently-ready-looking PR with unresolved threads is NOT ready.

    This is the structural counterpart of the ``pr.no_unresolved_threads``
    invariant: the readiness clock cannot advance while threads are open.
    """
    pr = _pr(unresolved_thread_count=1)
    assert compute_ready_since(pr) is None


def test_not_ready_when_many_unresolved_threads():
    pr = _pr(unresolved_thread_count=5)
    assert compute_ready_since(pr) is None


def test_ci_only_still_requires_zero_unresolved_threads():
    """Even ci-only policy requires threads resolved.

    Bot-driven threads are a documented signal in this codebase (per the
    gaps.md merge-gate decision: bots count). ci-only relaxes review,
    not unresolved-thread gating.
    """
    pr = _pr(review_decision=None, unresolved_thread_count=1)
    assert compute_ready_since(pr, require_approval=False) is None


# ─── timeline-aware ready_since ────────────────────────────────────────────


def test_no_timeline_events_anchor_is_last_pushed_at():
    """Empty timeline + currently-ready ⇒ ready_since == last_pushed_at."""
    pr = _pr(timeline_events=())
    assert compute_ready_since(pr) == _LAST_PUSH


def test_timeline_events_only_before_last_push_are_ignored():
    """Events older than last_pushed_at don't affect the anchor.

    Force-pushes reset the clock to last_pushed_at, so prior breakers are
    already invalidated.
    """
    pre_push = _LAST_PUSH - timedelta(days=2)
    pr = _pr(
        timeline_events=(
            TimelineEvent(kind="changes_requested", at=pre_push),
            TimelineEvent(kind="approved", at=pre_push + timedelta(hours=1)),
        )
    )
    assert compute_ready_since(pr) == _LAST_PUSH


def test_convert_to_draft_then_ready_for_review_anchors_at_ready():
    """draft → ready: anchor at the ready_for_review timestamp."""
    drafted = _LAST_PUSH + timedelta(hours=2)
    readied = _LAST_PUSH + timedelta(hours=5)
    pr = _pr(
        timeline_events=(
            TimelineEvent(kind="draft", at=drafted),
            TimelineEvent(kind="ready", at=readied),
        )
    )
    assert compute_ready_since(pr) == readied


def test_changes_requested_then_approved_anchor_stays_at_last_push():
    """CHANGES_REQUESTED → APPROVED: anchor does NOT move per zk3r4nqp.

    Approvals are the signal we're waiting for, not a clock-restart event.
    The anchor stays at last_pushed_at; the "merge immediately on approval"
    outcome comes from pr.age_threshold's short-circuit on
    review_decision == APPROVED, not from this anchor.
    """
    changes = _LAST_PUSH + timedelta(hours=3)
    approved = _LAST_PUSH + timedelta(hours=7)
    pr = _pr(
        timeline_events=(
            TimelineEvent(kind="changes_requested", at=changes),
            TimelineEvent(kind="approved", at=approved),
        )
    )
    assert compute_ready_since(pr) == _LAST_PUSH


def test_lone_approval_does_not_move_anchor():
    """push → approved → now (no breakers in between): anchor stays.

    A bare approval with no prior breaker is the canonical case of the
    "approvals don't restart the clock" rule. Before the fix, this
    advanced the anchor to the approval timestamp, silently restarting
    the wait. The current behavior keeps the anchor at last_pushed_at.
    """
    approved = _LAST_PUSH + timedelta(hours=2)
    pr = _pr(
        timeline_events=(
            TimelineEvent(kind="approved", at=approved),
        )
    )
    assert compute_ready_since(pr) == _LAST_PUSH


def test_two_approvals_in_a_row_still_anchor_at_last_push():
    """Multiple approvals (e.g., from different reviewers) don't compound."""
    pr = _pr(
        timeline_events=(
            TimelineEvent(kind="approved", at=_LAST_PUSH + timedelta(hours=1)),
            TimelineEvent(kind="approved", at=_LAST_PUSH + timedelta(hours=4)),
        )
    )
    assert compute_ready_since(pr) == _LAST_PUSH


def test_cr_approved_draft_ready_anchors_at_ready_event():
    """CR cleared by approval (no anchor move), then draft cleared by
    ready (anchor advances). Final anchor is the ``ready`` timestamp.
    """
    t1 = _LAST_PUSH + timedelta(hours=1)
    t2 = _LAST_PUSH + timedelta(hours=2)
    t3 = _LAST_PUSH + timedelta(hours=3)
    t4 = _LAST_PUSH + timedelta(hours=4)
    pr = _pr(
        timeline_events=(
            TimelineEvent(kind="changes_requested", at=t1),
            TimelineEvent(kind="approved", at=t2),  # clears CR, no anchor move
            TimelineEvent(kind="draft", at=t3),
            TimelineEvent(kind="ready", at=t4),  # clears draft, anchor=t4
        )
    )
    assert compute_ready_since(pr) == t4


def test_uncleared_breaker_returns_none():
    """If the timeline shows a breaker with no subsequent restorer,
    we can't pin a ready_since — return None so the PR Skips.

    This is conservative-safe: the structured fields say the PR is
    currently ready, but the timeline says ready broke and we never
    saw it restored. Better to wait another cycle than authorize a
    merge on uncertain history.
    """
    broke = _LAST_PUSH + timedelta(hours=3)
    pr = _pr(
        timeline_events=(
            TimelineEvent(kind="changes_requested", at=broke),
        )
    )
    assert compute_ready_since(pr) is None


def test_uncleared_breaker_then_unrelated_restore_does_not_clear():
    """A ``ready`` event does not clear a CHANGES_REQUESTED breaker, and
    an ``approved`` event does not clear a ConvertToDraft breaker.

    Each breaker requires its matching kind of restorer. This is what
    "continuously ready" actually means: all conditions must be restored
    in sequence.
    """
    t1 = _LAST_PUSH + timedelta(hours=1)
    t2 = _LAST_PUSH + timedelta(hours=2)
    pr = _pr(
        timeline_events=(
            TimelineEvent(kind="changes_requested", at=t1),
            # ``ready`` is the wrong restorer for ``changes_requested``
            TimelineEvent(kind="ready", at=t2),
        )
    )
    assert compute_ready_since(pr) is None


def test_two_concurrent_breakers_need_both_restorers():
    """draft + changes_requested both open: both must be restored.

    Order: draft → CR → ready (clears draft, CR still open, no anchor
    advance) → approved (clears CR, no anchor advance per the rule that
    approvals don't restart the clock). Anchor stays at last_pushed_at
    even though the PR is currently ready.

    The "merge immediately on approval" outcome is delivered by
    pr.age_threshold's review_decision==APPROVED short-circuit, not by
    this anchor.
    """
    t1 = _LAST_PUSH + timedelta(hours=1)
    t2 = _LAST_PUSH + timedelta(hours=2)
    t3 = _LAST_PUSH + timedelta(hours=3)
    t4 = _LAST_PUSH + timedelta(hours=4)
    pr = _pr(
        timeline_events=(
            TimelineEvent(kind="draft", at=t1),
            TimelineEvent(kind="changes_requested", at=t2),
            TimelineEvent(kind="ready", at=t3),  # clears draft; CR still open
            TimelineEvent(kind="approved", at=t4),  # clears CR; no anchor move
        )
    )
    assert compute_ready_since(pr) == _LAST_PUSH


def test_cr_draft_approved_ready_anchors_at_ready():
    """CR → draft → approved → ready. The approved event clears CR but
    doesn't advance the anchor (approvals never advance). The ready
    event clears draft AND is the last breaker → anchor=ready timestamp.
    """
    t1 = _LAST_PUSH + timedelta(hours=1)
    t2 = _LAST_PUSH + timedelta(hours=2)
    t3 = _LAST_PUSH + timedelta(hours=3)
    t4 = _LAST_PUSH + timedelta(hours=4)
    pr = _pr(
        timeline_events=(
            TimelineEvent(kind="changes_requested", at=t1),
            TimelineEvent(kind="draft", at=t2),
            TimelineEvent(kind="approved", at=t3),  # clears CR, no anchor move
            TimelineEvent(kind="ready", at=t4),  # clears draft → anchor=t4
        )
    )
    assert compute_ready_since(pr) == t4


def test_two_concurrent_breakers_one_restorer_returns_none():
    t1 = _LAST_PUSH + timedelta(hours=1)
    t2 = _LAST_PUSH + timedelta(hours=2)
    t3 = _LAST_PUSH + timedelta(hours=3)
    pr = _pr(
        timeline_events=(
            TimelineEvent(kind="draft", at=t1),
            TimelineEvent(kind="changes_requested", at=t2),
            TimelineEvent(kind="ready", at=t3),
            # missing: approval
        )
    )
    assert compute_ready_since(pr) is None


def test_timeline_capped_with_no_events_falls_back_to_last_push():
    """Capped timeline + currently-ready + no events visible: trust
    last_pushed_at. The cap means GraphQL truncated; in the truncated
    region there *might* be a breaker, but we have no evidence — and
    in practice timelineItems(last:50) captures everything for typical
    PRs. The fallback matches the previous conservative behavior.
    """
    pr = _pr(timeline_events=(), timeline_capped=True)
    assert compute_ready_since(pr) == _LAST_PUSH


def test_ci_only_ignores_review_events_in_timeline():
    """When require_approval=False, CHANGES_REQUESTED/APPROVED in the
    timeline are not gates — only draft/ready remain breakers.
    """
    changes = _LAST_PUSH + timedelta(hours=3)
    pr = _pr(
        review_decision=None,
        timeline_events=(
            TimelineEvent(kind="changes_requested", at=changes),
        ),
    )
    # In ci-only mode the orphan changes_requested is irrelevant.
    assert compute_ready_since(pr, require_approval=False) == _LAST_PUSH


def test_ci_only_still_honors_draft_restore_cycle():
    drafted = _LAST_PUSH + timedelta(hours=2)
    readied = _LAST_PUSH + timedelta(hours=5)
    pr = _pr(
        review_decision=None,
        timeline_events=(
            TimelineEvent(kind="draft", at=drafted),
            TimelineEvent(kind="ready", at=readied),
        ),
    )
    assert compute_ready_since(pr, require_approval=False) == readied
