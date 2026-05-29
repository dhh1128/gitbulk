"""``gitbulk rebase-pr`` — rebase behind/conflicting PRs onto their base.

Motivating case: PR A merges, PR B's branch goes BEHIND (base advanced)
or DIRTY (real conflict). rebase-pr brings B's head branch back onto the
current base in a disposable worktree and force-pushes the result.

Pipeline (mirrors merge / close-stale):

  1. Load policy + repos (clone-touching: needs the local clones).
  2. Acquire global EXCLUSIVE lock.
  3. RunState.begin("rebase-pr", ...).
  4. Prime default-branch cache (per-repo invariants consult it).
  5. UNIVERSAL preflight; Fail → exit 1.
  6. PER_REPO preflight (local.exists / remote_matches / default_branch_
     in_sync / github.reachable); Skip drops the repo.
  7. Coalesced my_open_prs; PER_PR chain ends with pr.needs_rebase, so
     only BEHIND/DIRTY PRs survive as eligible.
  8. DRY-RUN GATE: list what WOULD be rebased.
  9. (--apply) For each eligible PR:
       - create a detached-HEAD worktree at the PR's head SHA;
       - rebase onto origin/<base>;
       - CLEAN  → force-push-with-lease, remove the worktree;
       - CONFLICT → leave the worktree mid-rebase, write CONFLICT.md,
         report its path (node vp7n2krq);
       - ERROR  → rebase already aborted, remove the worktree, report.
 10. Exit code: failures → 2; skipped repos / repos.txt entries → 3;
     --skip-check → 4; else 0. Conflicts count as "needs your
     attention" → exit 2.

Force-push uses --force-with-lease against the PR's last-observed head
SHA, so an intervening push aborts rather than clobbers. Only the PR's
own head branch is pushed; the main clone is never touched (all git
work happens in the worktree).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path
from typing import Iterable

from gitbulk import paths, sentinel
from gitbulk.config.policy import Policy, load_policy, policy_for
from gitbulk.config.repos import RepoEntry, SkippedEntry, load_repos
from gitbulk.default_branch_cache import prime_default_branches
from gitbulk.filters import (
    apply_pr_filters,
    filter_summary_line,
    resolve_filter_spec,
    select_repos,
)
from gitbulk.gh import GHError, ProductionGHClient
from gitbulk.invariants import InvariantContext, get, run_chain
from gitbulk.invariants.base import Invariant, InvariantKind
from gitbulk.locks import LockTimeoutError, global_lock
from gitbulk.pr_info import PRInfo
from gitbulk.rebase import (
    RebaseError,
    RebaseStatus,
    force_push_with_lease,
    rebase_onto_base,
)
from gitbulk.runstate import RunState
from gitbulk.util.progress import Progress
from gitbulk.worktree import (
    WorktreeError,
    create_worktree,
    remove_worktree,
)
from gitbulk import subcommands as subcommands_mod

EXIT_OK = 0
EXIT_STRUCTURAL_FAILURE = 1
EXIT_ATTENTION_NEEDED = 2
EXIT_INVARIANT_SKIPPED = 3
EXIT_OVERRIDES_APPLIED = 4

#: Per node ``tmlk5pq3``: mutating subcommands get a 1800s lock budget.
_LOCK_TIMEOUT_SECONDS: float = 1800.0


# ─── Internal helpers ─────────────────────────────────────────────────────


def _partition_chain(
    chain_names: Iterable[str],
) -> tuple[list[type[Invariant]], list[type[Invariant]], list[type[Invariant]]]:
    universal: list[type[Invariant]] = []
    per_repo: list[type[Invariant]] = []
    per_pr: list[type[Invariant]] = []
    for name in chain_names:
        cls = get(name)
        if cls.kind == InvariantKind.UNIVERSAL:
            universal.append(cls)
        elif cls.kind == InvariantKind.PER_REPO:
            per_repo.append(cls)
        else:
            per_pr.append(cls)
    return universal, per_repo, per_pr


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
    suffix = "-rebase-pr"
    if name.endswith(suffix):
        return name[: -len(suffix)]
    head, _, _ = name.rpartition("-")
    return head


def _read_repos_text() -> str:
    return paths.repos_file().read_text()


def _write_conflict_marker(
    worktree_path: Path, slug: str, pr: PRInfo, base_ref: str, detail: str
) -> None:
    """Write CONFLICT.md into the preserved worktree (node vp7n2krq).

    Tells the user exactly how to finish the rebase by hand and how to
    clean up afterward.
    """
    marker = worktree_path / "CONFLICT.md"
    marker.write_text(
        f"# gitbulk rebase-pr — worktree preserved\n"
        f"\n"
        f"Repo: {slug}\n"
        f"PR: #{pr.number} {pr.title}\n"
        f"URL: {pr.url}\n"
        f"Head branch: {pr.head_ref}\n"
        f"Rebasing onto: origin/{base_ref}\n"
        f"Conflicted files: {detail}\n"
        f"\n"
        f"The rebase stopped at a conflict and this worktree was left\n"
        f"mid-rebase so you can resolve it. To finish:\n"
        f"\n"
        f"    cd {worktree_path}\n"
        f"    # resolve the conflicts, then:\n"
        f"    git add -A\n"
        f"    git rebase --continue\n"
        f"    git push --force-with-lease={pr.head_ref}:{pr.head_sha} "
        f"origin HEAD:{pr.head_ref}\n"
        f"\n"
        f"Then remove the worktree:\n"
        f"\n"
        f"    git worktree remove --force {worktree_path}\n"
    )


# ─── Public handler ───────────────────────────────────────────────────────


def rebase_pr_handler(args: argparse.Namespace) -> int:
    policy = load_policy()
    code_root = Path(args.code_root).expanduser() if args.code_root else None
    repos, skipped_entries = load_repos(code_root=code_root)
    repos_text = _read_repos_text()

    # Fleet-subset filter (node flt7arg2). rebase-pr can only force-push
    # branches you own, so it REFUSES the --author dimension outright
    # (the per-command author veto): there's no safe way to rebase
    # someone else's PR. Other dimensions (org/repo/base/mergeable_state)
    # are honored normally.
    spec = resolve_filter_spec(args, policy)
    if spec.authors:
        from gitbulk.config.repos import ConfigError

        raise ConfigError(
            "rebase-pr does not support --author: it can only rebase and "
            "force-push your own PRs. Drop --author (it already targets "
            "yours)."
        )
    repos, repos_excluded = select_repos(repos, spec)

    try:
        with global_lock(
            "exclusive",
            timeout=_LOCK_TIMEOUT_SECONDS,
            subcommand="rebase-pr",
        ):
            return _run_under_lock(
                args, policy, repos, repos_text, skipped_entries,
                spec, repos_excluded,
            )
    except LockTimeoutError as e:
        print(
            f"gitbulk rebase-pr: timed out acquiring lock: {e}",
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
        "rebase-pr", argv=list(sys.argv), config_snapshot=config_snapshot
    )

    gh = ProductionGHClient()
    ctx_base = InvariantContext(policy=policy, runstate=rs, gh=gh)

    sub = subcommands_mod.by_name("rebase-pr")
    universal, per_repo, per_pr = _partition_chain(sub.invariant_chain)

    skip_list = list(args.skip_check or [])
    skip_set = frozenset(skip_list)
    if skip_list:
        rs.record_error(
            f"--skip-check applied: {sorted(skip_list)}",
            level="WARNING",
            context={"skipped_invariants": sorted(skip_list)},
        )

    # UNIVERSAL preflight.
    universal_result = run_chain(
        universal, ctx_base, skip_set=skip_set, target="global"
    )
    if not universal_result.passed:
        return _finish(
            rs,
            EXIT_STRUCTURAL_FAILURE,
            summary=f"universal preflight failed: {universal_result.fail_reason}",
            policy=policy,
            attention=False,
            all_repos=repos,
            passing_repos=[],
            skipped_repos=[],
            results=[],
            apply=bool(args.apply),
            skipped_entries=skipped_entries,
        )

    # Prime default-branch cache before per-repo invariants.
    prefetch_prog = Progress(
        len(repos), prefix="prefetching default branches: "
    )
    prime_default_branches(
        gh,
        [r.slug for r in repos],
        on_progress=lambda done, total: prefetch_prog.update(done),
    )
    prefetch_prog.done()

    # PER_REPO preflight.
    skipped_repos: list[tuple[str, str]] = []
    passing_repos: list[RepoEntry] = []
    progress = Progress(len(repos), prefix="per-repo checks: ")
    for i, repo in enumerate(repos, start=1):
        progress.update(i, repo.slug)
        ctx_repo = replace(ctx_base, repo=repo)
        r = run_chain(per_repo, ctx_repo, skip_set=skip_set, target=repo.slug)
        if not r.passed:
            progress.done()
            return _finish(
                rs,
                EXIT_STRUCTURAL_FAILURE,
                summary=f"per-repo invariant failed on {repo.slug}: {r.fail_reason}",
                policy=policy,
                attention=False,
                all_repos=repos,
                passing_repos=passing_repos,
                skipped_repos=skipped_repos,
                results=[],
                apply=bool(args.apply),
                skipped_entries=skipped_entries,
            )
        intrinsic_skips = [
            (n, reason) for n, reason in r.skips if n not in skip_set
        ]
        if intrinsic_skips:
            reason = "; ".join(reason for _, reason in intrinsic_skips)
            skipped_repos.append((repo.slug, reason))
        else:
            passing_repos.append(repo)
    progress.done()

    repo_by_slug = {r.slug: r for r in passing_repos}

    # Coalesced PR fetch.
    if passing_repos:
        try:
            prs_by_repo = gh.my_open_prs([r.slug for r in passing_repos])
        except GHError as e:
            rs.record_error(f"my_open_prs failed: {e}")
            return _finish(
                rs,
                EXIT_STRUCTURAL_FAILURE,
                summary=f"gh PR fetch failed: {e}",
                policy=policy,
                attention=False,
                all_repos=repos,
                passing_repos=passing_repos,
                skipped_repos=skipped_repos,
                results=[],
                apply=bool(args.apply),
                skipped_entries=skipped_entries,
            )
    else:
        prs_by_repo = {}

    # Apply PR-level filters (base, mergeable_state). Author is always
    # self here (the --author veto above guarantees it), so the fetch
    # stays the default @me.
    prs_by_repo, prs_excluded = apply_pr_filters(prs_by_repo, spec)
    filter_line = filter_summary_line(spec, repos_excluded, prs_excluded)

    # PER_PR chain → eligible PRs (those that need a rebase).
    eligible_prs: list[tuple[str, PRInfo]] = []
    for repo in passing_repos:
        ctx_repo = replace(ctx_base, repo=repo)
        for pr in prs_by_repo.get(repo.slug, []):
            ctx_pr = replace(ctx_repo, pr=pr)
            target = f"{repo.slug}#{pr.number}"
            pr_result = run_chain(per_pr, ctx_pr, skip_set=skip_set, target=target)
            intrinsic_pr_skips = [
                (n, reason) for n, reason in pr_result.skips if n not in skip_set
            ]
            if pr_result.passed and not intrinsic_pr_skips:
                eligible_prs.append((repo.slug, pr))

    # DRY-RUN GATE.
    if not args.apply:
        summary_md = _build_summary_md(
            policy,
            all_repos=repos,
            passing_repos=passing_repos,
            skipped_repos=skipped_repos,
            results=[
                {"slug": s, "number": pr.number, "title": pr.title,
                 "url": pr.url, "outcome": "would-rebase"}
                for s, pr in eligible_prs
            ],
            apply=False,
            skipped_entries=skipped_entries,
            filter_line=filter_line,
        )
        rs.write_summary(summary_md)
        if skipped_repos or skipped_entries:
            exit_code = EXIT_INVARIANT_SKIPPED
            attention = True
        elif skip_list:
            exit_code = EXIT_OVERRIDES_APPLIED
            attention = False
        elif eligible_prs:
            exit_code = EXIT_ATTENTION_NEEDED
            attention = True
        else:
            exit_code = EXIT_OK
            attention = False
        summary_text = (
            f"dry-run: {len(eligible_prs)} PR(s) would be rebased; "
            f"{len(skipped_repos)} repos skipped; "
            f"{len(skipped_entries)} entries skipped"
            + (f"; {filter_line}" if filter_line else "")
        )
        return _finish(
            rs,
            exit_code,
            summary=summary_text,
            policy=policy,
            attention=attention,
            all_repos=repos,
            passing_repos=passing_repos,
            skipped_repos=skipped_repos,
            results=None,
            apply=False,
            skip_writing_summary=True,
            skipped_entries=skipped_entries,
        )

    # ── --apply path ──
    runid = _runid_from_run_dir(rs.run_dir)
    results: list[dict] = []
    rebased_count = 0
    conflict_count = 0
    failure_count = 0
    apply_prog = Progress(len(eligible_prs), prefix="rebasing: ")
    for i, (slug, pr) in enumerate(eligible_prs, start=1):
        apply_prog.update(i, f"{slug}#{pr.number}")
        repo = repo_by_slug[slug]
        base_ref = pr.base_ref
        record = {
            "slug": slug,
            "number": pr.number,
            "title": pr.title,
            "url": pr.url,
        }
        # Create the worktree.
        try:
            worktree_path = create_worktree(
                repo.local_path,
                slug,
                pr.number,
                pr.head_ref,
                pr.head_sha,
                worktree_root=policy.worktree_root,
                runid=runid,
            )
        except WorktreeError as e:
            failure_count += 1
            rs.record_error(
                f"worktree creation failed for {slug}#{pr.number}: {e}",
                level="ERROR",
                context={"slug": slug, "pr": pr.number, "stderr": e.stderr or ""},
            )
            record.update(outcome="error", detail=f"worktree: {e}")
            results.append(record)
            continue

        rebase_result = rebase_onto_base(worktree_path, base_ref)

        if rebase_result.status is RebaseStatus.CLEAN:
            try:
                force_push_with_lease(worktree_path, pr.head_ref, pr.head_sha)
            except RebaseError as e:
                failure_count += 1
                rs.record_error(
                    f"force-push failed for {slug}#{pr.number}: {e}",
                    level="ERROR",
                    context={"slug": slug, "pr": pr.number, "stderr": e.stderr or ""},
                )
                record.update(outcome="error", detail=f"push: {e.stderr or e}")
                _safe_remove_worktree(repo.local_path, worktree_path, rs, slug, pr)
                results.append(record)
                continue
            rebased_count += 1
            record.update(outcome="rebased", detail=rebase_result.detail)
            _safe_remove_worktree(repo.local_path, worktree_path, rs, slug, pr)
            results.append(record)

        elif rebase_result.status is RebaseStatus.CONFLICT:
            # Preserve the worktree mid-rebase for manual resolution.
            conflict_count += 1
            _write_conflict_marker(
                worktree_path, slug, pr, base_ref, rebase_result.detail
            )
            rs.record_error(
                f"rebase conflict for {slug}#{pr.number}; worktree preserved at "
                f"{worktree_path}",
                level="WARNING",
                context={
                    "slug": slug,
                    "pr": pr.number,
                    "worktree": str(worktree_path),
                    "conflicted": rebase_result.detail,
                },
            )
            record.update(
                outcome="conflict",
                detail=rebase_result.detail,
                worktree=str(worktree_path),
            )
            results.append(record)

        else:  # ERROR — rebase already aborted inside rebase_onto_base
            failure_count += 1
            rs.record_error(
                f"rebase failed for {slug}#{pr.number}: {rebase_result.detail}",
                level="ERROR",
                context={"slug": slug, "pr": pr.number},
            )
            record.update(outcome="error", detail=rebase_result.detail)
            _safe_remove_worktree(repo.local_path, worktree_path, rs, slug, pr)
            results.append(record)
    apply_prog.done()

    for repo in passing_repos:
        repo_results = [r for r in results if r["slug"] == repo.slug]
        if repo_results:
            rs.record_repo_state(
                repo.slug,
                {"pr_count": len(repo_results), "prs": repo_results},
            )

    # Exit code: failures or conflicts both warrant attention.
    if failure_count > 0 or conflict_count > 0:
        exit_code = EXIT_ATTENTION_NEEDED
        attention = True
    elif skipped_repos or skipped_entries:
        exit_code = EXIT_INVARIANT_SKIPPED
        attention = True
    elif skip_list:
        exit_code = EXIT_OVERRIDES_APPLIED
        attention = False
    else:
        exit_code = EXIT_OK
        attention = False

    summary_md = _build_summary_md(
        policy,
        all_repos=repos,
        passing_repos=passing_repos,
        skipped_repos=skipped_repos,
        results=results,
        apply=True,
        skipped_entries=skipped_entries,
        filter_line=filter_line,
    )
    rs.write_summary(summary_md)

    summary_text = (
        f"rebased {rebased_count}, {conflict_count} conflicts (worktree "
        f"preserved), {failure_count} failed; "
        f"{len(skipped_repos)} repos skipped; "
        f"{len(skipped_entries)} entries skipped"
        + (f"; {filter_line}" if filter_line else "")
    )
    return _finish(
        rs,
        exit_code,
        summary=summary_text,
        policy=policy,
        attention=attention,
        all_repos=repos,
        passing_repos=passing_repos,
        skipped_repos=skipped_repos,
        results=results,
        apply=True,
        skip_writing_summary=True,
        skipped_entries=skipped_entries,
    )


def _safe_remove_worktree(
    repo_path: Path, worktree_path: Path, rs: RunState, slug: str, pr: PRInfo
) -> None:
    """Remove a worktree, recording (not raising) on teardown failure.

    A failed teardown leaves a stray worktree but must not crash the run
    or mask the actual rebase outcome — gitbulk gc (future) can sweep it.
    """
    try:
        remove_worktree(repo_path, worktree_path)
    except WorktreeError as e:
        rs.record_error(
            f"worktree teardown failed for {slug}#{pr.number}: {e}",
            level="WARNING",
            context={"slug": slug, "pr": pr.number, "worktree": str(worktree_path)},
        )


def _build_summary_md(
    policy: Policy,
    *,
    all_repos: list[RepoEntry],
    passing_repos: list[RepoEntry],
    skipped_repos: list[tuple[str, str]],
    results: list[dict] | None,
    apply: bool,
    skipped_entries: list[SkippedEntry] | None = None,
    filter_line: str | None = None,
) -> str:
    skipped_entries = skipped_entries or []
    results = results or []
    lines: list[str] = ["# gitbulk rebase-pr", ""]
    mode = "APPLY" if apply else "DRY-RUN"
    lines.append(f"Mode: **{mode}**")
    lines.append(
        f"Configured repos: {len(all_repos)}  "
        f"Reachable: {len(passing_repos)}  "
        f"Skipped: {len(skipped_repos)}  "
        f"PRs: {len(results)}"
    )
    if filter_line:
        lines.append(filter_line)
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

    if not results:
        lines.append("(no PRs need rebasing)")
        lines.append("")
        return "\n".join(lines)

    if not apply:
        lines.append("## Would rebase")
        for r in results:
            lines.append(f"- {r['url']} *{r['title']}*")
        lines.append("")
        return "\n".join(lines)

    # apply mode — group by outcome, one self-describing line each.
    section = {
        "rebased": "## Rebased (force-pushed)",
        "conflict": "## Conflicts — worktree preserved for manual fix-up",
        "error": "## Errors",
    }
    for outcome in ("rebased", "conflict", "error"):
        rows = [r for r in results if r["outcome"] == outcome]
        if not rows:
            continue
        lines.append(section[outcome])
        for r in rows:
            extra = ""
            if outcome == "conflict":
                extra = f"  [worktree: {r.get('worktree', '?')}]"
            elif outcome == "error":
                extra = f"  — {r.get('detail', '?')}"
            lines.append(f"- {r['url']} *{r['title']}*{extra}")
        lines.append("")
    return "\n".join(lines)


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
    results: list[dict] | None,
    apply: bool,
    skip_writing_summary: bool = False,
    skipped_entries: list[SkippedEntry] | None = None,
) -> int:
    if not skip_writing_summary:
        synth = _build_summary_md(
            policy,
            all_repos=all_repos,
            passing_repos=passing_repos,
            skipped_repos=skipped_repos,
            results=results,
            apply=apply,
            skipped_entries=skipped_entries,
        )
        rs.write_summary(f"# gitbulk rebase-pr (FAILED)\n\n{summary}\n\n{synth}")

    if attention:
        runid = _runid_from_run_dir(rs.run_dir)
        sentinel.set_attention(exit_code, "rebase-pr", runid, summary)

    rs.complete(exit_code, retain_runs=policy.defaults.retain_runs)
    print(f"gitbulk rebase-pr: {summary}. View: gitbulk show rebase-pr")
    return exit_code


__all__ = [
    "EXIT_ATTENTION_NEEDED",
    "EXIT_INVARIANT_SKIPPED",
    "EXIT_OK",
    "EXIT_OVERRIDES_APPLIED",
    "EXIT_STRUCTURAL_FAILURE",
    "rebase_pr_handler",
]
