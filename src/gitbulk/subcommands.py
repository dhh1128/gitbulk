"""Typed registry of gitbulk's subcommands.

Single source of truth for the metadata every subcommand carries.
Resolves the layering inversion identified by the platform-architect
adversarial review (2026-05-27, finding P-F3): ``cli.py`` and
``dashboard.py`` both consumed the subcommand list, so its home is
neither of them.

See this.i node ``smodlpr3`` for the contract; ``scinv4qm`` for the
``invariant_chain`` field (Phase 2).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Phase 2 invariant chains (this.i node ``ph2inv4n``). Ordering is
# UNIVERSAL → PER_REPO → PER_PR per ``c4jzm5pn``; the chain runner
# stops on first Fail and the subcommand handler partitions by kind.
_GH_TOUCHING_CHAIN: tuple[str, ...] = (
    "gh.authenticated",
    "config.parseable",
    "org.members.fresh",
    "github.reachable",
    "github.not_archived",
    "pr.base_is_default",
    "pr.author_known",
)

_CLONE_TOUCHING_CHAIN: tuple[str, ...] = (
    "gh.authenticated",
    "config.parseable",
    "org.members.fresh",
    "local.exists",
    "local.remote_matches",
    "local.default_branch_in_sync",
    "github.reachable",
    "github.not_archived",
    "pr.base_is_default",
    "pr.author_known",
)

# Phase 5 chain for ``merge``. Layers the four merge-only PER_PR
# invariants (mergeable_state, checks green, approval policy, age
# threshold) onto the standard gh-touching baseline. Ordering matters:
# the gh-touching baseline (PER_PR) runs first so a wrong-base PR is
# Skipped before the merge-specific checks even consider the PR.
_MERGE_CHAIN: tuple[str, ...] = (
    "gh.authenticated",
    "config.parseable",
    "org.members.fresh",
    "github.reachable",
    "github.not_archived",
    "pr.base_is_default",
    "pr.author_known",
    "pr.mergeable_state_clean",
    "pr.required_checks_green",
    "pr.approved_per_policy",
    "pr.no_unresolved_threads",
    "pr.age_threshold",
)

# Phase 6+ chain for ``close-stale``. Layers the close-stale-only
# ``pr.inactive`` invariant onto the gh-touching baseline. The two-phase
# warn-then-close decision lives in the handler (post-chain), not in the
# invariant chain — chains are gates, not action selectors.
_CLOSE_STALE_CHAIN: tuple[str, ...] = (
    "gh.authenticated",
    "config.parseable",
    "org.members.fresh",
    "github.reachable",
    "github.not_archived",
    "pr.base_is_default",
    "pr.author_known",
    "pr.inactive",
)

# Chain for ``rebase-pr``. Clone-touching (it force-pushes a rebased
# head branch from a disposable worktree), so it layers the local.*
# preflights onto the gh baseline, then gates on ``pr.needs_rebase``
# (only BEHIND/DIRTY PRs warrant a rebase). pr.author_is_me is NOT a
# separate invariant: my_open_prs already searches ``author:@me``, so
# every PR the handler sees is mine by construction; an always-passing
# invariant that re-fetches the user per PR would be wasted work.
_REBASE_PR_CHAIN: tuple[str, ...] = (
    "gh.authenticated",
    "config.parseable",
    "org.members.fresh",
    "local.exists",
    "local.remote_matches",
    "local.default_branch_in_sync",
    "github.reachable",
    "github.not_archived",
    "pr.base_is_default",
    "pr.author_known",
    "pr.needs_rebase",
)


# Chain for ``prune-branches`` (node prnbr4kq). Clone-free like merge — it
# operates on remote branches through gh only — so it carries just the
# UNIVERSAL + PER_REPO gh gates. There is no per-PR member: the command's
# unit of work is a BRANCH, and its branch-level guardrails (default,
# protected, open-PR head/base, data-loss, fork, grace) live in the handler,
# not the chain (chains gate, they don't iterate branches).
_PRUNE_BRANCHES_CHAIN: tuple[str, ...] = (
    "gh.authenticated",
    "config.parseable",
    "org.members.fresh",
    "github.reachable",
    "github.not_archived",
)

# Chain for ``prune-worktrees`` (node prnwt5nq). Clone-touching: it must read
# `git worktree list` from each clone, so it layers local.exists +
# local.remote_matches onto the gh gates. default_branch_in_sync is omitted
# deliberately — a clone whose default branch drifted can still have its
# orphaned worktrees pruned safely. Per-worktree guards live in the handler.
_PRUNE_WORKTREES_CHAIN: tuple[str, ...] = (
    "gh.authenticated",
    "config.parseable",
    "org.members.fresh",
    "local.exists",
    "local.remote_matches",
    "github.reachable",
    "github.not_archived",
)


@dataclass(frozen=True)
class Subcommand:
    """Static metadata about one gitbulk subcommand."""

    name: str
    help: str
    mutating: bool
    """When True, the subcommand defaults to --dry-run (per node 2vqp4nk6)."""
    needs_clone: bool
    """When True, the local.exists invariant (node 5xqp2nkr) applies."""
    invariant_chain: tuple[str, ...] = field(default=())
    """Registered invariant names this subcommand runs, in order.

    Per this.i node ``scinv4qm``. Empty tuple is valid (e.g. ``ack``,
    ``invariants``, ``show``).
    """
    sets_attention: bool = False
    """When True, the subcommand can raise the ATTENTION sentinel (exit 2/3)
    and so a clean (exit 0) run of it supersedes its own stale sentinel.

    Per this.i node ``aklr5pq3``. True for the six fleet operations
    (report, summarize, dispatch, merge, rebase-pr, close-stale); False for
    show/ack/invariants, which never set attention.
    """


KNOWN: tuple[Subcommand, ...] = (
    Subcommand(
        name="report",
        help="Summarize the state of your open PRs across all repos.",
        mutating=False,
        needs_clone=False,
        invariant_chain=_GH_TOUCHING_CHAIN,
        sets_attention=True,
    ),
    Subcommand(
        name="summarize",
        help="Run Claude over a previous report to prioritize attention.",
        mutating=False,
        needs_clone=False,
        invariant_chain=_GH_TOUCHING_CHAIN,
        sets_attention=True,
    ),
    Subcommand(
        name="dispatch",
        help="Launch headless Claude agents against PRs matching a filter.",
        mutating=True,
        needs_clone=True,
        invariant_chain=_CLONE_TOUCHING_CHAIN,
        sets_attention=True,
    ),
    Subcommand(
        name="merge",
        help="Auto-merge PRs that satisfy the per-repo merge policy.",
        mutating=True,
        needs_clone=False,
        invariant_chain=_MERGE_CHAIN,
        sets_attention=True,
    ),
    Subcommand(
        name="rebase-pr",
        help="Rebase your behind/conflicting PRs onto their current base.",
        mutating=True,
        needs_clone=True,
        invariant_chain=_REBASE_PR_CHAIN,
        sets_attention=True,
    ),
    Subcommand(
        name="close-stale",
        help="Close PRs that are inactive past the configured threshold.",
        mutating=True,
        needs_clone=False,
        invariant_chain=_CLOSE_STALE_CHAIN,
        sets_attention=True,
    ),
    Subcommand(
        name="prune-branches",
        help="Delete remote branches whose only PRs are merged or closed.",
        mutating=True,
        needs_clone=False,
        invariant_chain=_PRUNE_BRANCHES_CHAIN,
        sets_attention=True,
    ),
    Subcommand(
        name="prune-worktrees",
        help="Remove local worktrees whose branch's only PRs are merged/closed.",
        mutating=True,
        needs_clone=True,
        invariant_chain=_PRUNE_WORKTREES_CHAIN,
        sets_attention=True,
    ),
    Subcommand(
        name="recover-branch",
        help="Restore a branch that prune-branches deleted, from its audit log.",
        mutating=True,
        needs_clone=False,
        invariant_chain=(),
    ),
    Subcommand(
        name="show",
        help="Show the latest run of a given subcommand.",
        mutating=False,
        needs_clone=False,
        invariant_chain=(),
    ),
    Subcommand(
        name="ack",
        help="Clear the ATTENTION sentinel after you have reviewed it.",
        mutating=False,
        needs_clone=False,
        invariant_chain=(),
    ),
    Subcommand(
        name="invariants",
        help="List the invariant registry and which subcommands use them.",
        mutating=False,
        needs_clone=False,
        invariant_chain=(),
    ),
)

NAMES: tuple[str, ...] = tuple(s.name for s in KNOWN)

#: Subcommands that can raise the ATTENTION sentinel; a clean (exit 0) run
#: of one of these supersedes its own stale sentinel (node ``aklr5pq3``).
ATTENTION_PRODUCING_NAMES: frozenset[str] = frozenset(
    s.name for s in KNOWN if s.sets_attention
)


def by_name(name: str) -> Subcommand:
    """Return the Subcommand metadata for ``name``. KeyError if unknown."""
    for s in KNOWN:
        if s.name == name:
            return s
    raise KeyError(f"unknown subcommand: {name!r}")
