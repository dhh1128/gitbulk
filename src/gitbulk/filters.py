"""Fleet-subset filtering for gitbulk subcommands (this.i node ``flt7arg2``).

A selection layer that runs AROUND the invariant chain, not inside it.
Filters express user INTENT ("only look at these") — distinct from
invariants, which are SAFETY gates ("this isn't safe to act on"). A
filtered-out repo is out of scope, NOT skipped-for-attention, so filters
must never feed the exit-3 "skipped" signal. Hence this separate module.

Two filter stages, matching the pipeline:

  - :func:`select_repos` prunes the RepoEntry list BEFORE the per-repo
    invariant loop and the PR fetch — a repo filter makes the run
    cheaper, not just narrower.
  - :func:`select_prs` prunes the PRInfo list AFTER the fetch.

The ``author`` dimension is special: it can't be applied client-side
(the search only fetched matching authors in the first place), so it's
pushed into ``gh.my_open_prs(author=...)`` at fetch time and is NOT
handled here.

v1 dimensions: org, repo glob, base, mergeable_state, author. Deferred
to v2 (see node ``flt7arg2``): on-disk path, PR age, regex (vs glob),
negation, single ``--pr`` targeting.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

from gitbulk.config.repos import RepoEntry
from gitbulk.pr_info import PRInfo


@dataclass(frozen=True)
class FilterSpec:
    """A resolved fleet-subset selection. Empty tuple = unconstrained on
    that dimension. Dimensions AND together; values within a dimension
    OR together.

    ``authors`` is recorded here for the fetch layer + the per-command
    author veto, but is NOT applied by :func:`select_prs` (the search
    already enforced it).
    """

    orgs: tuple[str, ...] = ()
    repo_globs: tuple[str, ...] = ()
    bases: tuple[str, ...] = ()
    mergeable_states: tuple[str, ...] = ()
    authors: tuple[str, ...] = ()
    # TECH_DEBT: filter spec v2 dimensions [this.i node flt7arg2]
    # v2 dimensions land here — on-disk path, PR age, regex match, negation,
    # single --pr targeting. New repo-level dims extend constrains_repos +
    # _matches_repo; PR-level dims extend constrains_prs + select_prs;
    # remember to widen resolve_filter_spec, the policy `filters:` parser,
    # and filter_summary_line in lockstep. (Cross-module: tracked by this.i
    # node flt7arg2.)

    @property
    def is_empty(self) -> bool:
        """True when no dimension constrains anything (whole fleet)."""
        return not (
            self.orgs
            or self.repo_globs
            or self.bases
            or self.mergeable_states
            or self.authors
        )

    @property
    def constrains_repos(self) -> bool:
        return bool(self.orgs or self.repo_globs)

    @property
    def constrains_prs(self) -> bool:
        return bool(self.bases or self.mergeable_states)


def _matches_repo(slug: str, spec: FilterSpec) -> bool:
    """True if ``slug`` (owner/repo) passes the repo-level constraints.

    org: the owner segment must equal one of ``spec.orgs`` (exact, the
    natural ergonomic for "everything under this org"). repo_globs:
    fnmatch against the FULL slug, so both ``provenant-dev/origin-*`` and
    ``*/origin-*`` work. Both constraints AND together when present.
    """
    owner = slug.split("/", 1)[0]
    if spec.orgs and owner not in spec.orgs:
        return False
    if spec.repo_globs and not any(
        fnmatch.fnmatch(slug, g) for g in spec.repo_globs
    ):
        return False
    return True


def select_repos(
    repos: list[RepoEntry], spec: FilterSpec
) -> tuple[list[RepoEntry], int]:
    """Return ``(kept, excluded_count)`` after applying repo filters."""
    if not spec.constrains_repos:
        return repos, 0
    kept = [r for r in repos if _matches_repo(r.slug, spec)]
    return kept, len(repos) - len(kept)


def _matches_pr(pr: PRInfo, spec: FilterSpec) -> bool:
    if spec.bases and pr.base_ref not in spec.bases:
        return False
    if spec.mergeable_states and (pr.mergeable_state or "") not in spec.mergeable_states:
        return False
    return True


def select_prs(
    prs: list[PRInfo], spec: FilterSpec
) -> tuple[list[PRInfo], int]:
    """Return ``(kept, excluded_count)`` after applying PR filters.

    Author is intentionally not applied here (enforced at fetch time).
    """
    if not spec.constrains_prs:
        return prs, 0
    kept = [pr for pr in prs if _matches_pr(pr, spec)]
    return kept, len(prs) - len(kept)


def fetch_author(spec: FilterSpec, *, default: str = "@me") -> str | None:
    """The author qualifier to pass to ``gh.my_open_prs``.

    No ``--author`` → the default (``@me``, preserving prior behavior).
    A single author → that login. Multiple authors aren't expressible
    in one search qualifier in v1, so the first is used at fetch time
    and the rest are documented as a v2 gap (node ``flt7arg2``); in
    practice the CLI only sets one author.
    """
    if not spec.authors:
        return default
    return spec.authors[0]


def apply_pr_filters(
    prs_by_repo: dict[str, list[PRInfo]], spec: FilterSpec
) -> tuple[dict[str, list[PRInfo]], int]:
    """Filter every repo's PR list by ``spec``; return (filtered_map,
    total_excluded). Keeps the per-repo grouping intact."""
    if not spec.constrains_prs:
        return prs_by_repo, 0
    out: dict[str, list[PRInfo]] = {}
    excluded = 0
    for slug, prs in prs_by_repo.items():
        kept, n = select_prs(prs, spec)
        out[slug] = kept
        excluded += n
    return out, excluded


def filter_summary_line(
    spec: FilterSpec, repos_excluded: int, prs_excluded: int
) -> str | None:
    """One-line "Filtered: ..." note for a run summary, or None when no
    filter was active (so summaries stay clean for unfiltered runs).

    Deliberately distinct wording from "Skipped" — a filtered-out item
    is out of scope by user choice, not flagged for attention."""
    if spec.is_empty:
        return None
    dims: list[str] = []
    if spec.orgs:
        dims.append(f"org={','.join(spec.orgs)}")
    if spec.repo_globs:
        dims.append(f"repo={','.join(spec.repo_globs)}")
    if spec.bases:
        dims.append(f"base={','.join(spec.bases)}")
    if spec.mergeable_states:
        dims.append(f"mergeable_state={','.join(spec.mergeable_states)}")
    if spec.authors:
        dims.append(f"author={','.join(spec.authors)}")
    return (
        f"Filtered [{' '.join(dims)}]: "
        f"{repos_excluded} repos, {prs_excluded} PRs excluded"
    )


def resolve_filter_spec(args, policy) -> FilterSpec:
    """Merge a named config filter set (``--filter NAME``) with CLI
    flags. CLI flags NARROW: a CLI value on a dimension replaces the
    named set's value on that dimension (it doesn't union — narrowing
    is the intuitive verb for "I typed a flag to focus further").

    Reads optional attributes off ``args`` so handlers that haven't
    wired every flag still work; missing → unconstrained.
    """
    # Start from the named set, if any.
    base = FilterSpec()
    name = getattr(args, "filter", None)
    if name:
        named = policy.filters.get(name)
        if named is None:
            from gitbulk.config.repos import ConfigError

            raise ConfigError(
                f"--filter {name!r} not found; defined filter sets: "
                f"{sorted(policy.filters) or '(none)'}"
            )
        base = named

    def _tuple(attr: str, current: tuple[str, ...]) -> tuple[str, ...]:
        val = getattr(args, attr, None)
        if val:
            return tuple(val)
        return current

    return FilterSpec(
        orgs=_tuple("org", base.orgs),
        repo_globs=_tuple("repo", base.repo_globs),
        bases=_tuple("base", base.bases),
        mergeable_states=_tuple("mergeable_state", base.mergeable_states),
        authors=_tuple("author", base.authors),
    )


__all__ = [
    "FilterSpec",
    "fetch_author",
    "resolve_filter_spec",
    "select_prs",
    "select_repos",
]
