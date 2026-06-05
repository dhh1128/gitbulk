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
import re
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import Iterable

from gitbulk import paths, sentinel
from gitbulk.agent import AgentConfigError, backend_for, resolve_agent_name
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
from gitbulk.isolated_clone import create_isolated_clone, remove_isolated_clone
from gitbulk.invariants.base import Invariant, InvariantKind
from gitbulk.locks import (
    LockTimeoutError,
    repo_lock,
    run_state_lock,
    sentinel_lock,
)
from gitbulk.pr_info import PRInfo
from gitbulk.rebase import (
    PushReadiness,
    RebaseError,
    RebaseStatus,
    fetch_base,
    force_push_with_lease,
    verify_resolved_for_push,
)
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


def _stdout_isatty() -> bool:
    """True if stdout is an interactive terminal. Cron / pipes are not TTYs,
    which is how we detect unattended mode (SEC-F3). Wrapped for monkeypatching
    and to swallow odd stream objects."""
    try:
        return bool(sys.stdout.isatty())
    except Exception:  # pragma: no cover - defensive
        return False


def _apply_foreign_author_gate(
    eligible_prs: list[tuple[str, "PRInfo"]],
    gh,
    rs: RunState,
    *,
    allow_foreign: bool,
) -> tuple[list[tuple[str, "PRInfo"]], str | None]:
    """Drop PRs not authored by the operator unless explicitly allowed (SEC-F3).

    Returns ``(filtered_prs, error)``. ``error`` is non-None (and the run
    should abort structurally) when ``--allow-foreign-authors`` is passed in
    unattended/cron mode — the flag is interactive-only. When allowed and
    interactive, foreign PRs are kept (a WARNING is still recorded for the
    audit trail).
    """
    if allow_foreign and not _stdout_isatty():
        return (
            [],
            "--allow-foreign-authors is refused in unattended/cron mode "
            "(no TTY); run it interactively to dispatch agents on PRs you "
            "did not author.",
        )
    try:
        operator_login = (gh.authenticated_user() or {}).get("login")
    except GHError as e:
        return ([], f"could not determine the authenticated user: {e}")

    kept: list[tuple[str, PRInfo]] = []
    for slug, pr in eligible_prs:
        is_foreign = operator_login is None or pr.author != operator_login
        if is_foreign and not allow_foreign:
            rs.record_error(
                f"skipping foreign-authored PR {slug}#{pr.number}: author "
                f"{pr.author!r} != operator {operator_login!r}. Pass "
                f"--allow-foreign-authors (interactively) to dispatch on it.",
                level="WARNING",
                context={"slug": slug, "pr": pr.number, "author": pr.author},
            )
            continue
        if is_foreign:
            rs.record_error(
                f"dispatching on FOREIGN-authored PR {slug}#{pr.number} "
                f"(author {pr.author!r}) under --allow-foreign-authors",
                level="WARNING",
                context={"slug": slug, "pr": pr.number, "author": pr.author},
            )
        kept.append((slug, pr))
    return (kept, None)


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
    outcomes: dict[str, str] | None = None,
) -> str:
    """Human-readable summary.md.

    Two distinct shapes:
      - dry-run: lists what WOULD dispatch (eligible_prs).
      - apply: lists what was attempted + final status per PR.

    ``outcomes`` maps an ExecTarget key to the agent's normalized
    ``RESOLVED:``/``ESCALATED:`` line; when present it is shown per PR
    instead of the bare process status (Gap 1, this.i dspesc4q).
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
    outcome_map = outcomes or {}
    n_resolved = sum(1 for v in outcome_map.values() if v.startswith("RESOLVED"))
    n_escalated = sum(
        1 for v in outcome_map.values() if v.startswith("ESCALATED")
    )
    lines.append("## Dispatch results")
    lines.append(f"Resolved: {n_resolved}  Escalated: {n_escalated}")
    lines.append("")
    for slug, pr in eligible_prs:
        key = _key_for_pr(slug, pr.number)
        r = by_key.get(key)
        outcome_line = outcome_map.get(key)
        if r is None:
            status = "no result recorded"
        elif outcome_line is not None:
            # Prefer the agent's own verdict over the bare process status.
            status = outcome_line
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


#: The resolve-conflicts prompt ends with a single ``RESOLVED:`` / ``ESCALATED:``
#: status line. Agents sometimes wrap it in backticks, hence the strip below.
_OUTCOME_RE = re.compile(r"^(RESOLVED|ESCALATED):\s*(.*)$")


def _parse_agent_outcome(stdout_path: Path) -> tuple[str | None, str | None]:
    """Return ``(verdict, line)`` from a dispatched agent's stdout log.

    ``verdict`` is ``"RESOLVED"`` or ``"ESCALATED"``; ``line`` is the
    normalized full status line (``"VERDICT: detail"``, or just the verdict
    when no detail). Scans from the end and returns the LAST match — the
    agent's final word. Returns ``(None, None)`` when the log is missing,
    unreadable, or has no status line, so a malformed agent run degrades to
    "unknown" rather than crashing the finalizer (Gap 1, this.i dspesc4q).
    """
    try:
        text = stdout_path.read_text()
    except OSError:
        return (None, None)
    for raw in reversed(text.splitlines()):
        line = raw.strip().strip("`").strip()
        m = _OUTCOME_RE.match(line)
        if m:
            verdict = m.group(1)
            detail = m.group(2).strip()
            return (verdict, f"{verdict}: {detail}" if detail else verdict)
    return (None, None)


def _salvage_escalation(
    worktree_path: Path, dest_dir: Path, key: str
) -> str | None:
    """Copy an agent-written ``ESCALATION.md`` into the durable run dir.

    A clean escalation runs ``git rebase --abort``, so the worktree is NOT
    in a git-conflict state and gets torn down — taking its ``ESCALATION.md``
    with it. Salvage it into ``<run>/escalations/<key>.md`` BEFORE teardown
    so the reason survives (Gap 2, this.i dspesc4q). Returns the saved path, or ``None`` when
    there is no ``ESCALATION.md`` or the copy fails (best-effort: a failed
    salvage must not abort finalizing the other PRs).
    """
    src = worktree_path / "ESCALATION.md"
    if not src.is_file():
        return None
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{key}.md"
        shutil.copyfile(src, dest)
    except OSError:
        return None
    return str(dest)


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

    # 2. Run the pipeline. Resource-scoped locking (node rsclk7nq): no global
    # lock. Caches self-lock in their helpers; each PR's worktree create/remove
    # takes repo_lock(slug) (NOT held across the agent pool); the terminal
    # writes take sentinel_lock + run_state_lock("dispatch").
    try:
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

    # 7b. FOREIGN-AUTHOR GATE (SEC-F3). The dispatched agent reads and operates
    # on the PR's content at its head SHA — which is attacker-controllable for a
    # PR you did not author. By default, skip PRs not authored by you. The
    # opt-in flag is interactive-only: refuse it under cron/no-TTY so a
    # misconfigured schedule can't quietly run agents on strangers' code.
    eligible_prs, foreign_gate_error = _apply_foreign_author_gate(
        eligible_prs, gh, rs, allow_foreign=bool(getattr(args, "allow_foreign_authors", False))
    )
    if foreign_gate_error is not None:
        return _finish(
            rs,
            EXIT_STRUCTURAL_FAILURE,
            summary=foreign_gate_error,
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
    # Map ExecTarget.key → (slug, pr, workspace_path, is_clone) so the post-pass
    # cleanup can find the workspace and tear it down correctly. ``is_clone`` is
    # True for a sandboxed agent's self-contained clone (agecln4k), False for a
    # linked worktree.
    target_meta: dict[str, tuple[str, PRInfo, Path, bool]] = {}

    # Resolve the agent backend(s) up front: --agent → per-repo agent: →
    # default_agent → claude (this.i agprof4k). The run default is resolved
    # FIRST — if it requires a sandbox the host can't provide (agsbx3k), refuse
    # the whole run cheaply before any worktree exists. Per-repo overrides are
    # resolved inside the loop so a refusing one skips just that PR. Backends
    # are cached by resolved name (build each distinct one once).
    requested_agent = getattr(args, "agent", None)
    default_name = resolve_agent_name(policy, requested_agent)
    try:
        default_backend = backend_for(policy, requested_agent)
    except AgentConfigError as e:
        return _finish(
            rs,
            EXIT_STRUCTURAL_FAILURE,
            summary=f"agent unavailable: {e}",
            policy=policy,
            attention=False,
            all_repos=repos,
            passing_repos=passing_repos,
            skipped_repos=skipped_repos,
            eligible_prs=eligible_prs,
            results=None,
            apply=True,
            prompt_path=prompt_path,
        )
    backend_by_name: dict = {default_name: default_backend}
    backends: dict = {}

    for slug, pr in eligible_prs:
        repo = repo_by_slug[slug]
        # Resolve this PR's backend before creating its worktree, so a per-repo
        # agent that refuses (sandbox unavailable) skips the PR with nothing to
        # clean up.
        name = resolve_agent_name(policy, requested_agent, slug=slug)
        if name not in backend_by_name:
            try:
                backend_by_name[name] = backend_for(
                    policy, requested_agent, slug=slug
                )
            except AgentConfigError as e:
                rs.record_error(
                    f"agent unavailable for {slug}#{pr.number}: {e}",
                    level="ERROR",
                    context={"slug": slug, "pr": pr.number},
                )
                continue
        # A sandboxed agent (agsbx3k) gets a self-contained CLONE instead of a
        # linked worktree (SEC-F1 / agecln4k): a worktree's .git points into the
        # operator clone, which the sandbox cannot bind. claude/unsandboxed
        # agents keep the cheaper linked worktree.
        is_clone = getattr(backend_by_name[name], "_sandbox", "none") != "none"
        try:
            # repo_lock(slug): workspace creation reads/mutates the clone's
            # .git admin — serialize it against another gitbulk run on the SAME
            # repo (node rsclk7nq #6). NOT held across the agent pool below.
            with repo_lock(
                slug, "exclusive", timeout=_LOCK_TIMEOUT_SECONDS,
                subcommand="dispatch",
            ):
                if is_clone:
                    workspace_path = create_isolated_clone(
                        repo.local_path, slug, pr.number, pr.head_ref,
                        pr.head_sha, worktree_root=policy.worktree_root,
                        runid=runid,
                    )
                else:
                    workspace_path = create_worktree(
                        repo.local_path, slug, pr.number, pr.head_ref,
                        pr.head_sha, worktree_root=policy.worktree_root,
                        runid=runid,
                    )
                # Least privilege (this.i agpriv8n): gitbulk performs the
                # networked base fetch itself, BEFORE the agent runs, so the
                # agent can rebase against origin/<base> with no network and no
                # credentials of its own. On fetch failure the workspace is
                # removed and the PR skipped (the agent could not rebase
                # offline anyway).
                fetched = fetch_base(workspace_path, pr.base_ref)
                if fetched.status is not RebaseStatus.CLEAN:
                    if is_clone:
                        remove_isolated_clone(
                            workspace_path, worktree_root=policy.worktree_root
                        )
                    else:
                        remove_worktree(repo.local_path, workspace_path)
        except WorktreeError as e:
            # Workspace creation/teardown failed; record and skip this PR.
            # We DO NOT abort the whole run — the user's other 149
            # repos still deserve a shot.
            rs.record_error(
                f"workspace creation failed for {slug}#{pr.number}: {e}",
                level="ERROR",
                context={
                    "slug": slug,
                    "pr": pr.number,
                    "stderr": e.stderr or "",
                },
            )
            continue

        if fetched.status is not RebaseStatus.CLEAN:
            rs.record_error(
                f"base prefetch failed for {slug}#{pr.number}: {fetched.detail}",
                level="ERROR",
                context={"slug": slug, "pr": pr.number},
            )
            continue

        key = _key_for_pr(slug, pr.number)
        targets.append(
            ExecTarget(
                key=key,
                working_directory=workspace_path,
                prompt=prompt_text,
                input_text=None,
            )
        )
        target_meta[key] = (slug, pr, workspace_path, is_clone)
        # Only non-default backends need a per-target entry; absent keys fall
        # back to ``default_backend`` in the kernel.
        if name != default_name:
            backends[key] = backend_by_name[name]

    # SEC-F4: a profile that requested a sandbox but ran UNSANDBOXED under
    # `sandbox_fallback: warn-run` must leave a durable signal, not just a
    # logging.warning that cron swallows. Record a WARNING per downgraded agent
    # and force ATTENTION so the operator actually sees it.
    sandbox_downgraded = False
    for agent_name, backend in backend_by_name.items():
        if getattr(backend, "sandbox_downgraded", False):
            sandbox_downgraded = True
            rs.record_error(
                f"agent {agent_name!r} ran UNSANDBOXED: requested sandbox "
                f"{getattr(backend, 'requested_sandbox', '?')!r} but bubblewrap "
                f"is unavailable (sandbox_fallback=warn-run)",
                level="WARNING",
                context={"agent": agent_name},
            )

    # 10. Run the bounded pool.
    log_dir = rs.run_dir / "dispatch-logs"
    concurrency = int(getattr(args, "concurrency", _DEFAULT_CONCURRENCY))
    timeout_per_target = float(
        getattr(args, "timeout", _DEFAULT_PER_TARGET_TIMEOUT)
    )

    results = execute_targets(
        targets,
        claude=default_backend,
        log_dir=log_dir,
        concurrency=concurrency,
        timeout_per_target=timeout_per_target,
        model=getattr(args, "model", None),
        backends=backends or None,
    )

    # 11. Worktree cleanup vs preservation per node vp7n2krq.
    per_pr_states: dict[str, list[dict]] = {}
    # key -> normalized "VERDICT: detail" line, for the summary (Gap 1).
    outcomes: dict[str, str] = {}
    # Keys whose RESOLVED verdict could not be safely pushed (blocked /
    # push-failed) → these warrant ATTENTION just like a failed process.
    push_problem_keys: list[str] = []
    escalations_dir = rs.run_dir / "escalations"
    for r in results:
        slug, pr, worktree_path, is_clone = target_meta[r.key]
        repo = repo_by_slug[slug]
        # Gap 1: the agent's RESOLVED:/ESCALATED: verdict lives only in its
        # stdout log; lift it so summary.md/state.yaml tell the true story.
        verdict, outcome_line = _parse_agent_outcome(r.stdout_path)
        if outcome_line is not None:
            outcomes[r.key] = outcome_line

        # Least privilege (this.i agpriv8n): gitbulk — never the agent —
        # performs the push, and only on a RESOLVED verdict AND only after
        # independently re-checking the worktree is genuinely safe (the verdict
        # is advisory; a spoofed RESOLVED that left conflict markers or a
        # half-finished rebase is caught here and pushed NOTHING — threat-model
        # §5). The lease is against the head SHA gitbulk first observed, so a
        # concurrent push aborts rather than clobbers.
        push_status: str | None = None
        push_detail: str | None = None
        if verdict == "RESOLVED":
            readiness, detail = verify_resolved_for_push(
                worktree_path, pr.head_sha
            )
            if readiness is PushReadiness.READY:
                try:
                    force_push_with_lease(
                        worktree_path, pr.head_ref, pr.head_sha
                    )
                    push_status, push_detail = "pushed", detail
                except RebaseError as e:
                    push_status = "push-failed"
                    push_detail = e.stderr or str(e)
                    push_problem_keys.append(r.key)
                    rs.record_error(
                        f"push failed for {slug}#{pr.number}: {push_detail}",
                        level="ERROR",
                        context={"slug": slug, "pr": pr.number},
                    )
            elif readiness is PushReadiness.NO_CHANGE:
                push_status, push_detail = "no-change", detail
            else:  # BLOCKED — RESOLVED claimed but worktree says otherwise
                push_status, push_detail = "blocked", detail
                push_problem_keys.append(r.key)
                rs.record_error(
                    f"agent reported RESOLVED but worktree is not pushable "
                    f"for {slug}#{pr.number}: {detail}",
                    level="WARNING",
                    context={"slug": slug, "pr": pr.number},
                )

        # Gap 2: salvage any ESCALATION.md into the run dir BEFORE teardown
        # (a clean escalation aborts the rebase, so the worktree is not
        # in-conflict and would otherwise be removed with the note inside).
        escalation_file = _salvage_escalation(
            worktree_path, escalations_dir, r.key
        )
        in_conflict = is_worktree_in_conflict(worktree_path)
        preserved = False
        if in_conflict:
            preserved = True
            _write_conflict_marker(worktree_path, slug, pr, r)
        else:
            try:
                with repo_lock(
                    slug, "exclusive", timeout=_LOCK_TIMEOUT_SECONDS,
                    subcommand="dispatch",
                ):
                    if is_clone:
                        remove_isolated_clone(
                            worktree_path, worktree_root=policy.worktree_root
                        )
                    else:
                        remove_worktree(repo.local_path, worktree_path)
            except WorktreeError as e:
                # Best-effort: record and leave on disk for the operator.
                preserved = True
                rs.record_error(
                    f"workspace teardown failed for {slug}#{pr.number}: {e}",
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
                "outcome": verdict,
                "outcome_detail": outcome_line,
                "push_status": push_status,
                "push_detail": push_detail,
                "escalation_file": escalation_file,
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

    # 12. Compute exit code. A RESOLVED verdict that could not be safely
    # pushed (blocked / push-failed) is an attention condition too — gitbulk
    # owns the push, so a push it refused or that failed is something the
    # operator must see (this.i agpriv8n).
    attention_results = _attention_results(results)
    if attention_results or push_problem_keys or sandbox_downgraded:
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
        outcomes=outcomes,
    )
    rs.write_summary(summary_md)

    n_resolved = sum(1 for v in outcomes.values() if v.startswith("RESOLVED"))
    n_escalated = sum(1 for v in outcomes.values() if v.startswith("ESCALATED"))
    n_pushed = sum(
        1
        for states in per_pr_states.values()
        for s in states
        if s.get("push_status") == "pushed"
    )
    summary_text = (
        f"dispatched {len(results)} PRs; "
        f"{n_resolved} resolved, {n_escalated} escalated; "
        f"{n_pushed} pushed; "
        f"{len(attention_results) + len(push_problem_keys)} need attention; "
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
        with sentinel_lock(timeout=_LOCK_TIMEOUT_SECONDS, subcommand="dispatch"):
            sentinel.set_attention(exit_code, "dispatch", runid, summary)

    with run_state_lock(
        "dispatch", "exclusive", timeout=_LOCK_TIMEOUT_SECONDS, subcommand="dispatch"
    ):
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
