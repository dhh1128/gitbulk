"""Shared helpers for the command handler modules.

Several command modules (``merge``, ``close_stale``, ``dispatch``,
``rebase_pr``, ``report``, ``prune_branches``, ``prune_worktrees``) built
their run-manifest snapshots the same way: flatten a frozen policy
dataclass to a YAML-friendly dict, read ``repos.txt`` verbatim, and split a
named invariant chain into kind buckets. Those helpers were copied verbatim
across the modules with zero per-command variation, so they live here once
and are imported where needed (MNT-F2 / TST-F5).

Note on ``partition_chain``: this is the three-bucket form
(UNIVERSAL / PER_REPO / PER_PR) used by every command whose unit of work is
a PR. ``prune_branches`` and ``prune_worktrees`` deliberately keep a local
two-bucket variant (no PER_PR) because their unit of work is a branch /
worktree, not a PR — see the comments at those call sites.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from typing import Iterable

from gitbulk import paths
from gitbulk.config.policy import Policy, policy_for
from gitbulk.invariants import get
from gitbulk.invariants.base import Invariant, InvariantKind


#: Branch names ALWAYS treated as SACRED — never auto-pruned by either
#: ``prune-branches`` (remote branch deletion) or ``prune-worktrees`` (local
#: branch/worktree removal), independent of config. The user's rule: a name
#: that is a backstop against LOCAL deletion must be an equal backstop against
#: REMOTE deletion. These are unioned with the operator-configured
#: ``sacred_branches`` (defaults + per-repo override) by
#: :func:`sacred_branch_names`. Each command additionally protects the repo's
#: own default branch (and ``prune-branches`` also honours GitHub branch
#: protection), so this set is purely additive — it only ever keeps MORE
#: branches and can never cause a deletion.
#:
#: ``gh-pages`` and ``tick`` join the universally-sacred set (node prnorph7):
#: both are well-known ORPHAN-branch conventions — a GitHub Pages publish
#: branch and the ``tick`` defect-ledger branch — that are deliberately
#: long-lived and detached from the default branch, never stale feature work.
#: The structural orphan guard (:func:`gitbulk.worktree.branch_shares_history`)
#: already protects any unrelated-history branch, but these names are the
#: name-based backstop that also covers a NON-orphan ``gh-pages`` and the case
#: where the default branch can't be resolved to run the structural check.
SACRED_BRANCH_NAMES: frozenset[str] = frozenset(
    {"main", "master", "gh-pages", "tick"}
)


def apply_prune_min_age_override(
    policy: Policy, args: argparse.Namespace
) -> Policy:
    """Return ``policy`` with the prune grace period overridden by
    ``--min-age-days`` if it was passed, else ``policy`` unchanged (node
    prgrc3kp).

    The flag is a per-run knob meaning "instead of the default N days": it
    rewrites ``defaults.prune_min_age_days`` only. A repo that carries an
    explicit per-repo ``prune_min_age_days`` override still wins via
    :func:`policy_for`, because a deliberately-configured per-repo grace is a
    stronger statement of intent than an ad-hoc CLI flag and is usually a
    SAFETY setting (a longer cool-off) we must not silently shorten. Folding
    the value into ``defaults`` rather than threading it through every
    classifier means the whole call chain — and the run's config snapshot —
    sees the effective grace with no extra plumbing."""
    val = getattr(args, "min_age_days", None)
    if val is None:
        return policy
    return replace(
        policy, defaults=replace(policy.defaults, prune_min_age_days=val)
    )


def sacred_branch_names(policy: Policy, slug: str) -> frozenset[str]:
    """The effective sacred-branch set for ``slug``: the always-sacred
    ``main``/``master`` unioned with the configured ``sacred_branches`` (the
    ``defaults`` list plus any per-repo override). Matching is exact and
    case-sensitive, mirroring git's own branch-name semantics."""
    return SACRED_BRANCH_NAMES.union(policy_for(policy, slug).sacred_branches)


def partition_chain(
    chain_names: Iterable[str],
) -> tuple[list[type[Invariant]], list[type[Invariant]], list[type[Invariant]]]:
    """Look up each registered name and split by ``InvariantKind``.

    Returns ``(universal, per_repo, per_pr)``. Used by the PR-oriented
    commands; branch/worktree-oriented commands use a two-bucket variant.
    """
    universal: list[type[Invariant]] = []
    per_repo: list[type[Invariant]] = []
    per_pr: list[type[Invariant]] = []
    for name in chain_names:
        cls = get(name)
        if cls.kind == InvariantKind.UNIVERSAL:
            universal.append(cls)
        elif cls.kind == InvariantKind.PER_REPO:
            per_repo.append(cls)
        else:  # PER_PR
            per_pr.append(cls)
    return universal, per_repo, per_pr


def dc_to_dict(obj) -> dict:
    """Flatten a frozen dataclass into a YAML-friendly dict.

    Tuples become lists so the manifest serialises cleanly.
    """
    out: dict = {}
    for k, v in asdict(obj).items():
        out[k] = list(v) if isinstance(v, tuple) else v
    return out


def read_repos_text() -> str:
    """Read the configured ``repos.txt`` verbatim for the run manifest."""
    return paths.repos_file().read_text()
