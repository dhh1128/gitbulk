"""Structured PR data model used across invariants, ``report``, and ``gh.py``.

See this.i node ``prdtm4kn`` for the field-set rationale, including the
deliberate use of ``str | None`` (not hard enums) for ``mergeable_state``,
``review_decision``, and ``checks_status``: GitHub adds new values
periodically, and a hard enum would force a code change on every new value.
The documented "known" sets below are used for validation and forward-
compat assertions, but not for type narrowing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

PRState = Literal["OPEN", "CLOSED", "MERGED"]

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
