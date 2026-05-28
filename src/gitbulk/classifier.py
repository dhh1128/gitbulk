"""Humans-vs-bots classifier (this.i node ``hbcls4pq``).

A pure function over (login, Policy, optional org-members frozenset)
that returns a :class:`Classification`. No I/O; tests pass canned
inputs. The resolution order mirrors node ``pj5kn2zw``: unknown
logins default to BOT so that gitbulk fails toward "skip" rather
than toward "silently approve."

Production code guarantees the org-members cache is loaded before
any classifier call via the ``org.members.fresh`` preflight
invariant; the ``org_members=None`` overload exists for tooling that
must call the classifier without a cache (e.g., during a cache
refresh itself, before the new members list is committed).
"""

from __future__ import annotations

from enum import Enum

from gitbulk.config.policy import Policy


class Classification(str, Enum):
    """Result of :func:`classify_login`.

    ``UNKNOWN`` is reserved for tooling that needs to distinguish
    "we asked but couldn't decide" from "we decided BOT." Production
    code paths never see ``UNKNOWN`` because the ``org.members.fresh``
    preflight runs first; see node ``hbcls4pq``.
    """

    HUMAN = "human"
    BOT = "bot"
    UNKNOWN = "unknown"


def classify_login(
    login: str,
    policy: Policy,
    org_members: frozenset[str] | None = None,
) -> Classification:
    """Classify a GitHub ``login`` as HUMAN or BOT.

    Resolution order (node ``hbcls4pq``, mirrors ``pj5kn2zw``):

      1. ``login in policy.humans.always_human``                 → HUMAN
      2. ``login in policy.bots``                                → BOT
      3. ``login in org_members`` AND
         ``login not in policy.humans.exceptions``               → HUMAN
      4. otherwise                                               → BOT

    When ``org_members is None`` the cache-aware step 3 is skipped and
    the function falls through to step 4 (default non-human per
    ``pj5kn2zw``).
    """
    if login in policy.humans.always_human:
        return Classification.HUMAN
    if login in policy.bots:
        return Classification.BOT
    if org_members is not None and login in org_members:
        if login not in policy.humans.exceptions:
            return Classification.HUMAN
    return Classification.BOT
