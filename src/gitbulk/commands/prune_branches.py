"""``gitbulk prune-branches`` — delete remote branches whose only PRs are
merged or closed (this.i node ``prnbr4kq``).

Clone-free, like :mod:`merge`: it operates purely through the gh network
boundary and deletes via the git-ref API (node ``prdel4rq``), never
``git push --delete``. Pipeline mirrors close-stale:

  1. Load policy + repos; fleet-subset filter; acquire global EXCLUSIVE lock.
  2. RunState.begin + org-members refresh.
  3. UNIVERSAL preflight (Fail → exit 1); PER_REPO preflight (Skip drops repo).
  4. Per surviving repo: list branches, list ALL-author open PRs (the
     dependency index), and classify each branch via :func:`_classify_branch`.
  5. DRY-RUN gate: list what WOULD delete. ``--apply`` deletes each candidate
     via ``gh.delete_branch_ref`` (recording its SHA first for recovery).
  6. Exit-code ladder: failures → 2; skipped repos/entries → 3; --skip-check
     → 4; else 0. Successful cleanup is intentionally QUIET (no ATTENTION) —
     deleting merged-PR branches is routine, not something to surface.

A branch is deleted only when ALL guardrails pass (skip-with-reason on any
ambiguity or gh error). See :func:`_classify_branch` for the full list.
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

# Exit codes — duplicated (not imported from cli) so cli ↔ commands stays
# one-way, matching the other command modules.
EXIT_OK = 0
EXIT_STRUCTURAL_FAILURE = 1
EXIT_ATTENTION_NEEDED = 2
EXIT_INVARIANT_SKIPPED = 3
EXIT_OVERRIDES_APPLIED = 4

_LOCK_TIMEOUT_SECONDS: float = 1800.0


def _utc_now() -> datetime:
    """Indirection so tests can monkeypatch the clock."""
    return datetime.now(timezone.utc)


def _partition_chain(
    chain_names: Iterable[str],
) -> tuple[list[type[Invariant]], list[type[Invariant]]]:
    """Split the chain into UNIVERSAL vs per-repo gates.

    prune-branches has no PER_PR invariants (its unit of work is a branch,
    not a PR), so there is no third bucket — any non-UNIVERSAL invariant is
    a per-repo gate.
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
    }


def _dc_to_dict(obj) -> dict:
    from dataclasses import asdict

    out: dict = {}
    for k, v in asdict(obj).items():
        out[k] = list(v) if isinstance(v, tuple) else v
    return out


def _runid_from_run_dir(run_dir: Path) -> str:
    name = run_dir.name
    suffix = "-prune-branches"
    if name.endswith(suffix):
        return name[: -len(suffix)]
    head, _, _ = name.rpartition("-")
    return head


def _read_repos_text() -> str:
    return paths.repos_file().read_text()


# ─── per-branch classification (the guardrails) ────────────────────────────


def _classify_branch(
    gh,
    policy: Policy,
    slug: str,
    default_branch: str,
    branch,
    open_heads: set[str],
    open_bases: set[str],
    now: datetime,
) -> dict:
    """Decide what to do with one remote ``branch`` on ``slug``.

    Returns a dict with ``decision`` in {"delete", "skip"} and a human
    ``reason``. A ``delete`` decision means every guardrail (node prnbr4kq)
    passed:

      1. not the default branch
      2. not protected
      3. not the head of any OPEN PR
      4. not the base of any OPEN PR (stacked-PR dependency)
      5. there IS a closed/merged PR for it on the UPSTREAM (not a fork)
      6. that PR is older than the grace period (node prgrc3kp)
      7. no commit loss (node prdls2nq): the branch tip equals the merged
         PR's recorded head SHA, OR the branch is fully contained in the
         default branch

    Any gh error or inconclusive check biases to ``skip`` (fail safe).
    """
    name = branch.name
    base = {
        "slug": slug,
        "branch": name,
        "sha": branch.sha,
    }

    if name == default_branch:
        return {**base, "decision": "skip", "reason": "default branch"}
    if branch.protected:
        return {**base, "decision": "skip", "reason": "branch is protected"}
    if name in open_heads:
        return {**base, "decision": "skip", "reason": "head of an open PR"}
    if name in open_bases:
        return {
            **base,
            "decision": "skip",
            "reason": "base of an open PR (stacked dependency)",
        }

    try:
        closed = gh.closed_prs_for_head(slug, name)
    except GHError as e:
        return {
            **base,
            "decision": "skip",
            "reason": f"could not list closed PRs: {e}",
        }
    # Keep only PRs that originated on the upstream repo itself.
    upstream_closed = [c for c in closed if c.head_repo_slug == slug]
    if not upstream_closed:
        return {
            **base,
            "decision": "skip",
            "reason": "no merged/closed PR for this branch on the upstream",
        }
    # closed_prs_for_head returns newest first; the most recent close governs.
    pr = upstream_closed[0]
    base["pr_number"] = pr.number
    base["pr_state"] = pr.state

    grace = policy_for(policy, slug).prune_min_age_days
    age_days = (now - pr.closed_at).days
    if age_days < grace:
        return {
            **base,
            "decision": "skip",
            "reason": (
                f"PR #{pr.number} {pr.state.lower()} {age_days}d ago "
                f"(< {grace}d grace period)"
            ),
        }

    # Data-loss guard (node prdls2nq).
    if pr.merged and branch.sha == pr.head_sha:
        # Nothing was pushed after the merge — the merged PR fully accounts
        # for the branch tip.
        return {**base, "decision": "delete", "reason": f"merged PR #{pr.number}"}
    try:
        ahead = gh.branch_ahead_by(slug, default_branch, name)
    except GHError as e:
        return {
            **base,
            "decision": "skip",
            "reason": f"could not verify merge state: {e}",
        }
    if ahead == 0:
        return {
            **base,
            "decision": "delete",
            "reason": f"fully merged into {default_branch} (PR #{pr.number})",
        }
    return {
        **base,
        "decision": "skip",
        "reason": (
            f"{ahead} commit(s) not in {default_branch} — would lose work"
        ),
    }


# ─── public handler ────────────────────────────────────────────────────────


def prune_branches_handler(args: argparse.Namespace) -> int:
    policy = load_policy()
    code_root = Path(args.code_root).expanduser() if args.code_root else None
    repos, skipped_entries = load_repos(code_root=code_root)
    repos_text = _read_repos_text()

    spec = resolve_filter_spec(args, policy)
    repos, repos_excluded = select_repos(repos, spec)

    # Resource-scoped locking (node rsclk7nq): no global lock. The org/
    # default-branches caches self-lock in their helpers; each remote branch
    # delete takes repo_lock(slug) so two gitbulk runs never mutate the SAME
    # repo at once (different repos run in parallel); the terminal writes take
    # sentinel_lock + run_state_lock("prune-branches"). Any LockTimeoutError
    # surfaces as exit 1 (per tmlk5pq3).
    try:
        return _run_under_lock(
            args, policy, repos, repos_text, skipped_entries,
            spec, repos_excluded,
        )
    except LockTimeoutError as e:
        print(
            error_line(f"gitbulk prune-branches: timed out acquiring lock: {e}"),
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
        "prune-branches", argv=list(sys.argv), config_snapshot=config_snapshot
    )

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

    sub = subcommands_mod.by_name("prune-branches")
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
    # results: one record per branch we evaluated to a delete/skip decision.
    # (Branches with no closed PR are recorded as skips so the run is auditable.)
    results: list[dict] = []
    scan = Progress(len(passing_repos), prefix="scanning branches: ")
    for i, repo in enumerate(passing_repos, start=1):
        scan.update(i, repo.slug)
        slug = repo.slug
        try:
            default_branch = gh.default_branch(slug)
            branches = gh.list_branches(slug)
            open_prs = gh.my_open_prs([slug], author=None).get(slug, [])
        except GHError as e:
            rs.record_error(
                f"branch scan failed for {slug}: {e}",
                level="ERROR",
                context={"slug": slug, "error": str(e)},
            )
            results.append(
                {"slug": slug, "branch": None, "decision": "error",
                 "reason": f"scan failed: {e}"}
            )
            continue
        open_heads = {pr.head_ref for pr in open_prs}
        open_bases = {pr.base_ref for pr in open_prs}
        for branch in branches:
            decision = _classify_branch(
                gh, policy, slug, default_branch, branch,
                open_heads, open_bases, now,
            )
            # Only surface branches that are delete candidates OR that had a
            # closed/merged PR but were skipped for a safety reason. A branch
            # with simply no PR is not interesting to report.
            if decision["decision"] == "delete" or "pr_number" in decision:
                results.append(decision)
    scan.done()

    delete_candidates = [r for r in results if r["decision"] == "delete"]

    if not args.apply:
        return _finish_dry_run(
            rs, policy, repos, passing_repos, skipped_repos, results,
            delete_candidates, skip_list, skipped_entries, filter_line,
        )

    # ── --apply: delete each candidate ──
    failure_count = 0
    deleted_count = 0
    for cand in delete_candidates:
        slug = cand["slug"]
        branch = cand["branch"]
        try:
            # repo_lock(slug): serialize this remote mutation against any other
            # gitbulk run touching the SAME repo (node rsclk7nq resource #7).
            with repo_lock(
                slug, "exclusive", timeout=_LOCK_TIMEOUT_SECONDS,
                subcommand="prune-branches",
            ):
                gh.delete_branch_ref(slug, branch)
        except GHError as e:
            failure_count += 1
            cand["error"] = str(e)
            rs.record_error(
                f"delete_branch_ref failed for {slug}:{branch}: {e}",
                level="ERROR",
                context={"slug": slug, "branch": branch, "error": str(e)},
            )
            continue
        cand["deleted"] = True
        deleted_count += 1
        # AUDIT: record the SHA so a mistaken delete is restorable.
        rs.record_error(
            f"deleted {slug}:{branch} @ {cand['sha'][:7]} ({cand['reason']})",
            level="WARNING",
            context={
                "slug": slug, "branch": branch, "sha": cand["sha"],
                "pr": cand.get("pr_number"), "action": "deleted-branch",
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
        f"deleted {deleted_count} of {len(delete_candidates)} branches; "
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
        # Pending deletions in a dry run are NOT attention-worthy: routine
        # cleanup the user will confirm by re-running with --apply.
        exit_code, attention = EXIT_OK, False
    for repo in passing_repos:
        _record_repo_state(rs, repo.slug, results)
    summary_text = (
        f"dry-run: {len(delete_candidates)} branches would be deleted; "
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
    lines: list[str] = ["# gitbulk prune-branches", ""]
    lines.append(f"Mode: **{'APPLY' if apply else 'DRY-RUN'}**")
    if filter_line:
        lines.append(filter_line)
    deletes = [r for r in results if r["decision"] == "delete"]
    skips = [r for r in results if r["decision"] == "skip"]
    errors = [r for r in results if r["decision"] == "error"]
    lines.append(
        f"Configured repos: {len(all_repos)}  Reachable: {len(passing_repos)}  "
        f"Skipped repos: {len(skipped_repos)}  "
        f"Delete candidates: {len(deletes)}"
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
        lines.append("## Deleted" if apply else "## Would delete")
        for r in deletes:
            status = ""
            if apply:
                status = " — FAILED: " + r["error"] if "error" in r else " — deleted"
            lines.append(
                f"- `{r['slug']}` `{r['branch']}` @ {r['sha'][:7]} "
                f"({r['reason']}){status}"
            )
        lines.append("")
    if skips:
        lines.append("## Kept (guardrail)")
        for r in skips:
            lines.append(f"- `{r['slug']}` `{r['branch']}` — {r['reason']}")
        lines.append("")
    if errors:
        lines.append("## Errors")
        for r in errors:
            lines.append(f"- `{r['slug']}` — {r['reason']}")
        lines.append("")
    if not deletes and not skips and not errors:
        lines.append("(no branches matched)")
        lines.append("")
    return "\n".join(lines)


def _record_repo_state(rs: RunState, slug: str, results: list[dict]) -> None:
    rows = [r for r in results if r["slug"] == slug]
    if not rows:
        return
    rs.record_repo_state(
        slug,
        {
            "branch_count": len(rows),
            "branches": [
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
        rs.write_summary(f"# gitbulk prune-branches (FAILED)\n\n{summary}\n\n{synth}")
    if attention:
        runid = _runid_from_run_dir(rs.run_dir)
        with sentinel_lock(timeout=_LOCK_TIMEOUT_SECONDS, subcommand="prune-branches"):
            sentinel.set_attention(exit_code, "prune-branches", runid, summary)
    with run_state_lock(
        "prune-branches", "exclusive", timeout=_LOCK_TIMEOUT_SECONDS,
        subcommand="prune-branches",
    ):
        rs.complete(exit_code, retain_runs=policy.defaults.retain_runs)
    print(summary_line(
        f"gitbulk prune-branches: {summary}. View: gitbulk show prune-branches",
        exit_code,
    ))
    return exit_code


__all__ = [
    "EXIT_ATTENTION_NEEDED",
    "EXIT_INVARIANT_SKIPPED",
    "EXIT_OK",
    "EXIT_OVERRIDES_APPLIED",
    "EXIT_STRUCTURAL_FAILURE",
    "prune_branches_handler",
]
