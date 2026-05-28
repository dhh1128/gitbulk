"""Structured PR data model used across invariants, ``report``, and ``gh.py``.

See this.i node ``prdtm4kn`` for the field-set rationale, including the
deliberate use of ``str | None`` (not hard enums) for ``mergeable_state``,
``review_decision``, and ``checks_status``: GitHub adds new values
periodically, and a hard enum would force a code change on every new value.
The documented "known" sets below are used for validation and forward-
compat assertions, but not for type narrowing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

PRState = Literal["OPEN", "CLOSED", "MERGED"]

#: Kinds of PR-timeline events that affect the continuously-ready window.
#: ``draft`` (ConvertToDraftEvent) and ``changes_requested`` (a review
#: with state CHANGES_REQUESTED) BREAK ready. ``ready`` (ReadyForReviewEvent)
#: and ``approved`` (a review with state APPROVED) RESTORE it.
#: See zk3r4nqp / bg4pqn7m and ``gitbulk.ready.compute_ready_since``.
TimelineEventKind = Literal["draft", "ready", "changes_requested", "approved"]

#: Values gh GraphQL currently emits for ``mergeStateStatus``. New values
#: are tolerated at the type level (the field is ``str``) but trigger a
#: log entry from the gh client so we can catch new states early.
KNOWN_MERGEABLE_STATES: frozenset[str] = frozenset(
    {
        "CLEAN",
        "DIRTY",
        "BLOCKED",
        "BEHIND",
        "HAS_HOOKS",
        "UNKNOWN",
        "UNSTABLE",
    }
)

#: Values gh currently emits for ``reviewDecision`` (PR-level).
KNOWN_REVIEW_DECISIONS: frozenset[str] = frozenset(
    {
        "APPROVED",
        "CHANGES_REQUESTED",
        "REVIEW_REQUIRED",
    }
)

#: Values gh currently emits as the combined checks rollup. PENDING is the
#: shape we use; gh's actual statuses also include EXPECTED/IN_PROGRESS
#: which we treat as PENDING semantically.
KNOWN_CHECKS_STATUSES: frozenset[str] = frozenset(
    {
        "SUCCESS",
        "FAILURE",
        "PENDING",
        "ERROR",
        "CANCELLED",
        "SKIPPED",
        "TIMED_OUT",
        "ACTION_REQUIRED",
        "NEUTRAL",
        "STALE",
    }
)


@dataclass(frozen=True)
class TimelineEvent:
    """One PR-timeline event relevant to the continuously-ready window.

    Captured from a subset of GraphQL ``timelineItems`` types — currently
    ``ReadyForReviewEvent``, ``ConvertToDraftEvent``, and
    ``PullRequestReview`` (where ``state in {APPROVED, CHANGES_REQUESTED}``).
    Other timeline kinds (label changes, comments, force-pushes captured
    via ``last_pushed_at``) are not stored.
    """

    kind: TimelineEventKind
    at: datetime


@dataclass(frozen=True)
class PRInfo:
    """Read-only structured snapshot of one open PR.

    Returned by the gh client and used as a parameter type in per-PR
    invariant chains. Frozen so it can be passed through ``InvariantContext``
    without defensive copying.
    """

    slug: str
    number: int
    title: str
    url: str
    author: str
    base_ref: str
    head_ref: str
    head_sha: str
    state: PRState
    is_draft: bool
    mergeable_state: str | None
    created_at: datetime
    updated_at: datetime
    last_pushed_at: datetime | None
    labels: tuple[str, ...]
    review_decision: str | None
    checks_status: str | None
    #: Count of currently-unresolved review threads on the PR. Bots are
    #: counted alongside humans per the merge-gate decision (see gaps.md
    #: and the merge-only ``pr.no_unresolved_threads`` invariant).
    #: Defaults to 0 so legacy test fixtures and the FakeGHClient stay
    #: ergonomic; production code fills this from
    #: ``reviewThreads.totalCount`` minus the resolved count.
    unresolved_thread_count: int = 0
    #: Subset of PR ``timelineItems`` ordered chronologically (oldest
    #: first). Only kinds enumerated in :data:`TimelineEventKind` are
    #: stored. Empty tuple when no relevant events occurred (or when
    #: timeline data was not fetched, e.g. by FakeGHClient defaults).
    timeline_events: tuple[TimelineEvent, ...] = field(default_factory=tuple)
    #: True if the timeline window we fetched did not reach back to
    #: ``last_pushed_at`` (i.e., GraphQL truncated). ``compute_ready_since``
    #: treats this conservatively: if there is no anchor inside the
    #: returned window, fall through to ``last_pushed_at`` as the only
    #: reliable lower bound.
    timeline_capped: bool = False
