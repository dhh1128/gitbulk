"""``gitbulk dispatch`` — parallel headless-claude execution per PR.

Wires Phase 4 pieces together (worktrees, exec kernel, invariants,
RunState) into the only mutating Phase-4 subcommand. Inside a single
gitbulk run:

  - one shared worktree root under ``~/.cache/gitbulk/worktrees/<runid>/``
  - one disposable detached-HEAD worktree per eligible PR
  - one bounded-parallel pool of ``claude`` children (default 2)
  - one ``ExecResult`` per PR, recorded into ``state.yaml``

Pipeline:

  1. Load policy + repos (same shape as ``report``); validate
     ``--prompt`` was passed and points at a non-empty file. (Prompt
     validation lives in the handler rather than as an invariant —
     see node ``c4jzm5pn``: the invariants framework is the right
     place for policy that depends on ``repo``/``pr``/``gh`` context.
     The prompt path is a CLI argument that doesn't fit that shape,
     and synthesizing a new ``InvariantContext.extras`` channel for
     one user would make the framework more permissive than it
     should be. The summarize subcommand already validates its
     ``--prompt`` in-handler — we follow the same pattern for
     cross-subcommand consistency.)
  2. Acquire global EXCLUSIVE lock with 1800s timeout (mutating
     subcommand per node ``2vqp4nk6``; lock-mode per ``lj5pqn4kr``;
     timeout per ``tmlk5pq3``).
  3. RunState.begin("dispatch", ...).
  4. Run UNIVERSAL invariants (``gh.authenticated``, ``config.parseable``,
     ``org.members.fresh``). Fail → exit 1.
  5. Run PER_REPO invariants for each repo. Skip drops the repo from
     this run; Fail aborts the whole run with exit 1.
  6. Coalesced ``gh.my_open_prs`` for surviving repos.
  7. Run PER_PR invariants per PR; eligible PRs are those that PASS
     with no intrinsic Skips.
  8. DRY-RUN GATE: if ``--apply`` is not set (the default per node
     ``2vqp4nk6``), emit a summary listing what WOULD dispatch and
     exit 0. No worktree created, no claude invoked.
  9. (``--apply`` path) For each eligible PR:
     - ``worktree.create_worktree(...)`` rooted at
       ``policy.worktree_root/<runid>``.
     - Build an :class:`~gitbulk.exec.ExecTarget` with ``cwd`` set to
       the worktree.
  10. ``execute_targets(...)`` runs the bounded pool.
  11. For each result: if the worktree is in conflict per
      ``worktree.is_worktree_in_conflict`` (or the run failed), write a
      ``CONFLICT.md`` alongside the worktree and PRESERVE it (per node
      ``vp7n2krq``); otherwise tear it down.
  12. Compute exit code (priority order):
        - any failed/timed-out → EXIT_ATTENTION_NEEDED (2)
        - any skipped repos → EXIT_INVARIANT_SKIPPED (3)
        - --skip-check applied → EXIT_OVERRIDES_APPLIED (4)
        - else → EXIT_OK
  13. ATTENTION sentinel iff exit ∈ {2, 3}.
  14. ``rs.complete(exit_code, retain_runs=...)``.

The handler does NOT modify the user's main clone in any way (per
AGENTS.md "Local-git safety contract"). The only filesystem writes
on the apply path are to the worktree subdir and to the run dir.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path
from typing import Iterable

from gitbulk import paths, sentinel
from gitbulk.claude import ProductionClaudeClient
from gitbulk.config.policy import Policy, load_policy
from gitbulk.config.repos import RepoEntry, load_repos
from gitbulk.default_branch_cache import prime_default_branches
from gitbulk.filters import (
    apply_pr_filters,
    fetch_author,
    resolve_filter_spec,
    select_repos,
)
from gitbulk.exec import ExecResult, ExecTarget, execute_targets
from gitbulk.gh import GHError, ProductionGHClient
from gitbulk.org_members_cache import (
    OrgMembersRefreshError,
    ensure_org_members_fresh,
)
from gitbulk.invariants import InvariantContext, get, run_chain
from gitbulk.invariants.base import Invariant, InvariantKind
from gitbulk.locks import LockTimeoutError, global_lock
from gitbulk.pr_info import PRInfo
from gitbulk.runstate import RunState
from gitbulk.util.progress import Progress
from gitbulk.util.style import error_line
from gitbulk import subcommands as subcommands_mod
from gitbulk.worktree import (
    WorktreeError,
    create_worktree,
    is_worktree_in_conflict,
    remove_worktree,
)

# Exit codes — duplicated here (instead of importing from cli.py) so
# the cli ↔ commands dep stays one-way: cli imports commands, never
# the reverse. See report.py for the same convention.
EXIT_OK = 0
EXIT_STRUCTURAL_FAILURE = 1
EXIT_ATTENTION_NEEDED = 2
EXIT_INVARIANT_SKIPPED = 3
EXIT_OVERRIDES_APPLIED = 4

#: Per node ``tmlk5pq3``: mutating subcommands get a 1800s lock budget.
_LOCK_TIMEOUT_SECONDS: float = 1800.0

#: Per-target default timeout (seconds). Matches the conservative
#: default in node ``execk7nm`` for unattended dispatch.
_DEFAULT_PER_TARGET_TIMEOUT: float = 1800.0

#: Default bounded-pool size. CLAUDE.md notes the user's machine
#: tolerates ~2 concurrent claude children comfortably.
_DEFAULT_CONCURRENCY: int = 2


# ─── Internal helpers ─────────────────────────────────────────────────────


def _partition_chain(
    chain_names: Iterable[str],
) -> tuple[list[type[Invariant]], list[type[Invariant]], list[type[Invariant]]]:
    """Look up each registered name and split by ``InvariantKind``.

    Mirrors :func:`gitbulk.commands.report._partition_chain`. Kept as a
    private copy rather than imported to avoid cross-command coupling
    that would force a refactor of either file on future renames.
    """
    universal: list[type[Invariant]] = []
    per_repo: list[type[Invariant]] = []
    per_pr: list[type[Invariant]] = []
    for n in chain_names:
        cls = get(n)
        if cls.kind == InvariantKind.UNIVERSAL:
            universal.append(cls)
        elif cls.kind == InvariantKind.PER_REPO:
            per_repo.append(cls)
        else:  # PER_PR
            per_pr.append(cls)
    return universal, per_repo, per_pr


def _config_snapshot(
    policy: Policy, repos_text: str, prompt_path: Path, args: argparse.Namespace
) -> dict:
    """Inline manifest snapshot. Records the prompt path + the apply
    flag so a forensic reader can tell a dry-run from an apply run.
    """
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
        "prompt_path": str(prompt_path),
        "apply": bool(getattr(args, "apply", False)),
        "concurrency": int(getattr(args, "concurrency", _DEFAULT_CONCURRENCY)),
        "timeout": float(getattr(args, "timeout", _DEFAULT_PER_TARGET_TIMEOUT)),
        "filter": getattr(args, "filter", None),
    }


def _dc_to_dict(obj) -> dict:
    """Flatten a frozen dataclass into a YAML-friendly dict.

    Identical to the helper in report.py; duplicated for the same
    reason — keeping each handler standalone makes cross-handler
    refactors cheaper.
    """
    from dataclasses import asdict

    out: dict = {}
    for k, v in asdict(obj).items():
        if isinstance(v, tuple):
            out[k] = list(v)
        else:
            out[k] = v
    return out


def _runid_from_run_dir(run_dir: Path) -> str:
    """Extract the timestamp portion of ``<RUNID>-dispatch``."""
    name = run_dir.name
    suffix = "-dispatch"
    if name.endswith(suffix):
        return name[: -len(suffix)]
    head, _, _ = name.rpartition("-")
    return head


def _read_repos_text() -> str:
    """Return the raw text of repos.txt; ``load_repos`` already
    validated the file exists, so a separate exists() check would be
    dead code."""
    return paths.repos_file().read_text()


def _validate_prompt(args: argparse.Namespace) -> tuple[Path | None, str | None]:
    """Validate --prompt was given and points at a non-empty file.

    Returns ``(prompt_path, None)`` on success; ``(None, error_msg)``
    on failure. Three error branches: arg missing, path missing, path
    empty. Each is reported with a distinct message so the operator
    can act without guessing.
    """
    raw = getattr(args, "prompt", None)
    if not raw:
        return None, "dispatch requires --prompt PATH"
    prompt_path = Path(raw).expanduser()
    if not prompt_path.exists():
        return None, f"prompt file not found: {prompt_path}"
    # ``stat().st_size`` is the cheap empty-check; reading the file
    # would require loading megabytes if the operator pointed at a
    # huge prompt file.
    if prompt_path.stat().st_size == 0:
        return None, f"prompt file is empty: {prompt_path}"
    return prompt_path, None


def _key_for_pr(slug: str, pr_number: int) -> str:
    """Build a filesystem-safe key for an ExecTarget.

    ``slug`` is ``owner/repo``; we use the same ``owner__repo`` shape
    that ``paths._normalize_slug`` writes for worktree directories so
    a log file's key matches the worktree directory name.
    """
    return f"{slug.replace('/', '__')}__pr{pr_number}"


def _build_summary_md(
    policy: Policy,
    *,
    all_repos: list[RepoEntry],
    passing_repos: list[RepoEntry],
    skipped_repos: list[tuple[str, str]],
    eligible_prs: list[tuple[str, PRInfo]],
    results: list[ExecResult] | None,
    apply: bool,
    prompt_path: Path,
) -> str:
    """Human-readable summary.md.

    Two distinct shapes:
      - dry-run: lists what WOULD dispatch (eligible_prs).
      - apply: lists what was attempted + final status per PR.
    """
    del policy  # not yet rendered; kept in signature for parity
    lines: list[str] = ["# gitbulk dispatch", ""]

    mode = "APPLY" if apply else "DRY-RUN"
    lines.append(f"Mode: **{mode}**")
    lines.append(f"Prompt: `{prompt_path}`")
    lines.append(
        f"Configured repos: {len(all_repos)}  "
        f"Reachable: {len(passing_repos)}  "
        f"Skipped: {len(skipped_repos)}  "
        f"Eligible PRs: {len(eligible_prs)}"
    )
    lines.append("")

    if skipped_repos:
        lines.append("## Skipped repos")
        for slug, reason in skipped_repos:
            lines.append(f"- `{slug}` — {reason}")
        lines.append("")

    if not eligible_prs:
        lines.append("(no eligible PRs to dispatch)")
        lines.append("")
        return "\n".join(lines)

    if not apply:
        lines.append("## Would dispatch")
        for slug, pr in eligible_prs:
            lines.append(
                f"- `{slug}` #{pr.number} *{pr.title}* "
                f"(head={pr.head_ref}@{pr.head_sha[:7]})"
            )
        lines.append("")
        return "\n".join(lines)

    # apply mode: render per-PR results.
    by_key = {r.key: r for r in (results or [])}
    lines.append("## Dispatch results")
    for slug, pr in eligible_prs:
        key = _key_for_pr(slug, pr.number)
        r = by_key.get(key)
        if r is None:
            status = "no result recorded"
        else:
            status = f"{r.status}"
            if r.exit_code is not None:
                status += f" (exit {r.exit_code})"
        lines.append(
            f"- `{slug}` #{pr.number} *{pr.title}* — {status}"
        )
    lines.append("")
    return "\n".join(lines)


def _attention_results(results: list[ExecResult]) -> list[ExecResult]:
    """Return the subset of results whose status warrants ATTENTION.

    ``failed`` and ``timed-out`` both bubble up as exit 2. ``interrupted``
    is treated as attention too — the operator interrupted the run, so
    the half-finished state is something they should look at.
    """
    return [
        r for r in results if r.status in ("failed", "timed-out", "interrupted")
    ]


# ─── Public handler ───────────────────────────────────────────────────────


def dispatch_handler(args: argparse.Namespace) -> int:
    """Top-level entry for ``gitbulk dispatch``."""
    # 1a. Load configuration (same shape as report). load_policy raises
    # if the YAML is malformed; that's a structural failure but we
    # don't have a RunState yet, so let it bubble to argparse-level.
    policy = load_policy()
    code_root = (
        Path(args.code_root).expanduser() if args.code_root else None
    )
    # TODO: surface skipped_entries in dispatch summary (mirror
    # report/merge treatment). For now ignore them so a typo in
    # repos.txt doesn't block the dispatch run.
    repos, _ = load_repos(code_root=code_root)
    repos_text = _read_repos_text()

    # Fleet-subset filter (node flt7arg2): prune repos before the lock.
    spec = resolve_filter_spec(args, policy)
    repos, repos_excluded = select_repos(repos, spec)

    # 1b. Validate --prompt BEFORE acquiring the lock. The error is
    # purely structural; nothing else can change between this check
    # and the lock-protected pipeline.
    prompt_path, err = _validate_prompt(args)
    if err is not None:
        print(error_line(f"gitbulk dispatch: {err}"), file=sys.stderr)
        return EXIT_STRUCTURAL_FAILURE
    assert prompt_path is not None  # narrowed by _validate_prompt contract

    # 2. Acquire global EXCLUSIVE lock with the mutating-budget timeout.
    try:
        with global_lock(
            "exclusive",
            timeout=_LOCK_TIMEOUT_SECONDS,
            subcommand="dispatch",
        ):
            return _run_under_lock(
                args, policy, repos, repos_text, prompt_path, spec
            )
    except LockTimeoutError as e:
        print(
            error_line(f"gitbulk dispatch: timed out acquiring lock: {e}"),
            file=sys.stderr,
        )
        return EXIT_STRUCTURAL_FAILURE


def _run_under_lock(
    args: argparse.Namespace,
    policy: Policy,
    repos: list[RepoEntry],
    repos_text: str,
    prompt_path: Path,
    spec,
) -> int:
    """Pipeline body that runs while the global EXCLUSIVE lock is held."""
    config_snapshot = _config_snapshot(policy, repos_text, prompt_path, args)
    rs = RunState.begin(
        "dispatch",
        argv=list(sys.argv),
        config_snapshot=config_snapshot,
    )

    gh = ProductionGHClient()
    ctx_base = InvariantContext(policy=policy, runstate=rs, gh=gh)

    # Auto-refresh org-members before the preflight (ormrf7kq). Mirrors
    # report + the default-branch cache: a missing/stale cache self-heals
    # rather than hard-failing; --refresh-org-members forces it. Inside
    # the lock (security-hawk F4). A refresh failure is the one
    # legitimate abort — a mutating command must not classify on a guess.
    try:
        ensure_org_members_fresh(
            gh, policy, force=bool(getattr(args, "refresh_org_members", False))
        )
    except OrgMembersRefreshError as e:
        rs.record_error(str(e))
        return _finish(
            rs,
            EXIT_STRUCTURAL_FAILURE,
            summary=str(e),
            policy=policy,
            attention=False,
            all_repos=repos,
            passing_repos=[],
            skipped_repos=[],
            eligible_prs=[],
            results=None,
            apply=bool(args.apply),
            prompt_path=prompt_path,
        )

    dispatch_sub = subcommands_mod.by_name("dispatch")
    universal, per_repo, per_pr = _partition_chain(dispatch_sub.invariant_chain)

    skip_list = list(args.skip_check or [])
    skip_set = frozenset(skip_list)
    if skip_list:
        rs.record_error(
            f"--skip-check applied: {sorted(skip_list)}",
            level="WARNING",
            context={"skipped_invariants": sorted(skip_list)},
        )

    # 4. UNIVERSAL preflight.
    universal_result = run_chain(
        universal, ctx_base, skip_set=skip_set, target="global"
    )
    if not universal_result.passed:
        return _finish(
            rs,
            EXIT_STRUCTURAL_FAILURE,
            summary=(
                f"universal preflight failed: "
                f"{universal_result.fail_reason}"
            ),
            policy=policy,
            attention=False,
            all_repos=repos,
            passing_repos=[],
            skipped_repos=[],
            eligible_prs=[],
            results=None,
            apply=bool(args.apply),
            prompt_path=prompt_path,
        )

    # 5. PER_REPO preflight; same Skip-vs-Skip discrimination as report.
    # Prime default-branch cache (see report.py / default_branch_cache
    # for rationale). Warm entries cost nothing; cold prefetch shows
    # progress.
    prefetch_prog = Progress(
        len(repos), prefix="prefetching default branches: "
    )
    prime_default_branches(
        gh,
        [r.slug for r in repos],
        on_progress=lambda done, total: prefetch_prog.update(done),
    )
    prefetch_prog.done()
    skipped_repos: list[tuple[str, str]] = []
    passing_repos: list[RepoEntry] = []
    for repo in repos:
        ctx_repo = replace(ctx_base, repo=repo)
        r = run_chain(per_repo, ctx_repo, skip_set=skip_set, target=repo.slug)
        if not r.passed:
            return _finish(
                rs,
                EXIT_STRUCTURAL_FAILURE,
                summary=(
                    f"per-repo invariant failed on {repo.slug}: "
                    f"{r.fail_reason}"
                ),
                policy=policy,
                attention=False,
                all_repos=repos,
                passing_repos=passing_repos,
                skipped_repos=skipped_repos,
                eligible_prs=[],
                results=None,
                apply=bool(args.apply),
                prompt_path=prompt_path,
            )
        intrinsic_skips = [
            (n, reason) for n, reason in r.skips if n not in skip_set
        ]
        if intrinsic_skips:
            reason = "; ".join(reason for _, reason in intrinsic_skips)
            skipped_repos.append((repo.slug, reason))
        else:
            passing_repos.append(repo)

    # 6. Coalesced PR fetch. dispatch can run agents against others' PRs
    # (e.g. reviewing/fixing contributions), so the author filter may
    # widen (node flt7arg2); default @me.
    if passing_repos:
        try:
            prs_by_repo = gh.my_open_prs(
                [r.slug for r in passing_repos], author=fetch_author(spec)
            )
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
                eligible_prs=[],
                results=None,
                apply=bool(args.apply),
                prompt_path=prompt_path,
            )
    else:
        prs_by_repo = {}

    # Apply PR-level filters (base, mergeable_state) before the per-PR
    # invariant chain selects dispatch targets.
    prs_by_repo, _prs_excluded = apply_pr_filters(prs_by_repo, spec)

    # 7. PER_PR invariants; build eligible list.
    eligible_prs: list[tuple[str, PRInfo]] = []
    repo_by_slug = {r.slug: r for r in passing_repos}
    for repo in passing_repos:
        ctx_repo = replace(ctx_base, repo=repo)
        repo_prs = prs_by_repo.get(repo.slug, [])
        for pr in repo_prs:
            ctx_pr = replace(ctx_repo, pr=pr)
            target = f"{repo.slug}#{pr.number}"
            pr_result = run_chain(
                per_pr, ctx_pr, skip_set=skip_set, target=target
            )
            intrinsic_pr_skips = [
                (n, reason) for n, reason in pr_result.skips
                if n not in skip_set
            ]
            if pr_result.passed and not intrinsic_pr_skips:
                eligible_prs.append((repo.slug, pr))

    # 8. DRY-RUN GATE.
    if not args.apply:
        summary_md = _build_summary_md(
            policy,
            all_repos=repos,
            passing_repos=passing_repos,
            skipped_repos=skipped_repos,
            eligible_prs=eligible_prs,
            results=None,
            apply=False,
            prompt_path=prompt_path,
        )
        rs.write_summary(summary_md)
        # Dry-run exit code: same priority ladder, minus the attention
        # branch (there's nothing to attend to yet).
        if skipped_repos:
            exit_code = EXIT_INVARIANT_SKIPPED
            attention = True
        elif skip_list:
            exit_code = EXIT_OVERRIDES_APPLIED
            attention = False
        else:
            exit_code = EXIT_OK
            attention = False
        summary_text = (
            f"dry-run: {len(eligible_prs)} PRs would dispatch; "
            f"{len(skipped_repos)} repos skipped"
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
            eligible_prs=eligible_prs,
            results=None,
            apply=False,
            prompt_path=prompt_path,
            skip_writing_summary=True,
        )

    # 9. (--apply path) Create one worktree per eligible PR.
    prompt_text = prompt_path.read_text()
    runid = _runid_from_run_dir(rs.run_dir)
    targets: list[ExecTarget] = []
    # Map ExecTarget.key → (slug, pr, worktree_path) so the post-pass
    # cleanup can find the worktree without re-deriving the path.
    target_meta: dict[str, tuple[str, PRInfo, Path]] = {}

    for slug, pr in eligible_prs:
        repo = repo_by_slug[slug]
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
            # Worktree creation failed; record and skip this PR.
            # We DO NOT abort the whole run — the user's other 149
            # repos still deserve a shot.
            rs.record_error(
                f"worktree creation failed for {slug}#{pr.number}: {e}",
                level="ERROR",
                context={
                    "slug": slug,
                    "pr": pr.number,
                    "stderr": e.stderr or "",
                },
            )
            continue

        key = _key_for_pr(slug, pr.number)
        targets.append(
            ExecTarget(
                key=key,
                working_directory=worktree_path,
                prompt=prompt_text,
                input_text=None,
            )
        )
        target_meta[key] = (slug, pr, worktree_path)

    # 10. Run the bounded pool.
    log_dir = rs.run_dir / "dispatch-logs"
    claude = ProductionClaudeClient()
    concurrency = int(getattr(args, "concurrency", _DEFAULT_CONCURRENCY))
    timeout_per_target = float(
        getattr(args, "timeout", _DEFAULT_PER_TARGET_TIMEOUT)
    )

    results = execute_targets(
        targets,
        claude=claude,
        log_dir=log_dir,
        concurrency=concurrency,
        timeout_per_target=timeout_per_target,
    )

    # 11. Worktree cleanup vs preservation per node vp7n2krq.
    per_pr_states: dict[str, list[dict]] = {}
    for r in results:
        slug, pr, worktree_path = target_meta[r.key]
        repo = repo_by_slug[slug]
        in_conflict = is_worktree_in_conflict(worktree_path)
        preserved = False
        if in_conflict:
            preserved = True
            _write_conflict_marker(worktree_path, slug, pr, r)
        else:
            try:
                remove_worktree(repo.local_path, worktree_path)
            except WorktreeError as e:
                # Best-effort: record and leave on disk for the operator.
                preserved = True
                rs.record_error(
                    f"worktree teardown failed for {slug}#{pr.number}: {e}",
                    level="WARNING",
                    context={
                        "slug": slug,
                        "pr": pr.number,
                        "stderr": e.stderr or "",
                    },
                )

        per_pr_states.setdefault(slug, []).append(
            {
                "number": pr.number,
                "title": pr.title,
                "url": pr.url,
                "head_sha": pr.head_sha,
                "status": r.status,
                "exit_code": r.exit_code,
                "duration_seconds": r.duration_seconds,
                "worktree_path": str(worktree_path),
                "worktree_preserved": preserved,
                "in_conflict": in_conflict,
                "stdout_log": str(r.stdout_path),
                "stderr_log": str(r.stderr_path),
            }
        )

    for slug, states in per_pr_states.items():
        rs.record_repo_state(slug, {"pr_count": len(states), "prs": states})

    # 12. Compute exit code.
    attention_results = _attention_results(results)
    if attention_results:
        exit_code = EXIT_ATTENTION_NEEDED
    elif skipped_repos:
        exit_code = EXIT_INVARIANT_SKIPPED
    elif skip_list:
        exit_code = EXIT_OVERRIDES_APPLIED
    else:
        exit_code = EXIT_OK

    summary_md = _build_summary_md(
        policy,
        all_repos=repos,
        passing_repos=passing_repos,
        skipped_repos=skipped_repos,
        eligible_prs=eligible_prs,
        results=results,
        apply=True,
        prompt_path=prompt_path,
    )
    rs.write_summary(summary_md)

    summary_text = (
        f"dispatched {len(results)} PRs; "
        f"{len(attention_results)} need attention; "
        f"{len(skipped_repos)} repos skipped"
    )
    return _finish(
        rs,
        exit_code,
        summary=summary_text,
        policy=policy,
        attention=(exit_code in (EXIT_ATTENTION_NEEDED, EXIT_INVARIANT_SKIPPED)),
        all_repos=repos,
        passing_repos=passing_repos,
        skipped_repos=skipped_repos,
        eligible_prs=eligible_prs,
        results=results,
        apply=True,
        prompt_path=prompt_path,
        skip_writing_summary=True,
    )


def _write_conflict_marker(
    worktree_path: Path,
    slug: str,
    pr: PRInfo,
    result: ExecResult,
) -> None:
    """Write a CONFLICT.md alongside the preserved worktree.

    Per node vp7n2krq the in-conflict worktree is left on disk; this
    marker tells the user what state the run finished in and which
    log files to read first.
    """
    marker = worktree_path / "CONFLICT.md"
    marker.write_text(
        f"# gitbulk dispatch — worktree preserved\n"
        f"\n"
        f"Repo: {slug}\n"
        f"PR: #{pr.number} {pr.title}\n"
        f"URL: {pr.url}\n"
        f"Head SHA: {pr.head_sha}\n"
        f"Exec status: {result.status} (exit {result.exit_code})\n"
        f"\n"
        f"Logs:\n"
        f"- stdout: {result.stdout_path}\n"
        f"- stderr: {result.stderr_path}\n"
        f"\n"
        f"The worktree was left on disk because `git status --porcelain` "
        f"reported conflict markers, or because `git worktree remove` "
        f"itself failed. Resolve conflicts manually and run\n"
        f"`git worktree remove --force {worktree_path}` to clean up.\n"
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
    eligible_prs: list[tuple[str, PRInfo]],
    results: list[ExecResult] | None,
    apply: bool,
    prompt_path: Path,
    skip_writing_summary: bool = False,
) -> int:
    """Terminal-stage write: summary.md (if not already), sentinel,
    runstate.complete().

    Mirrors :func:`gitbulk.commands.report._finish`. ``skip_writing_summary``
    lets the apply-success and dry-run paths reuse this same finisher
    without overwriting a richer summary they already wrote.
    """
    if not skip_writing_summary:
        synth = _build_summary_md(
            policy,
            all_repos=all_repos,
            passing_repos=passing_repos,
            skipped_repos=skipped_repos,
            eligible_prs=eligible_prs,
            results=results,
            apply=apply,
            prompt_path=prompt_path,
        )
        rs.write_summary(f"# gitbulk dispatch (FAILED)\n\n{summary}\n\n{synth}")

    if attention:
        runid = _runid_from_run_dir(rs.run_dir)
        sentinel.set_attention(exit_code, "dispatch", runid, summary)

    rs.complete(exit_code, retain_runs=policy.defaults.retain_runs)
    return exit_code


__all__ = [
    "EXIT_ATTENTION_NEEDED",
    "EXIT_INVARIANT_SKIPPED",
    "EXIT_OK",
    "EXIT_OVERRIDES_APPLIED",
    "EXIT_STRUCTURAL_FAILURE",
    "dispatch_handler",
]
