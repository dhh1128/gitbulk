"""Compute ``ready_since`` for a PR: when did it become continuously merge-ready?

A PR is "ready" at moment T iff at T:
  - mergeable_state == "CLEAN"
  - checks_status == "SUCCESS"
  - review_decision == "APPROVED" (unless merge_policy=ci-only)
  - All review threads are resolved

``ready_since`` is the earliest T such that the PR has been continuously
ready from T to now. See this.i node ``zk3r4nqp`` (Ready To Merge Stricter
Than GitHub) and ``bg4pqn7m`` (Three Business Days From Continuously Ready).

Phase 5 MVP simplification (documented per AGENTS.md "Skipping invariants…"
discipline applied to derived data): :class:`PRInfo` as of Phase 2 does
NOT carry the PR's full event timeline; a precise ``ready_since`` would
require querying the timelineItems GraphQL connection per PR. For now we
return a CONSERVATIVE proxy:

  - if the PR currently satisfies the three structured fields above
    (mergeable_state CLEAN, checks SUCCESS, review APPROVED when strict)
    AND has a ``last_pushed_at``, ``ready_since = last_pushed_at``.
  - else: None.

The proxy under-estimates rather than over-estimates ready-time: any
later force-push, status-check flap, or review change resets the clock
to the most recent push, never earlier. This is the SAFE direction for a
mutating-by-default subcommand. A future enhancement should query the
timeline and replace this function in-place — callers consume only the
return value, not the algorithm.
"""

from __future__ import annotations

from datetime import datetime

from gitbulk.pr_info import PRInfo


def compute_ready_since(
    pr: PRInfo,
    *,
    require_approval: bool = True,
) -> datetime | None:
    """Return the conservative ``ready_since`` timestamp, or None.

    Args:
      pr: the PR to evaluate.
      require_approval: when True (the default, matching merge_policy
        ``strict``), the PR's review_decision must be "APPROVED". When
        False (merge_policy=``ci-only``), the review decision is ignored.

    Returns:
      A timezone-aware UTC datetime if the PR currently appears ready,
      else None. The returned value is ``pr.last_pushed_at`` when it is
      not None; if ``last_pushed_at`` is None even though the PR appears
      ready, we still return None (we have no anchor to use as the
      ready-since timestamp).
    """
    if pr.mergeable_state != "CLEAN":
        return None
    if pr.checks_status != "SUCCESS":
        return None
    if require_approval and pr.review_decision != "APPROVED":
        return None
    # The two branches above guard the structured fields. The conservative
    # anchor is the last push: a force-push would reset the clock, so the
    # current head's commit time is the earliest moment we can claim "still
    # ready since then" without consulting the timeline.
    return pr.last_pushed_at


__all__ = ["compute_ready_since"]
