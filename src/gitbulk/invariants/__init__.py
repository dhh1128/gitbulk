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
    "seed_org_members",
]

# Side-effect import: registers all concrete invariants in the catalog.
from gitbulk.invariants import catalog  # noqa: F401, E402

# Re-export the once-per-run org-members seeding helper (node 37ic).
from gitbulk.invariants.catalog import seed_org_members  # noqa: E402
