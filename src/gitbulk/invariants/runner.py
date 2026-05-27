"""Chain runner for invariants.

See this.i node ``ivp4wq7n``. Override semantics from ``r4nzp7kq`` are
NOT enforced here; the caller computes the effective ``skip_set`` and
records any cmdline-relax audit events before invoking ``run_chain``.
"""

from __future__ import annotations

from dataclasses import dataclass

from gitbulk.invariants.base import (
    Fail,
    Invariant,
    InvariantContext,
    Pass,
    Skip,
)


@dataclass(frozen=True)
class ChainResult:
    """Outcome of running a chain of invariants against one target.

    ``passed`` is True iff no Fail was reached (Skips are still passing).
    ``fail_reason`` is set only when ``passed`` is False.
    ``skips`` is the tuple of (invariant_name, reason) pairs that were
    Skipped (either explicitly by the invariant or via ``skip_set``).
    """

    passed: bool
    fail_reason: str | None = None
    skips: tuple[tuple[str, str], ...] = ()


def run_chain(
    invariants: list[type[Invariant]],
    ctx: InvariantContext,
    *,
    skip_set: frozenset[str] = frozenset(),
    target: str = "global",
) -> ChainResult:
    """Run ``invariants`` in order; record outcomes to ``ctx.runstate``.

    Stops on the first Fail (whether returned or raised). A raised
    exception is converted to Fail and the traceback summary is recorded
    in ``errors.log`` so the run is debuggable. A non-Result return
    value is a programmer bug and raises TypeError.
    """
    skips: list[tuple[str, str]] = []
    for inv_cls in invariants:
        if inv_cls.name in skip_set:
            ctx.runstate.record_invariant(
                inv_cls.name,
                target,
                "SKIP",
                "skipped by configuration or --skip-check override",
            )
            skips.append((inv_cls.name, "configuration"))
            continue
        try:
            result = inv_cls().check(ctx)
        except Exception as e:
            reason = f"invariant raised: {type(e).__name__}: {e}"
            ctx.runstate.record_invariant(inv_cls.name, target, "FAIL", reason)
            ctx.runstate.record_error(
                f"invariant {inv_cls.name} raised unexpected exception",
                context={
                    "exception_type": type(e).__name__,
                    "exception": str(e),
                    "target": target,
                },
            )
            return ChainResult(
                passed=False, fail_reason=reason, skips=tuple(skips)
            )

        if isinstance(result, Pass):
            ctx.runstate.record_invariant(inv_cls.name, target, "PASS", None)
        elif isinstance(result, Skip):
            ctx.runstate.record_invariant(inv_cls.name, target, "SKIP", result.reason)
            skips.append((inv_cls.name, result.reason))
        elif isinstance(result, Fail):
            ctx.runstate.record_invariant(inv_cls.name, target, "FAIL", result.reason)
            return ChainResult(
                passed=False, fail_reason=result.reason, skips=tuple(skips)
            )
        else:
            raise TypeError(
                f"invariant {inv_cls.name} returned {type(result).__name__}; "
                f"expected Pass | Skip | Fail"
            )
    return ChainResult(passed=True, fail_reason=None, skips=tuple(skips))
