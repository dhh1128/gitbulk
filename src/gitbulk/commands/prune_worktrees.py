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
from gitbulk.invariants import InvariantContext, get, run_chain
from gitbulk.invariants.base import Invariant, InvariantKind
from gitbulk.locks import (
    LockTimeoutError,
    repo_lock,
    run_state_lock,
    sentinel_lock,
)
from gitbulk.runstate import RunState
from gitbulk.util.progress import Progress
from gitbulk.util.style import error_line, summary_line
from gitbulk import subcommands as subcommands_mod
from gitbulk.worktree import (
    WorktreeError,
    branch_unpushed_commit_count,
    delete_merged_local_branch,
    list_worktrees,
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


def _partition_chain(
    chain_names: Iterable[str],
) -> tuple[list[type[Invariant]], list[type[Invariant]]]:
    """Split the chain into UNIVERSAL vs per-repo gates (no PER_PR bucket —
    prune-worktrees operates on worktrees, not PRs)."""
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
            "defaults": _dc_to_dict(policy.defaults),
            "humans": _dc_to_dict(policy.humans),
            "bots": list(policy.bots),
            "repos": {
                slug: _dc_to_dict(ov) for slug, ov in policy.repos.items()
            },
            "worktree_root": str(policy.worktree_root),
        },
        "repos_txt": repos_text,
        "apply": bool(getattr(args, "apply", False)),
        "include_untracked": bool(getattr(args, "include_untracked", False)),
    }


def _dc_to_dict(obj) -> dict:
    from dataclasses import asdict

    out: dict = {}
    for k, v in asdict(obj).items():
        out[k] = list(v) if isinstance(v, tuple) else v
    return out


def _runid_from_run_dir(run_dir: Path) -> str:
    name = run_dir.name
    suffix = "-prune-worktrees"
    if name.endswith(suffix):
        return name[: -len(suffix)]
    head, _, _ = name.rpartition("-")
    return head


def _read_repos_text() -> str:
    return paths.repos_file().read_text()


# ─── per-worktree classification (the guardrails) ──────────────────────────


def _classify_worktree(
    gh,
    policy: Policy,
    slug: str,
    clone_path: Path,
    wt,
    open_heads: set[str],
    now: datetime,
    include_untracked: bool,
) -> dict:
    """Decide what to do with one LINKED worktree ``wt`` of ``slug``.

    Returns a dict with ``decision`` in {"delete", "skip"}. ``delete`` means
    every guardrail (node prnwt5nq) passed: not bare/locked/detached, not
    mid-op, clean (and no untracked unless allowed), branch not head of an
    open PR, branch HAS a merged/closed upstream PR past the grace period,
    and no unpushed commits (node prdls2nq). Caller never passes the main
    worktree here.
    """
    base = {"slug": slug, "path": str(wt.path), "branch": wt.branch}

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

    branch = wt.branch
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
    base["pr_number"] = pr.number
    base["pr_state"] = pr.state

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


# ─── public handler ────────────────────────────────────────────────────────


def prune_worktrees_handler(args: argparse.Namespace) -> int:
    policy = load_policy()
    code_root = Path(args.code_root).expanduser() if args.code_root else None
    repos, skipped_entries = load_repos(code_root=code_root)
    repos_text = _read_repos_text()

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
    results: list[dict] = []
    scan = Progress(len(passing_repos), prefix="scanning worktrees: ")
    for i, repo in enumerate(passing_repos, start=1):
        scan.update(i, repo.slug)
        slug = repo.slug
        try:
            # repo_lock(slug, shared): the worktree-list read is git against
            # the clone, so it serializes against another run's worktree
            # removal on the SAME repo (node rsclk7nq #6). Released before the
            # network call below.
            with repo_lock(
                slug, "shared", timeout=_LOCK_TIMEOUT_SECONDS,
                subcommand="prune-worktrees",
            ):
                worktrees = list_worktrees(repo.local_path)
            open_prs = gh.my_open_prs([slug], author=None).get(slug, [])
        except (WorktreeError, GHError) as e:
            rs.record_error(
                f"worktree scan failed for {slug}: {e}",
                level="ERROR", context={"slug": slug, "error": str(e)},
            )
            results.append(
                {"slug": slug, "path": None, "branch": None,
                 "decision": "error", "reason": f"scan failed: {e}"}
            )
            continue
        open_heads = {pr.head_ref for pr in open_prs}
        for wt in worktrees:
            if wt.is_main:
                continue  # never the primary working tree
            results.append(
                _classify_worktree(
                    gh, policy, slug, repo.local_path, wt, open_heads, now,
                    include_untracked,
                )
            )
    scan.done()

    delete_candidates = [r for r in results if r["decision"] == "delete"]

    if not args.apply:
        return _finish_dry_run(
            rs, policy, repos, passing_repos, skipped_repos, results,
            delete_candidates, skip_list, skipped_entries, filter_line,
        )

    # ── --apply: remove each candidate worktree, then its merged branch ──
    failure_count = 0
    removed_count = 0
    for cand in delete_candidates:
        slug = cand["slug"]
        clone = clone_by_slug[slug]
        wt_path = Path(cand["path"])
        try:
            # repo_lock(slug, exclusive): both git mutations on the clone (the
            # worktree removal and the merged-branch delete) run under one
            # acquisition so another gitbulk run never touches this clone mid-
            # operation (node rsclk7nq #6).
            with repo_lock(
                slug, "exclusive", timeout=_LOCK_TIMEOUT_SECONDS,
                subcommand="prune-worktrees",
            ):
                remove_linked_worktree(clone, wt_path)
                cand["removed"] = True
                removed_count += 1
                branch_deleted = delete_merged_local_branch(clone, cand["branch"])
        except WorktreeError as e:
            failure_count += 1
            cand["error"] = str(e)
            rs.record_error(
                f"remove_linked_worktree failed for {slug} {wt_path}: {e}",
                level="ERROR",
                context={"slug": slug, "path": str(wt_path), "error": str(e)},
            )
            continue
        cand["branch_deleted"] = branch_deleted
        rs.record_error(
            f"removed worktree {wt_path} ({cand['branch']}); "
            f"branch {'deleted' if branch_deleted else 'kept (not fully merged)'}",
            level="WARNING",
            context={
                "slug": slug, "path": str(wt_path), "branch": cand["branch"],
                "branch_deleted": branch_deleted, "action": "removed-worktree",
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
        f"removed {removed_count} of {len(delete_candidates)} worktrees; "
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
    summary_text = (
        f"dry-run: {len(delete_candidates)} worktrees would be removed; "
        f"{len(skipped_repos)} repos skipped; "
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
                else:
                    bd = r.get("branch_deleted")
                    status = (
                        " — removed; branch "
                        + ("deleted" if bd else "kept")
                    )
            lines.append(
                f"- `{r['slug']}` `{r['path']}` ({r['branch']}) "
                f"[{r['reason']}]{status}"
            )
        lines.append("")
    if skips:
        lines.append("## Kept (guardrail)")
        for r in skips:
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
