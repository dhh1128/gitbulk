"""Public surface for the invariants framework.

See this.i node ``ivp4wq7n`` for the implementation contract and
``c4jzm5pn`` for the policy-as-named-chains rationale.
"""

from gitbulk.invariants.base import (
    Fail,
    Invariant,
    InvariantContext,
    InvariantKind,
    Pass,
    Result,
    Skip,
)
from gitbulk.invariants.registry import (
    all_invariants,
    for_subcommand,
    get,
    register,
)
from gitbulk.invariants.runner import ChainResult, run_chain

__all__ = [
    "Fail",
    "Invariant",
    "InvariantContext",
    "InvariantKind",
    "Pass",
    "Result",
    "Skip",
    "all_invariants",
    "for_subcommand",
    "get",
    "register",
    "ChainResult",
    "run_chain",
]

# Side-effect import: registers all concrete invariants in the catalog.
from gitbulk.invariants import catalog  # noqa: F401, E402
