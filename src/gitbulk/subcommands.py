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
from typing import Literal

LockMode = Literal["shared", "exclusive"]


# Phase 2 invariant chains (this.i node ``ph2inv4n``). Ordering is
# UNIVERSAL → PER_REPO → PER_PR per ``c4jzm5pn``; the chain runner
# stops on first Fail and the subcommand handler partitions by kind.
_GH_TOUCHING_CHAIN: tuple[str, ...] = (
    "gh.authenticated",
    "config.parseable",
    "org.members.fresh",
    "github.reachable",
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
    "pr.base_is_default",
    "pr.author_known",
    "pr.mergeable_state_clean",
    "pr.required_checks_green",
    "pr.approved_per_policy",
    "pr.no_unresolved_threads",
    "pr.age_threshold",
)


@dataclass(frozen=True)
class Subcommand:
    """Static metadata about one gitbulk subcommand."""

    name: str
    help: str
    mutating: bool
    """When True, the subcommand defaults to --dry-run (per node 2vqp4nk6)."""
    lock_mode: LockMode
    """Global lock mode this subcommand acquires (per node lj5pqn4kr).
    Mutating subcommands take exclusive; read-only take shared."""
    needs_clone: bool
    """When True, the local.exists invariant (node 5xqp2nkr) applies."""
    invariant_chain: tuple[str, ...] = field(default=())
    """Registered invariant names this subcommand runs, in order.

    Per this.i node ``scinv4qm``. Empty tuple is valid (e.g. ``ack``,
    ``invariants``, ``show``).
    """


KNOWN: tuple[Subcommand, ...] = (
    Subcommand(
        name="report",
        help="Summarize the state of your open PRs across all repos.",
        mutating=False,
        lock_mode="shared",
        needs_clone=False,
        invariant_chain=_GH_TOUCHING_CHAIN,
    ),
    Subcommand(
        name="summarize",
        help="Run Claude over a previous report to prioritize attention.",
        mutating=False,
        lock_mode="shared",
        needs_clone=False,
        invariant_chain=_GH_TOUCHING_CHAIN,
    ),
    Subcommand(
        name="dispatch",
        help="Launch headless Claude agents against PRs matching a filter.",
        mutating=True,
        lock_mode="exclusive",
        needs_clone=True,
        invariant_chain=_CLONE_TOUCHING_CHAIN,
    ),
    Subcommand(
        name="merge",
        help="Auto-merge PRs that satisfy the per-repo merge policy.",
        mutating=True,
        lock_mode="exclusive",
        needs_clone=False,
        invariant_chain=_MERGE_CHAIN,
    ),
    Subcommand(
        name="rebase-onto-default",
        help="Rebase your PRs onto their repo's default branch.",
        mutating=True,
        lock_mode="exclusive",
        needs_clone=True,
        invariant_chain=_CLONE_TOUCHING_CHAIN,
    ),
    Subcommand(
        name="close-stale",
        help="Close PRs that are inactive past the configured threshold.",
        mutating=True,
        lock_mode="exclusive",
        needs_clone=False,
        invariant_chain=_GH_TOUCHING_CHAIN,
    ),
    Subcommand(
        name="show",
        help="Show the latest run of a given subcommand.",
        mutating=False,
        lock_mode="shared",
        needs_clone=False,
        invariant_chain=(),
    ),
    Subcommand(
        name="ack",
        help="Clear the ATTENTION sentinel after you have reviewed it.",
        mutating=False,
        lock_mode="shared",
        needs_clone=False,
        invariant_chain=(),
    ),
    Subcommand(
        name="invariants",
        help="List the invariant registry and which subcommands use them.",
        mutating=False,
        lock_mode="shared",
        needs_clone=False,
        invariant_chain=(),
    ),
)

NAMES: tuple[str, ...] = tuple(s.name for s in KNOWN)


def by_name(name: str) -> Subcommand:
    """Return the Subcommand metadata for ``name``. KeyError if unknown."""
    for s in KNOWN:
        if s.name == name:
            return s
    raise KeyError(f"unknown subcommand: {name!r}")
