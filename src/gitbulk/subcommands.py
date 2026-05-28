"""Typed registry of gitbulk's subcommands.

Single source of truth for the metadata every subcommand carries.
Resolves the layering inversion identified by the platform-architect
adversarial review (2026-05-27, finding P-F3): ``cli.py`` and
``dashboard.py`` both consumed the subcommand list, so its home is
neither of them.

See this.i node ``smodlpr3`` for the contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

LockMode = Literal["shared", "exclusive"]


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


KNOWN: tuple[Subcommand, ...] = (
    Subcommand(
        name="report",
        help="Summarize the state of your open PRs across all repos.",
        mutating=False,
        lock_mode="shared",
        needs_clone=False,
    ),
    Subcommand(
        name="summarize",
        help="Run Claude over a previous report to prioritize attention.",
        mutating=False,
        lock_mode="shared",
        needs_clone=False,
    ),
    Subcommand(
        name="dispatch",
        help="Launch headless Claude agents against PRs matching a filter.",
        mutating=True,
        lock_mode="exclusive",
        needs_clone=True,
    ),
    Subcommand(
        name="merge",
        help="Auto-merge PRs that satisfy the per-repo merge policy.",
        mutating=True,
        lock_mode="exclusive",
        needs_clone=False,
    ),
    Subcommand(
        name="rebase-onto-default",
        help="Rebase your PRs onto their repo's default branch.",
        mutating=True,
        lock_mode="exclusive",
        needs_clone=True,
    ),
    Subcommand(
        name="close-stale",
        help="Close PRs that are inactive past the configured threshold.",
        mutating=True,
        lock_mode="exclusive",
        needs_clone=False,
    ),
    Subcommand(
        name="show",
        help="Show the latest run of a given subcommand.",
        mutating=False,
        lock_mode="shared",
        needs_clone=False,
    ),
    Subcommand(
        name="ack",
        help="Clear the ATTENTION sentinel after you have reviewed it.",
        mutating=False,
        lock_mode="shared",
        needs_clone=False,
    ),
    Subcommand(
        name="invariants",
        help="List the invariant registry and which subcommands use them.",
        mutating=False,
        lock_mode="shared",
        needs_clone=False,
    ),
)

NAMES: tuple[str, ...] = tuple(s.name for s in KNOWN)


def by_name(name: str) -> Subcommand:
    """Return the Subcommand metadata for ``name``. KeyError if unknown."""
    for s in KNOWN:
        if s.name == name:
            return s
    raise KeyError(f"unknown subcommand: {name!r}")
