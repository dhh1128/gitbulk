"""``gitbulk prune-worktrees`` — remove local LINKED worktrees whose branch's
only PRs are merged or closed (this.i node ``prnwt5nq``).

Clone-touching: it reads ``git worktree list`` from each clone and removes
linked worktrees with ``git worktree remove`` (no --force), then deletes the
now-orphaned local branch IFF it is fully merged. The one blessed local
mutation per node ``wtrm6kpq``; the primary working tree is never touched.

Pipeline mirrors prune-branches; the per-worktree guardrails live in
:func:`_classify_worktree`. Bias to skip-with-reason on any ambiguity.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from gitbulk import paths, sentinel
from gitbulk.config.policy import Policy, load_policy, policy_for
from gitbulk.config.repos import RepoEntry, SkippedEntry, load_repos
from gitbulk.default_branch_cache import prime_default_branches
from gitbulk.filters import (
    filter_summary_line,
    resolve_filter_spec,
    select_repos,
)
from gitbulk.gh import GHError, ProductionGHClient
from gitbulk.org_members_cache import (
    OrgMembersRefreshError,
    ensure_org_members_fresh,
)
from gitbulk.commands._common import dc_to_dict, read_repos_text
from gitbulk.invariants import InvariantContext, get, run_chain
from gitbulk.invariants.base import Invariant, InvariantKind
from gitbulk.locks import (
    LockTimeoutError,
    repo_lock,
    run_state_lock,
    sentinel_lock,
)
from gitbulk.runstate import RunState
from gitbulk.util.parallel import parallel_map
from gitbulk.util.progress import Progress
from gitbulk.util.style import error_line, summary_line
from gitbulk import subcommands as subcommands_mod
from gitbulk.worktree import (
    WorktreeError,
    branch_unpushed_commit_count,
    delete_merged_local_branch,
    list_worktrees,
    local_branch_upstreams,
    remove_linked_worktree,
    worktree_change_summary,
    worktree_in_progress_op,
)

EXIT_OK = 0
EXIT_STRUCTURAL_FAILURE = 1
EXIT_ATTENTION_NEEDED = 2
EXIT_INVARIANT_SKIPPED = 3
EXIT_OVERRIDES_APPLIED = 4

_LOCK_TIMEOUT_SECONDS: float = 1800.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_concurrency(args: argparse.Namespace, policy: Policy) -> int:
    """The scan worker count: ``--concurrency`` if given, else the policy
    default (``prune_scan_concurrency``, shared with prune-branches). Floored
    at 1 so a bogus 0/negative degrades to a sequential scan (node prnwpf9k)."""
    val = getattr(args, "concurrency", None)
    if val is None:
        val = policy.defaults.prune_scan_concurrency
    return max(1, int(val))


def _prune_local_enabled(args: argparse.Namespace) -> bool:
    """Local-branch sweep is DEFAULT-ON (node prnwlb7q); ``--no-prune-local-
    branches`` opts out for a worktrees-only run."""
    return not bool(getattr(args, "no_prune_local_branches", False))


def _partition_chain(
    chain_names: Iterable[str],
) -> tuple[list[type[Invariant]], list[type[Invariant]]]:
    """Split the chain into UNIVERSAL vs per-repo gates (no PER_PR bucket —
    prune-worktrees operates on worktrees, not PRs).

    Kept local on purpose: this is a two-bucket variant, distinct from the
    three-bucket :func:`gitbulk.commands._common.partition_chain` used by the
    PR-oriented commands (MNT-F2). Do not collapse the two.
    """
    universal: list[type[Invariant]] = []
    per_repo: list[type[Invariant]] = []
    for name in chain_names:
        cls = get(name)
        if cls.kind == InvariantKind.UNIVERSAL:
            universal.append(cls)
        else:
            per_repo.append(cls)
    return universal, per_repo


def _config_snapshot(
    policy: Policy, repos_text: str, args: argparse.Namespace
) -> dict:
    return {
        "policy": {
            "defaults": dc_to_dict(policy.defaults),
            "humans": dc_to_dict(policy.humans),
            "bots": list(policy.bots),
            "repos": {
                slug: dc_to_dict(ov) for slug, ov in policy.repos.items()
            },
            "worktree_root": str(policy.worktree_root),
        },
        "repos_txt": repos_text,
        "apply": bool(getattr(args, "apply", False)),
        "include_untracked": bool(getattr(args, "include_untracked", False)),
        "concurrency": _resolve_concurrency(args, policy),
        "prune_local_branches": _prune_local_enabled(args),
    }


def _runid_from_run_dir(run_dir: Path) -> str:
    name = run_dir.name
    suffix = "-prune-worktrees"
    if name.endswith(suffix):
        return name[: -len(suffix)]
    head, _, _ = name.rpartition("-")
    return head


# ─── per-worktree classification (the guardrails) ──────────────────────────


def _classify_branch_by_pr(
    gh,
    policy: Policy,
    slug: str,
    clone_path: Path,
    branch: str,
    open_heads: set[str],
    now: datetime,
    base: dict,
    *,
    default_branch: str | None,
    protected_upstreams: frozenset[str] | None,
    upstream: str | None,
) -> dict:
    """The protection/PR/grace/data-loss guardrails shared by a worktree's
    branch and a worktree-less local branch (nodes prnwt5nq, prnwlb7q,
    prdls2nq, prgrc3kp).

    ``base`` carries the row identity (slug, kind, path, branch). The
    protection guard is REMOTE-driven (never name-based): ``upstream`` is the
    remote branch this local branch tracks, and the branch is kept when that
    upstream is the repo's default branch OR is protected on GitHub. If
    ``protected_upstreams`` is ``None`` the remote protection could not be
    fetched, so we refuse (bias to safe). Returns a ``delete`` only when, after
    those gates, the branch is not the head of an open PR, HAS a merged/closed
    upstream PR past the grace period, and has no unpushed commits.
    """
    if protected_upstreams is None:
        return {
            **base, "decision": "skip",
            "reason": "could not verify remote branch protection",
        }
    if upstream is not None and (
        upstream == default_branch or upstream in protected_upstreams
    ):
        return {
            **base, "decision": "skip",
            "reason": f"tracks protected/default upstream '{upstream}'",
        }

    if branch in open_heads:
        return {**base, "decision": "skip", "reason": "branch is head of an open PR"}

    try:
        closed = gh.closed_prs_for_head(slug, branch)
    except GHError as e:
        return {**base, "decision": "skip", "reason": f"could not list closed PRs: {e}"}
    upstream_closed = [c for c in closed if c.head_repo_slug == slug]
    if not upstream_closed:
        return {
            **base, "decision": "skip",
            "reason": "no merged/closed PR for this branch on the upstream",
        }
    pr = upstream_closed[0]
    base = {**base, "pr_number": pr.number, "pr_state": pr.state}

    grace = policy_for(policy, slug).prune_min_age_days
    age_days = (now - pr.closed_at).days
    if age_days < grace:
        return {
            **base, "decision": "skip",
            "reason": (
                f"PR #{pr.number} {pr.state.lower()} {age_days}d ago "
                f"(< {grace}d grace period)"
            ),
        }

    try:
        unpushed = branch_unpushed_commit_count(clone_path, branch)
    except WorktreeError as e:
        return {**base, "decision": "skip", "reason": f"could not verify commits: {e}"}
    if unpushed > 0:
        return {
            **base, "decision": "skip",
            "reason": f"{unpushed} unpushed commit(s) — would lose work",
        }
    return {**base, "decision": "delete", "reason": f"PR #{pr.number} {pr.state.lower()}"}


def _classify_worktree(
    gh,
    policy: Policy,
    slug: str,
    clone_path: Path,
    wt,
    open_heads: set[str],
    now: datetime,
    include_untracked: bool,
    *,
    default_branch: str | None = None,
    protected_upstreams: frozenset[str] | None = frozenset(),
    upstream: str | None = None,
) -> dict:
    """Decide what to do with one LINKED worktree ``wt`` of ``slug``.

    Returns a dict with ``decision`` in {"delete", "skip"}. ``delete`` means
    every guardrail (node prnwt5nq) passed: not bare/locked/detached, not
    mid-op, clean (and no untracked unless allowed), then the shared
    protection/PR/grace/data-loss gates in :func:`_classify_branch_by_pr`.
    Caller never passes the main worktree here.
    """
    base = {"slug": slug, "kind": "worktree", "path": str(wt.path), "branch": wt.branch}

    if wt.is_bare:
        return {**base, "decision": "skip", "reason": "bare worktree"}
    if wt.is_locked:
        return {**base, "decision": "skip", "reason": "worktree is locked"}
    if wt.is_detached or wt.branch is None:
        return {
            **base, "decision": "skip",
            "reason": "detached HEAD (no branch/PR association)",
        }

    op = worktree_in_progress_op(wt.path)
    if op:
        return {**base, "decision": "skip", "reason": f"{op} in progress"}

    tracked, untracked, conflicted = worktree_change_summary(wt.path)
    if conflicted:
        return {**base, "decision": "skip", "reason": "merge/rebase conflict present"}
    if tracked:
        return {**base, "decision": "skip", "reason": "uncommitted changes"}
    if untracked and not include_untracked:
        return {
            **base, "decision": "skip",
            "reason": "untracked files present (use --include-untracked)",
        }

    return _classify_branch_by_pr(
        gh, policy, slug, clone_path, wt.branch, open_heads, now, base,
        default_branch=default_branch, protected_upstreams=protected_upstreams,
        upstream=upstream,
    )


def _classify_local_branch(
    gh,
    policy: Policy,
    slug: str,
    clone_path: Path,
    branch: str,
    open_heads: set[str],
    now: datetime,
    *,
    default_branch: str | None = None,
    protected_upstreams: frozenset[str] | None = frozenset(),
    upstream: str | None = None,
) -> dict:
    """Decide what to do with one LOCAL branch of ``slug`` that is NOT checked
    out in any worktree (node prnwlb7q).

    Applies the same protection/PR/grace/data-loss gates as a worktree's branch
    (there is no working-tree state to guard, since the branch has no worktree).
    A ``delete`` is acted on via ``git branch -d`` (merged-only), so an unmerged
    branch is kept even if this returns delete.
    """
    base = {"slug": slug, "kind": "branch", "path": None, "branch": branch}
    return _classify_branch_by_pr(
        gh, policy, slug, clone_path, branch, open_heads, now, base,
        default_branch=default_branch, protected_upstreams=protected_upstreams,
        upstream=upstream,
    )


# ─── parallel scan (node prnwpf9k) ─────────────────────────────────────────


def _scan_repo(repo: RepoEntry, prune_local: bool) -> dict:
    """Pass A worker: read one clone's worktrees and (always) its local
    branches' upstreams under a SHARED repo lock — pure-local git, no network.
    Returns the linked (non-main) worktrees, the worktree-less local branches
    to sweep (when enabled), and an ``upstreams`` map (every local branch ->
    the remote branch it tracks, or None) used by the remote-driven protection
    guard. ``error`` is set on git failure (the caller surfaces it as a
    per-repo error row, never sinking the run). Runs in a worker thread; each
    call opens its own lock fd so shared reads across threads don't contend
    (node rsclk7nq res #6)."""
    slug = repo.slug
    try:
        with repo_lock(
            slug, "shared", timeout=_LOCK_TIMEOUT_SECONDS,
            subcommand="prune-worktrees",
        ):
            worktrees = list_worktrees(repo.local_path)
            # Upstreams are needed for BOTH worktree branches and free
            # branches (protection guard), so always read them.
            upstream_pairs = local_branch_upstreams(repo.local_path)
    except WorktreeError as e:
        return {"slug": slug, "error": str(e)}
    upstreams = dict(upstream_pairs)
    checked_out = {wt.branch for wt in worktrees if wt.branch}
    free_branches = (
        [name for name, _up in upstream_pairs if name not in checked_out]
        if prune_local else []
    )
    return {
        "slug": slug,
        "error": None,
        "repo": repo,
        "linked": [wt for wt in worktrees if not wt.is_main],
        "free_branches": free_branches,
        "upstreams": upstreams,
    }


# ─── public handler ────────────────────────────────────────────────────────


def prune_worktrees_handler(args: argparse.Namespace) -> int:
    policy = load_policy()
    code_root = Path(args.code_root).expanduser() if args.code_root else None
    repos, skipped_entries = load_repos(code_root=code_root)
    repos_text = read_repos_text()

    spec = resolve_filter_spec(args, policy)
    repos, repos_excluded = select_repos(repos, spec)

    # Resource-scoped locking (node rsclk7nq): no global lock. Caches self-lock
    # in their helpers; the per-repo clone reads/removes take repo_lock(slug)
    # (shared for the worktree-list read, exclusive for the removals); the
    # terminal writes take sentinel_lock + run_state_lock("prune-worktrees").
    try:
        return _run_under_lock(
            args, policy, repos, repos_text, skipped_entries,
            spec, repos_excluded,
        )
    except LockTimeoutError as e:
        print(
            error_line(f"gitbulk prune-worktrees: timed out acquiring lock: {e}"),
            file=sys.stderr,
        )
        return EXIT_STRUCTURAL_FAILURE


def _run_under_lock(
    args: argparse.Namespace,
    policy: Policy,
    repos: list[RepoEntry],
    repos_text: str,
    skipped_entries: list[SkippedEntry],
    spec,
    repos_excluded: int,
) -> int:
    config_snapshot = _config_snapshot(policy, repos_text, args)
    rs = RunState.begin(
        "prune-worktrees", argv=list(sys.argv), config_snapshot=config_snapshot
    )
    include_untracked = bool(getattr(args, "include_untracked", False))

    gh = ProductionGHClient()
    ctx_base = InvariantContext(policy=policy, runstate=rs, gh=gh)

    try:
        ensure_org_members_fresh(
            gh, policy, force=bool(getattr(args, "refresh_org_members", False))
        )
    except OrgMembersRefreshError as e:
        rs.record_error(str(e))
        return _finish(
            rs, EXIT_STRUCTURAL_FAILURE, summary=str(e), policy=policy,
            attention=False, all_repos=repos, passing_repos=[],
            skipped_repos=[], results=[], apply=bool(args.apply),
            skipped_entries=skipped_entries, filter_line=None,
        )

    sub = subcommands_mod.by_name("prune-worktrees")
    universal, per_repo = _partition_chain(sub.invariant_chain)

    skip_list = list(args.skip_check or [])
    skip_set = frozenset(skip_list)
    if skip_list:
        rs.record_error(
            f"--skip-check applied: {sorted(skip_list)}",
            level="WARNING",
            context={"skipped_invariants": sorted(skip_list)},
        )

    universal_result = run_chain(
        universal, ctx_base, skip_set=skip_set, target="global"
    )
    if not universal_result.passed:
        return _finish(
            rs, EXIT_STRUCTURAL_FAILURE,
            summary=f"universal preflight failed: {universal_result.fail_reason}",
            policy=policy, attention=False, all_repos=repos, passing_repos=[],
            skipped_repos=[], results=[], apply=bool(args.apply),
            skipped_entries=skipped_entries, filter_line=None,
        )

    skipped_repos: list[tuple[str, str]] = []
    passing_repos: list[RepoEntry] = []
    prefetch_prog = Progress(len(repos), prefix="prefetching default branches: ")
    prime_default_branches(
        gh, [r.slug for r in repos],
        on_progress=lambda done, total: prefetch_prog.update(done),
    )
    prefetch_prog.done()
    progress = Progress(len(repos), prefix="per-repo checks: ")
    for i, repo in enumerate(repos, start=1):
        progress.update(i, repo.slug)
        ctx_repo = replace(ctx_base, repo=repo)
        r = run_chain(per_repo, ctx_repo, skip_set=skip_set, target=repo.slug)
        if not r.passed:
            progress.done()
            return _finish(
                rs, EXIT_STRUCTURAL_FAILURE,
                summary=f"per-repo invariant failed on {repo.slug}: {r.fail_reason}",
                policy=policy, attention=False, all_repos=repos,
                passing_repos=passing_repos, skipped_repos=skipped_repos,
                results=[], apply=bool(args.apply),
                skipped_entries=skipped_entries, filter_line=None,
            )
        intrinsic_skips = [(n, reason) for n, reason in r.skips if n not in skip_set]
        if intrinsic_skips:
            skipped_repos.append(
                (repo.slug, "; ".join(reason for _, reason in intrinsic_skips))
            )
        else:
            passing_repos.append(repo)
    progress.done()

    now = _utc_now()
    filter_line = filter_summary_line(spec, repos_excluded, 0)
    clone_by_slug = {r.slug: r.local_path for r in passing_repos}
    concurrency = _resolve_concurrency(args, policy)
    prune_local = _prune_local_enabled(args)

    # One BATCHED open-PR fetch for the whole scope (node prnwpf9k): the gh
    # client chunks repo: qualifiers internally (~50/search), so this is a
    # handful of searches instead of one per repo. author=None — anyone's open
    # PR pins a branch, not just mine. A whole-scope failure is structural (we
    # can't reason about open-PR heads for any repo), so it aborts rather than
    # mis-classify every branch as having no open PR.
    try:
        open_prs_by_slug = gh.my_open_prs(
            [r.slug for r in passing_repos], author=None
        )
    except GHError as e:
        rs.record_error(
            f"open-PR fetch failed: {e}", level="ERROR", context={"error": str(e)}
        )
        return _finish(
            rs, EXIT_STRUCTURAL_FAILURE, summary=f"open-PR fetch failed: {e}",
            policy=policy, attention=False, all_repos=repos,
            passing_repos=passing_repos, skipped_repos=skipped_repos,
            results=[], apply=bool(args.apply),
            skipped_entries=skipped_entries, filter_line=filter_line,
        )
    open_heads_by_slug = {
        slug: {pr.head_ref for pr in prs} for slug, prs in open_prs_by_slug.items()
    }

    # Pass A (parallel): read each clone's worktrees + local branches.
    prog_a = Progress(len(passing_repos), prefix="scanning clones: ")
    repo_scans = parallel_map(
        lambda repo: _scan_repo(repo, prune_local),
        passing_repos,
        concurrency=concurrency,
        on_progress=lambda done, total: prog_a.update(done),
    )
    prog_a.done()

    # Flatten Pass A into a single work queue (worktrees + worktree-less
    # branches) so Pass B's per-item closed_prs_for_head runs at full width
    # regardless of how the candidates distribute across repos.
    results: list[dict] = []
    deep_items: list[tuple] = []
    upstreams_by_slug: dict[str, dict[str, str | None]] = {}
    for scan in repo_scans:
        if scan["error"] is not None:
            rs.record_error(
                f"worktree scan failed for {scan['slug']}: {scan['error']}",
                level="ERROR",
                context={"slug": scan["slug"], "error": scan["error"]},
            )
            results.append(
                {"slug": scan["slug"], "kind": None, "path": None, "branch": None,
                 "decision": "error", "reason": f"scan failed: {scan['error']}"}
            )
            continue
        repo = scan["repo"]
        upstreams_by_slug[repo.slug] = scan["upstreams"]
        for wt in scan["linked"]:
            deep_items.append(("worktree", repo, wt))
        for branch in scan["free_branches"]:
            deep_items.append(("branch", repo, branch))

    # Remote-driven protection (node prnwlb7q): for every repo with candidates,
    # learn its default branch (cached from the prefetch) and its protected
    # branch set (one list_branches call). A branch is kept when its UPSTREAM is
    # default/protected on GitHub — never decided by local name. A fetch failure
    # leaves protected=None, which the classifier treats as "refuse" (safe).
    candidate_slugs = sorted({repo.slug for _kind, repo, _obj in deep_items})

    def _fetch_protection(slug: str) -> tuple:
        try:
            default = gh.default_branch(slug)
            protected = frozenset(
                b.name for b in gh.list_branches(slug) if b.protected
            )
        except GHError as e:
            return (slug, None, None, str(e))
        return (slug, default, protected, None)

    prog_p = Progress(len(candidate_slugs), prefix="reading branch protection: ")
    default_by_slug: dict[str, str | None] = {}
    protected_by_slug: dict[str, frozenset[str] | None] = {}
    for slug, default, protected, err in parallel_map(
        _fetch_protection, candidate_slugs, concurrency=concurrency,
        on_progress=lambda done, total: prog_p.update(done),
    ):
        default_by_slug[slug] = default
        protected_by_slug[slug] = protected
        if err is not None:
            rs.record_error(
                f"branch-protection fetch failed for {slug}: {err}",
                level="WARNING", context={"slug": slug, "error": err},
            )
    prog_p.done()

    def _classify_item(item: tuple) -> dict:
        kind, repo, obj = item
        slug = repo.slug
        open_heads = open_heads_by_slug.get(slug, set())
        default_branch = default_by_slug.get(slug)
        protected = protected_by_slug.get(slug)
        ups = upstreams_by_slug.get(slug, {})
        if kind == "worktree":
            return _classify_worktree(
                gh, policy, slug, repo.local_path, obj, open_heads, now,
                include_untracked, default_branch=default_branch,
                protected_upstreams=protected, upstream=ups.get(obj.branch),
            )
        return _classify_local_branch(
            gh, policy, slug, repo.local_path, obj, open_heads, now,
            default_branch=default_branch, protected_upstreams=protected,
            upstream=ups.get(obj),
        )

    prog_b = Progress(len(deep_items), prefix="classifying: ")
    results.extend(
        parallel_map(
            _classify_item, deep_items, concurrency=concurrency,
            on_progress=lambda done, total: prog_b.update(done),
        )
    )
    prog_b.done()

    delete_candidates = [r for r in results if r["decision"] == "delete"]

    if not args.apply:
        return _finish_dry_run(
            rs, policy, repos, passing_repos, skipped_repos, results,
            delete_candidates, skip_list, skipped_entries, filter_line,
        )

    # ── --apply: remove each candidate worktree (then its merged branch), and
    # delete each worktree-less candidate branch (node prnwlb7q). ──
    failure_count = 0
    removed_count = 0  # worktrees removed
    branch_count = 0   # standalone local branches deleted
    wt_cands = sum(1 for c in delete_candidates if c["kind"] == "worktree")
    br_cands = len(delete_candidates) - wt_cands
    for cand in delete_candidates:
        slug = cand["slug"]
        clone = clone_by_slug[slug]
        try:
            # repo_lock(slug, exclusive): all git mutations on the clone (the
            # worktree removal and any branch delete) run under one acquisition
            # so another gitbulk run never touches this clone mid-operation
            # (node rsclk7nq #6).
            with repo_lock(
                slug, "exclusive", timeout=_LOCK_TIMEOUT_SECONDS,
                subcommand="prune-worktrees",
            ):
                if cand["kind"] == "worktree":
                    remove_linked_worktree(clone, Path(cand["path"]))
                    cand["removed"] = True
                    removed_count += 1
                    branch_deleted = delete_merged_local_branch(clone, cand["branch"])
                else:  # bare local branch — no worktree to remove
                    branch_deleted = delete_merged_local_branch(clone, cand["branch"])
                    if branch_deleted:
                        branch_count += 1
        except WorktreeError as e:
            failure_count += 1
            cand["error"] = str(e)
            rs.record_error(
                f"prune failed for {slug} ({cand['kind']} {cand['branch']}): {e}",
                level="ERROR",
                context={"slug": slug, "kind": cand["kind"],
                         "branch": cand["branch"], "error": str(e)},
            )
            continue
        cand["branch_deleted"] = branch_deleted
        if cand["kind"] == "worktree":
            msg = (
                f"removed worktree {cand['path']} ({cand['branch']}); "
                f"branch {'deleted' if branch_deleted else 'kept (not fully merged)'}"
            )
            action = "removed-worktree"
        else:
            msg = (
                f"local branch {cand['branch']} "
                f"{'deleted' if branch_deleted else 'kept (not fully merged)'}"
            )
            # Reflect the ACTUAL outcome: git branch -d may refuse (unmerged),
            # leaving the branch in place, so the audit action must not claim a
            # deletion that did not happen (Copilot PR #17 review).
            action = "deleted-branch" if branch_deleted else "kept-branch"
        rs.record_error(
            msg, level="WARNING",
            context={
                "slug": slug, "kind": cand["kind"], "path": cand["path"],
                "branch": cand["branch"], "branch_deleted": branch_deleted,
                "action": action,
            },
        )

    for repo in passing_repos:
        _record_repo_state(rs, repo.slug, results)

    if failure_count > 0:
        exit_code, attention = EXIT_ATTENTION_NEEDED, True
    elif skipped_repos or skipped_entries:
        exit_code, attention = EXIT_INVARIANT_SKIPPED, True
    elif skip_list:
        exit_code, attention = EXIT_OVERRIDES_APPLIED, False
    else:
        exit_code, attention = EXIT_OK, False

    summary_text = (
        f"removed {removed_count} of {wt_cands} worktrees; "
        f"deleted {branch_count} of {br_cands} local branches; "
        f"{failure_count} failed; {len(skipped_repos)} repos skipped; "
        f"{len(skipped_entries)} entries skipped"
        + (f"; {filter_line}" if filter_line else "")
    )
    rs.write_summary(
        _build_summary_md(
            policy, all_repos=repos, passing_repos=passing_repos,
            skipped_repos=skipped_repos, results=results, apply=True,
            skipped_entries=skipped_entries, filter_line=filter_line,
        )
    )
    return _finish(
        rs, exit_code, summary=summary_text, policy=policy, attention=attention,
        all_repos=repos, passing_repos=passing_repos, skipped_repos=skipped_repos,
        results=results, apply=True, skip_writing_summary=True,
        skipped_entries=skipped_entries, filter_line=filter_line,
    )


def _finish_dry_run(
    rs, policy, repos, passing_repos, skipped_repos, results,
    delete_candidates, skip_list, skipped_entries, filter_line,
) -> int:
    rs.write_summary(
        _build_summary_md(
            policy, all_repos=repos, passing_repos=passing_repos,
            skipped_repos=skipped_repos, results=results, apply=False,
            skipped_entries=skipped_entries, filter_line=filter_line,
        )
    )
    if skipped_repos or skipped_entries:
        exit_code, attention = EXIT_INVARIANT_SKIPPED, True
    elif skip_list:
        exit_code, attention = EXIT_OVERRIDES_APPLIED, False
    else:
        exit_code, attention = EXIT_OK, False
    for repo in passing_repos:
        _record_repo_state(rs, repo.slug, results)
    wt_cands = sum(1 for r in delete_candidates if r["kind"] == "worktree")
    br_cands = len(delete_candidates) - wt_cands
    summary_text = (
        f"dry-run: {wt_cands} worktrees + {br_cands} local branches would be "
        f"removed; {len(skipped_repos)} repos skipped; "
        f"{len(skipped_entries)} entries skipped"
        + (f"; {filter_line}" if filter_line else "")
    )
    return _finish(
        rs, exit_code, summary=summary_text, policy=policy, attention=attention,
        all_repos=repos, passing_repos=passing_repos, skipped_repos=skipped_repos,
        results=results, apply=False, skip_writing_summary=True,
        skipped_entries=skipped_entries, filter_line=filter_line,
    )


def _build_summary_md(
    policy: Policy,
    *,
    all_repos: list[RepoEntry],
    passing_repos: list[RepoEntry],
    skipped_repos: list[tuple[str, str]],
    results: list[dict],
    apply: bool,
    skipped_entries: list[SkippedEntry] | None = None,
    filter_line: str | None = None,
) -> str:
    lines: list[str] = ["# gitbulk prune-worktrees", ""]
    lines.append(f"Mode: **{'APPLY' if apply else 'DRY-RUN'}**")
    if filter_line:
        lines.append(filter_line)
    deletes = [r for r in results if r["decision"] == "delete"]
    skips = [r for r in results if r["decision"] == "skip"]
    errors = [r for r in results if r["decision"] == "error"]
    lines.append(
        f"Configured repos: {len(all_repos)}  Reachable: {len(passing_repos)}  "
        f"Skipped repos: {len(skipped_repos)}  Remove candidates: {len(deletes)}"
    )
    lines.append("")

    if skipped_repos:
        lines.append("## Skipped repos")
        for slug, reason in skipped_repos:
            lines.append(f"- `{slug}` — {reason}")
        lines.append("")
    if skipped_entries:
        lines.append("## Skipped repos.txt entries")
        for entry in skipped_entries:
            lines.append(f"- line {entry.lineno} (`{entry.raw}`): {entry.reason}")
        lines.append("")

    if deletes:
        lines.append("## Removed" if apply else "## Would remove")
        for r in deletes:
            status = ""
            if apply:
                if "error" in r:
                    status = " — FAILED: " + r["error"]
                elif r.get("kind") == "branch":
                    status = " — branch " + (
                        "deleted" if r.get("branch_deleted") else "kept"
                    )
                else:
                    status = " — removed; branch " + (
                        "deleted" if r.get("branch_deleted") else "kept"
                    )
            if r.get("kind") == "branch":
                lines.append(
                    f"- `{r['slug']}` branch `{r['branch']}` [{r['reason']}]{status}"
                )
            else:
                lines.append(
                    f"- `{r['slug']}` `{r['path']}` ({r['branch']}) "
                    f"[{r['reason']}]{status}"
                )
        lines.append("")
    if skips:
        lines.append("## Kept (guardrail)")
        for r in skips:
            if r.get("kind") == "branch":
                lines.append(f"- `{r['slug']}` branch `{r['branch']}` — {r['reason']}")
            else:
                lines.append(f"- `{r['slug']}` `{r['path']}` — {r['reason']}")
        lines.append("")
    if errors:
        lines.append("## Errors")
        for r in errors:
            lines.append(f"- `{r['slug']}` — {r['reason']}")
        lines.append("")
    if not deletes and not skips and not errors:
        lines.append("(no linked worktrees matched)")
        lines.append("")
    return "\n".join(lines)


def _record_repo_state(rs: RunState, slug: str, results: list[dict]) -> None:
    rows = [r for r in results if r["slug"] == slug]
    if not rows:
        return
    rs.record_repo_state(
        slug,
        {
            "worktree_count": len(rows),
            "worktrees": [
                {k: v for k, v in r.items() if k != "slug"} for r in rows
            ],
        },
    )


def _finish(
    rs: RunState,
    exit_code: int,
    *,
    summary: str,
    policy: Policy,
    attention: bool,
    all_repos: list[RepoEntry],
    passing_repos: list[RepoEntry],
    skipped_repos: list[tuple[str, str]],
    results: list[dict],
    apply: bool,
    skipped_entries: list[SkippedEntry] | None = None,
    filter_line: str | None = None,
    skip_writing_summary: bool = False,
) -> int:
    if not skip_writing_summary:
        synth = _build_summary_md(
            policy, all_repos=all_repos, passing_repos=passing_repos,
            skipped_repos=skipped_repos, results=results, apply=apply,
            skipped_entries=skipped_entries, filter_line=filter_line,
        )
        rs.write_summary(f"# gitbulk prune-worktrees (FAILED)\n\n{summary}\n\n{synth}")
    if attention:
        runid = _runid_from_run_dir(rs.run_dir)
        with sentinel_lock(timeout=_LOCK_TIMEOUT_SECONDS, subcommand="prune-worktrees"):
            sentinel.set_attention(exit_code, "prune-worktrees", runid, summary)
    with run_state_lock(
        "prune-worktrees", "exclusive", timeout=_LOCK_TIMEOUT_SECONDS,
        subcommand="prune-worktrees",
    ):
        rs.complete(exit_code, retain_runs=policy.defaults.retain_runs)
    print(summary_line(
        f"gitbulk prune-worktrees: {summary}. View: gitbulk show prune-worktrees",
        exit_code,
    ))
    return exit_code


__all__ = [
    "EXIT_ATTENTION_NEEDED",
    "EXIT_INVARIANT_SKIPPED",
    "EXIT_OK",
    "EXIT_OVERRIDES_APPLIED",
    "EXIT_STRUCTURAL_FAILURE",
    "prune_worktrees_handler",
]
