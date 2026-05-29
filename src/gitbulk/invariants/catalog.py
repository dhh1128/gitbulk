"""Concrete Phase 2 invariants (this.i node ``ph2inv4n``).

Each class registers itself via ``@register`` at import time. The
``gitbulk.invariants`` package re-imports this module for the
side effect; do not call into this module directly.

Invariant catalog:

  UNIVERSAL preflight
    - gh.authenticated
    - config.parseable
    - org.members.fresh

  PER_REPO preflight (subcommands whose ``needs_clone`` is True, plus
  ``github.reachable`` which applies to every gh-touching subcommand)
    - local.exists
    - local.remote_matches
    - local.default_branch_in_sync
    - github.reachable

  PER_PR baseline
    - pr.base_is_default
    - pr.author_known

Ordering in a chain is UNIVERSAL → PER_REPO → PER_PR; chain stops on
the first Fail per node ``c4jzm5pn``.

Skip-vs-Fail convention (from node ``5xqp2nkr``): "this one repo/PR
doesn't qualify" is a Skip; "the whole run is structurally broken"
is a Fail. The local-git probes return Skip when a clone is missing
or out-of-sync rather than aborting the whole run.

All local-git interactions use only the read-only allowlist from
node ``7mxr4pql`` (``rev-parse``, ``remote get-url``,
``symbolic-ref``). Mutating subcommands are not permitted from
invariants; this module never shells out to anything else.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone

from gitbulk.classifier import Classification, classify_login
from gitbulk.config.policy import policy_for
from gitbulk.gh import GHError
from gitbulk.invariants.base import (
    Fail,
    Invariant,
    InvariantContext,
    InvariantKind,
    Pass,
    Result,
    Skip,
)
from gitbulk.invariants.registry import register
from gitbulk.org_members_cache import is_fresh, load_cache
from gitbulk.ready import compute_ready_since
from gitbulk.util.businessdays import add_business_days

# Subcommand sets reused across invariants. Defined once so a typo
# would be caught (and so refactors only touch one place).
_ALL_SUBS: frozenset[str] = frozenset(
    {
        "report",
        "summarize",
        "dispatch",
        "merge",
        "rebase-pr",
        "close-stale",
    }
)
_CLONE_SUBS: frozenset[str] = frozenset({"dispatch", "rebase-pr"})
_MERGE_ONLY: frozenset[str] = frozenset({"merge"})
_CLOSE_STALE_ONLY: frozenset[str] = frozenset({"close-stale"})
_REBASE_PR_ONLY: frozenset[str] = frozenset({"rebase-pr"})

#: mergeable_state values that mean a rebase would help: BEHIND (base
#: advanced, clean fast-forward available) or DIRTY (real conflict — the
#: rebase will stop and the worktree is preserved for manual fix-up).
#: CLEAN needs nothing; BLOCKED is gated on review/checks not conflicts;
#: UNKNOWN/UNSTABLE/HAS_HOOKS don't indicate a base-staleness problem.
_REBASEABLE_MERGEABLE_STATES: frozenset[str] = frozenset({"BEHIND", "DIRTY"})


# ─── UNIVERSAL ────────────────────────────────────────────────────────────


@register
class GhAuthenticatedInvariant(Invariant):
    """Probe ``gh api user`` to confirm an authenticated gh CLI session."""

    name = "gh.authenticated"
    kind = InvariantKind.UNIVERSAL
    subcommands = _ALL_SUBS

    def check(self, ctx: InvariantContext) -> Result:
        if ctx.gh is None:
            return Fail("gh client not present on context")
        try:
            user = ctx.gh.authenticated_user()
        except GHError as e:
            return Fail(f"gh not authenticated: {e}")
        if not user.get("login"):
            return Fail(f"gh authenticated but user has no login: {user!r}")
        return Pass()


@register
class ConfigParseableInvariant(Invariant):
    """Sanity check that ``ctx.policy`` is loaded.

    The Policy is parsed by ``cli.py`` before the chain runs; if it
    weren't parseable the run wouldn't have started. This invariant
    is a defense-in-depth assertion that nobody constructed an
    InvariantContext with a placeholder policy.
    """

    name = "config.parseable"
    kind = InvariantKind.UNIVERSAL
    subcommands = _ALL_SUBS

    def check(self, ctx: InvariantContext) -> Result:
        if ctx.policy is None:
            return Fail("policy not loaded into context")
        return Pass()


@register
class OrgMembersFreshInvariant(Invariant):
    """Confirm the org-members cache exists and is younger than the TTL.

    If ``policy.humans.org`` is None the classifier falls through to
    BOT for unknown logins, which is the documented safe default; no
    cache is required in that mode.
    """

    name = "org.members.fresh"
    kind = InvariantKind.UNIVERSAL
    subcommands = _ALL_SUBS

    def check(self, ctx: InvariantContext) -> Result:
        org = ctx.policy.humans.org
        if org is None:
            return Pass()
        cached = load_cache(org)
        if cached is None:
            return Fail(
                f"org members cache for {org!r} is missing; "
                "rerun with --refresh-org-members"
            )
        if not is_fresh(cached, ctx.policy.humans.cache_ttl_hours):
            return Fail(
                f"org members cache for {org!r} is older than "
                f"{ctx.policy.humans.cache_ttl_hours}h; "
                "rerun with --refresh-org-members"
            )
        return Pass()


# ─── PER_REPO ─────────────────────────────────────────────────────────────


from gitbulk.util.github_url import extract_slug_from_url as _extract_slug_from_remote_url  # noqa: E402,F401


@register
class LocalExistsInvariant(Invariant):
    """Verify ``ctx.repo.local_path`` is a git working tree.

    Skip (not Fail) when missing or not a working tree: per node
    ``5xqp2nkr`` a missing clone is a per-repo skip, not a whole-run
    abort. The user's other 149 repos should still be processed.
    """

    name = "local.exists"
    kind = InvariantKind.PER_REPO
    subcommands = _CLONE_SUBS

    def check(self, ctx: InvariantContext) -> Result:
        if ctx.repo is None:
            return Fail("per-repo invariant called without ctx.repo")
        path = ctx.repo.local_path
        if not path.exists():
            return Skip(f"local clone missing at {path}")
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 or result.stdout.strip() != "true":
            return Skip(f"{path} is not a git working tree")
        return Pass()


@register
class LocalRemoteMatchesInvariant(Invariant):
    """Verify the clone's ``origin`` remote URL points at ``ctx.repo.slug``."""

    name = "local.remote_matches"
    kind = InvariantKind.PER_REPO
    subcommands = _CLONE_SUBS

    def check(self, ctx: InvariantContext) -> Result:
        if ctx.repo is None:
            return Fail("per-repo invariant called without ctx.repo")
        result = subprocess.run(
            ["git", "-C", str(ctx.repo.local_path), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return Skip(
                f"origin remote not configured: {result.stderr.strip()}"
            )
        url = result.stdout.strip()
        extracted = _extract_slug_from_remote_url(url)
        if extracted is None:
            return Skip(
                f"origin URL {url!r} is not a recognized GitHub remote"
            )
        if extracted != ctx.repo.slug:
            return Skip(
                f"origin points at {extracted!r} but configured slug "
                f"is {ctx.repo.slug!r}"
            )
        return Pass()


@register
class LocalDefaultBranchInSyncInvariant(Invariant):
    """Verify the local clone's ``origin/HEAD`` agrees with GitHub's
    current default branch for the slug.

    Skip (not Fail) on divergence: the user has a one-line fix
    (``git fetch && git remote set-head origin -a``) and we don't
    want to abort the whole run for it.
    """

    name = "local.default_branch_in_sync"
    kind = InvariantKind.PER_REPO
    subcommands = _CLONE_SUBS

    def check(self, ctx: InvariantContext) -> Result:
        if ctx.repo is None:
            return Fail("per-repo invariant called without ctx.repo")
        if ctx.gh is None:
            return Fail("per-repo invariant called without ctx.gh")
        try:
            github_default = ctx.gh.default_branch(ctx.repo.slug)
        except GHError as e:
            return Skip(
                f"could not determine github default branch: {e}"
            )
        result = subprocess.run(
            [
                "git",
                "-C",
                str(ctx.repo.local_path),
                "symbolic-ref",
                "refs/remotes/origin/HEAD",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return Skip(
                "local origin/HEAD not set: "
                f"{result.stderr.strip()}"
            )
        # symbolic-ref emits e.g. "refs/remotes/origin/main"
        symref = result.stdout.strip()
        prefix = "refs/remotes/origin/"
        if not symref.startswith(prefix):
            return Skip(
                f"unrecognized origin/HEAD symref {symref!r}"
            )
        local_default = symref[len(prefix):]
        if local_default != github_default:
            return Skip(
                f"local default branch {local_default!r} does not "
                f"match github default {github_default!r}"
            )
        return Pass()


@register
class GithubReachableInvariant(Invariant):
    """Single-call probe that ``ctx.gh`` works for this slug.

    Caches via the underlying gh client's own behavior; this
    invariant doesn't add a cache of its own.
    """

    name = "github.reachable"
    kind = InvariantKind.PER_REPO
    subcommands = _ALL_SUBS

    def check(self, ctx: InvariantContext) -> Result:
        if ctx.repo is None:
            return Fail("per-repo invariant called without ctx.repo")
        if ctx.gh is None:
            return Fail("per-repo invariant called without ctx.gh")
        try:
            ctx.gh.default_branch(ctx.repo.slug)
        except GHError as e:
            return Skip(f"github not reachable for {ctx.repo.slug}: {e}")
        return Pass()


# ─── PER_PR ───────────────────────────────────────────────────────────────


@register
class PrBaseIsDefaultInvariant(Invariant):
    """Verify ``ctx.pr.base_ref`` equals the repo's current default branch.

    Per AGENTS.md "Default branch detection": every PR-touching
    operation must verify the PR targets the current default branch
    before acting. A non-default base is a Skip with a prominent
    reason; ``--allow-non-default-base`` is the explicit override.
    """

    name = "pr.base_is_default"
    kind = InvariantKind.PER_PR
    subcommands = _ALL_SUBS

    def check(self, ctx: InvariantContext) -> Result:
        if ctx.pr is None or ctx.repo is None:
            return Fail("per-PR invariant called without ctx.pr/ctx.repo")
        if ctx.gh is None:
            return Fail("per-PR invariant called without ctx.gh")
        try:
            default = ctx.gh.default_branch(ctx.repo.slug)
        except GHError as e:
            return Skip(f"could not determine default branch: {e}")
        if ctx.pr.base_ref != default:
            return Skip(
                f"PR {ctx.pr.number} targets {ctx.pr.base_ref!r}, "
                f"repo default is {default!r}"
            )
        return Pass()


# ─── PER_PR (merge-only) ──────────────────────────────────────────────────


@register
class PrMergeableStateCleanInvariant(Invariant):
    """Skip the PR unless ``mergeable_state == "CLEAN"``.

    The merge subcommand will not call ``gh pr merge`` on a non-CLEAN
    PR; gh would refuse anyway and a Skip here surfaces the reason in
    the run summary rather than as a downstream gh error.
    """

    name = "pr.mergeable_state_clean"
    kind = InvariantKind.PER_PR
    subcommands = _MERGE_ONLY

    def check(self, ctx: InvariantContext) -> Result:
        if ctx.pr is None:
            return Fail("per-PR invariant called without ctx.pr")
        if ctx.pr.mergeable_state != "CLEAN":
            return Skip(
                f"mergeable_state is {ctx.pr.mergeable_state!r}, not 'CLEAN'"
            )
        return Pass()


@register
class PrRequiredChecksGreenInvariant(Invariant):
    """Skip unless ``checks_status == "SUCCESS"``."""

    name = "pr.required_checks_green"
    kind = InvariantKind.PER_PR
    subcommands = _MERGE_ONLY

    def check(self, ctx: InvariantContext) -> Result:
        if ctx.pr is None:
            return Fail("per-PR invariant called without ctx.pr")
        if ctx.pr.checks_status != "SUCCESS":
            return Skip(
                f"checks_status is {ctx.pr.checks_status!r}, not 'SUCCESS'"
            )
        return Pass()


@register
class PrApprovedPerPolicyInvariant(Invariant):
    """Apply the per-repo merge_policy gate.

    Three branches:
      - ``strict``: Skip unless ``review_decision == "APPROVED"``.
      - ``ci-only``: always Pass (CI is sufficient evidence).
      - ``never``: always Skip — the user has opted this repo out of
        gitbulk-driven merges entirely.
    """

    name = "pr.approved_per_policy"
    kind = InvariantKind.PER_PR
    subcommands = _MERGE_ONLY

    def check(self, ctx: InvariantContext) -> Result:
        if ctx.pr is None or ctx.repo is None:
            return Fail("per-PR invariant called without ctx.pr/ctx.repo")
        effective = policy_for(ctx.policy, ctx.repo.slug)
        if effective.merge_policy == "never":
            return Skip(
                f"merge_policy is 'never' for {ctx.repo.slug!r}; "
                "gitbulk will not merge this repo's PRs"
            )
        if effective.merge_policy == "ci-only":
            return Pass()
        # strict
        if ctx.pr.review_decision != "APPROVED":
            return Skip(
                f"merge_policy=strict requires APPROVED review_decision; "
                f"got {ctx.pr.review_decision!r}"
            )
        return Pass()


@register
class PrNoUnresolvedThreadsInvariant(Invariant):
    """Skip the PR unless every review thread is resolved.

    Per the merge-gate decision in ``gaps.md`` and zk3r4nqp: an unresolved
    review thread is an explicit human signal that something is awaiting
    response, and gitbulk must not bypass it. Bots are counted alongside
    humans — see the upfront merge-gate question/answer recorded in the
    Phase-5+ design session. Surfaced in the run summary as a
    "blocked (waiting on humans)" classification, distinguishing it from
    structural readiness skips like ``pr.required_checks_green``.
    """

    name = "pr.no_unresolved_threads"
    kind = InvariantKind.PER_PR
    subcommands = _MERGE_ONLY

    def check(self, ctx: InvariantContext) -> Result:
        if ctx.pr is None:
            return Fail("per-PR invariant called without ctx.pr")
        count = ctx.pr.unresolved_thread_count
        if count > 0:
            return Skip(
                f"{count} unresolved review thread(s) "
                "(blocked: waiting on humans)"
            )
        return Pass()


@register
class PrAgeThresholdInvariant(Invariant):
    """Skip unless the PR has been continuously ready long enough.

    "Long enough" = at least ``policy.defaults.min_business_days`` business
    days have elapsed between the timeline-aware ``ready_since`` anchor
    and "now". See ``bg4pqn7m`` (Three Business Days From Continuously
    Ready) and the ``compute_ready_since`` docstring for the algorithm.

    Implementation detail: ``add_business_days(ready_since, n)`` gives the
    moment at which the PR becomes age-eligible. We compare ``now`` to
    that moment; if ``now`` is earlier, Skip with the remaining duration.

    Approval short-circuit (zk3r4nqp): if the PR's review_decision is
    APPROVED, the age threshold is bypassed and the invariant Passes
    immediately. Rationale: the 3-business-day window's purpose is to
    let humans react; an explicit approval IS the reaction, so further
    waiting is pointless. Under strict policy this makes the gate
    effectively vacuous (``pr.approved_per_policy`` already required
    APPROVED to get this far); under ci-only it remains meaningful for
    PRs with green CI but no human review yet.
    """

    name = "pr.age_threshold"
    kind = InvariantKind.PER_PR
    subcommands = _MERGE_ONLY

    def check(self, ctx: InvariantContext) -> Result:
        if ctx.pr is None or ctx.repo is None:
            return Fail("per-PR invariant called without ctx.pr/ctx.repo")
        # Approval short-circuit: an APPROVED review is the merge signal
        # we're waiting for, so further age-gating adds no value.
        if ctx.pr.review_decision == "APPROVED":
            return Pass()
        effective = policy_for(ctx.policy, ctx.repo.slug)
        require_approval = effective.merge_policy != "ci-only"
        ready_since = compute_ready_since(
            ctx.pr, require_approval=require_approval
        )
        if ready_since is None:
            return Skip(
                "PR is not currently ready (mergeable_state/checks/review "
                "do not all qualify)"
            )
        eligible_at = add_business_days(ready_since, effective.min_business_days)
        now = _utc_now()
        if now < eligible_at:
            return Skip(
                f"ready_since={ready_since.isoformat()} but needs "
                f"{effective.min_business_days} business days; "
                f"eligible at {eligible_at.isoformat()}"
            )
        return Pass()


def _utc_now() -> datetime:
    """Indirection so tests can monkeypatch the clock on this module."""
    return datetime.now(timezone.utc)


@register
class PrAuthorKnownInvariant(Invariant):
    """Confirm the classifier can decide HUMAN-or-BOT for ``ctx.pr.author``.

    UNKNOWN is reserved for tooling running without an org-members
    cache; in production the ``org.members.fresh`` preflight
    guarantees a cache is loaded before this invariant runs, so
    UNKNOWN here is a defensive Fail.
    """

    name = "pr.author_known"
    kind = InvariantKind.PER_PR
    subcommands = _ALL_SUBS

    def check(self, ctx: InvariantContext) -> Result:
        if ctx.pr is None:
            return Fail("per-PR invariant called without ctx.pr")
        org_members = None
        if ctx.policy.humans.org:
            cached = load_cache(ctx.policy.humans.org)
            if cached is not None:
                org_members = cached.members
        result = classify_login(ctx.pr.author, ctx.policy, org_members)
        if result == Classification.UNKNOWN:
            return Fail(
                f"classifier returned UNKNOWN for {ctx.pr.author!r}"
            )
        return Pass()


# ─── PER_PR (close-stale-only) ────────────────────────────────────────────


@register
class PrInactiveInvariant(Invariant):
    """Pass unless the PR was touched too recently to plausibly be a
    close-stale candidate.

    Threshold is ``stale_cooloff_days``, NOT ``stale_age_days``. Reason:
    after gitbulk posts its own stale-warning comment, GitHub bumps
    ``updated_at`` to the comment time. If this invariant used
    ``stale_age_days`` (e.g. 60), a warned PR in its 7-day cooloff
    window would now appear "active" (updated 7 days ago, threshold 60),
    skip the chain, and never close. Using ``stale_cooloff_days``
    catches both the first-pass stale PRs and the warned-in-cooloff
    PRs, with the handler refining via ``stale_age_days`` for warn
    decisions specifically.

    A repo with effective ``stale_policy == "never"`` is Skipped
    regardless of inactivity (the per-repo opt-out).

    The two-phase warn-then-close decision happens in the close-stale
    handler, not here.
    """

    name = "pr.inactive"
    kind = InvariantKind.PER_PR
    subcommands = _CLOSE_STALE_ONLY

    def check(self, ctx: InvariantContext) -> Result:
        if ctx.pr is None or ctx.repo is None:
            return Fail("per-PR invariant called without ctx.pr/ctx.repo")
        effective = policy_for(ctx.policy, ctx.repo.slug)
        if effective.stale_policy == "never":
            return Skip(
                f"stale_policy=never for {ctx.repo.slug!r}; "
                "close-stale will not consider this repo's PRs"
            )
        threshold_days = effective.stale_cooloff_days
        age = _utc_now() - ctx.pr.updated_at
        if age.days < threshold_days:
            return Skip(
                f"PR active within stale_cooloff_days={threshold_days} "
                f"(updated {age.days} days ago)"
            )
        return Pass()


@register
class PrNeedsRebaseInvariant(Invariant):
    """Pass only when the PR would actually benefit from a rebase.

    "Benefits" = ``mergeable_state`` is BEHIND (base advanced, a clean
    rebase brings it current) or DIRTY (a real conflict — rebase-pr
    will stop at the conflict and preserve the worktree for manual
    resolution). Every other state Skips:

      - CLEAN: already up to date, nothing to do.
      - BLOCKED: blocked on review/checks, not on base staleness — a
        rebase wouldn't change anything.
      - UNKNOWN: GitHub is still computing mergeability; can't tell, so
        skip conservatively (the next run will know).
      - UNSTABLE / HAS_HOOKS: mergeable; not a base-staleness problem.

    rebase-pr-only. The clone-touching baseline (local.*, base_is_default,
    author_known) runs ahead of this in the chain.
    """

    name = "pr.needs_rebase"
    kind = InvariantKind.PER_PR
    subcommands = _REBASE_PR_ONLY

    def check(self, ctx: InvariantContext) -> Result:
        if ctx.pr is None:
            return Fail("per-PR invariant called without ctx.pr")
        state = ctx.pr.mergeable_state
        if state in _REBASEABLE_MERGEABLE_STATES:
            return Pass()
        return Skip(
            f"mergeable_state={state!r} does not warrant a rebase "
            f"(only {sorted(_REBASEABLE_MERGEABLE_STATES)} do)"
        )
