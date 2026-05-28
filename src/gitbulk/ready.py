"""Compute ``ready_since`` for a PR: when did it become continuously merge-ready?

A PR is "ready" at moment T iff at T:
  - mergeable_state == "CLEAN"
  - checks_status == "SUCCESS"
  - review_decision == "APPROVED" (unless merge_policy=ci-only)
  - All review threads are resolved (``unresolved_thread_count == 0``)

``ready_since`` is the earliest T such that the PR has been continuously
ready from T to now. See this.i node ``zk3r4nqp`` (Ready To Merge Stricter
Than GitHub) and ``bg4pqn7m`` (Three Business Days From Continuously Ready).

Algorithm (Phase 5+ timeline-aware):

1. Verify the PR is *currently* ready by checking the four structured
   gates above. If any fails, return None.
2. Use ``last_pushed_at`` as the anchor's lower bound: force-pushes reset
   the clock and are not represented in ``timeline_events``.
3. Walk ``timeline_events`` chronologically. Each event of kind ``draft``
   or ``changes_requested`` opens a breaker; ``ready`` closes a ``draft``
   breaker; ``approved`` closes a ``changes_requested`` breaker. Only
   ``ready`` events advance the anchor (and only when they clear the
   last open breaker). ``approved`` events DO clear the
   ``changes_requested`` breaker but DO NOT advance the anchor — per
   zk3r4nqp, an approval is the merge signal we're waiting for, not a
   clock-restart event. The age-threshold short-circuit on
   ``review_decision == "APPROVED"`` in ``pr.age_threshold`` provides
   the "merge immediately on approval" outcome.
4. If any breaker remains open at the end of the walk, return None —
   the timeline says ready was broken and we never saw it restored
   (even though the structured fields look ready now). Conservative
   safety wins over generosity here, per "a bug in gitbulk can damage
   real work in real repos."
5. When ``require_approval=False`` (ci-only policy), review-derived
   events (``changes_requested`` / ``approved``) are not gates; only
   ``draft`` / ``ready`` apply.

Reset events captured per the user's decision in the merge-gate design
session: ConvertToDraft, CHANGES_REQUESTED review, thread reopen, and
check transition-to-non-success. Threads are gated structurally via
``unresolved_thread_count`` rather than via timeline events (the GraphQL
``timelineItems`` schema doesn't expose thread resolve/reopen as a clean
kind). Check transitions back to SUCCESS are not surfaced cleanly by
GitHub's timeline either, so we rely on ``checks_status`` being SUCCESS
*right now* as the structural gate, and on a force-push (captured via
``last_pushed_at``) as the most common reason a previously-failing check
went green.
"""

from __future__ import annotations

from datetime import datetime

from gitbulk.pr_info import PRInfo


def compute_ready_since(
    pr: PRInfo,
    *,
    require_approval: bool = True,
) -> datetime | None:
    """Return the timeline-aware ``ready_since`` timestamp, or None.

    Args:
      pr: the PR to evaluate.
      require_approval: when True (the default, matching merge_policy
        ``strict``), the PR's review_decision must be "APPROVED" and
        review-derived timeline events (``approved`` / ``changes_requested``)
        are honored. When False (merge_policy=``ci-only``), the review
        decision is ignored and review-derived timeline events are
        skipped.

    Returns:
      A timezone-aware UTC datetime if the PR is continuously ready,
      else None.
    """
    # ─── structural gates ─────────────────────────────────────────────
    if pr.mergeable_state != "CLEAN":
        return None
    if pr.checks_status != "SUCCESS":
        return None
    if require_approval and pr.review_decision != "APPROVED":
        return None
    if pr.unresolved_thread_count > 0:
        return None
    if pr.last_pushed_at is None:
        # No anchor to base the clock on; can't claim continuously-ready.
        return None

    # ─── timeline walk ────────────────────────────────────────────────
    anchor = pr.last_pushed_at

    # Open breakers (kind -> earliest open timestamp) tracked as a small
    # set since there are only two kinds. Each restorer event closes its
    # matching kind. The anchor advances to the restorer's timestamp at
    # the moment NO breakers remain open.
    open_breakers: set[str] = set()
    sorted_events = sorted(pr.timeline_events, key=lambda e: e.at)
    for event in sorted_events:
        if event.at <= pr.last_pushed_at:
            # Pre-push events were invalidated by the force-push reset.
            continue
        if not require_approval and event.kind in ("approved", "changes_requested"):
            # ci-only: review events are not gates.
            continue
        # TimelineEventKind is a closed Literal of these four values;
        # the mapping below covers every legal case so no defensive else
        # branch is needed.
        if event.kind == "draft":
            open_breakers.add("draft")
        elif event.kind == "changes_requested":
            open_breakers.add("changes_requested")
        elif event.kind == "ready":
            open_breakers.discard("draft")
            if not open_breakers:
                # ready_for_review IS a clock-restart event: the PR was
                # paused (draft) and now it's actively up for review.
                anchor = event.at
        else:  # "approved"
            # Approval clears the changes_requested breaker so the
            # current-readiness gate passes, but it does NOT advance the
            # anchor — approvals are the merge signal we're waiting for
            # under zk3r4nqp, not a fresh start. See pr.age_threshold for
            # the explicit "merge immediately on approval" short-circuit.
            open_breakers.discard("changes_requested")

    if open_breakers:
        # Structural gates say currently ready, but timeline says a
        # breaker is still open. Be conservative.
        return None

    return anchor


__all__ = ["compute_ready_since"]
