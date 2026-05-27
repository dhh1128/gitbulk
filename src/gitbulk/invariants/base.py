"""Core types for the invariants framework.

See this.i node ``ivp4wq7n``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar

from gitbulk.config.policy import Policy
from gitbulk.config.repos import RepoEntry
from gitbulk.runstate import RunState


@dataclass(frozen=True)
class Pass:
    """The invariant passed; proceed."""


@dataclass(frozen=True)
class Skip:
    """Skip this target (repo or PR) with a reason; the chain continues."""

    reason: str


@dataclass(frozen=True)
class Fail:
    """Abort the whole run; the chain stops. Use for structural failures
    (bad auth, missing config, malformed state) rather than 'this one PR
    didn't qualify' situations."""

    reason: str


Result = Pass | Skip | Fail


class InvariantKind(str, Enum):
    """What scope an Invariant operates on.

    UNIVERSAL: run once per gitbulk run (no repo or PR context).
    PER_REPO: run once per configured repo (ctx.repo not None).
    PER_PR: run once per PR within a repo (ctx.repo and ctx.pr not None).
    """

    UNIVERSAL = "universal"
    PER_REPO = "per-repo"
    PER_PR = "per-pr"


@dataclass(frozen=True)
class InvariantContext:
    """Bag of read-only state an Invariant.check() receives.

    Universal invariants leave ``repo``/``pr``/``gh`` as None; per-repo
    and per-PR invariants assert non-None inside check() rather than
    relying on a separate context type per kind.
    """

    policy: Policy
    runstate: RunState
    repo: RepoEntry | None = None
    pr: Any = None  # PRInfo | None — defined in Phase 2
    gh: Any = None  # GHClient | None — defined in Phase 2


class Invariant(ABC):
    """Base class for invariants.

    Subclasses declare ``name``, ``kind``, and ``subcommands`` as
    ClassVars and implement ``check()``.
    """

    name: ClassVar[str]
    kind: ClassVar[InvariantKind]
    subcommands: ClassVar[frozenset[str]]

    @abstractmethod
    def check(self, ctx: InvariantContext) -> Result:
        ...
