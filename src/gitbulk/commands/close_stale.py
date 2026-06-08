"""``gitbulk close-stale`` — warn-then-close PRs that have been inactive.

Pipeline mirrors :mod:`merge`:

  1. Load policy + repos (no clone required).
  2. Acquire global EXCLUSIVE lock with 1800s budget (node ``tmlk5pq3``,
     ``2vqp4nk6``).
  3. RunState.begin("close-stale", ...).
  4. UNIVERSAL preflight; Fail → exit 1.
  5. PER_REPO preflight per repo; Skip drops the repo.
  6. Coalesced ``gh.my_open_prs`` for surviving repos.
  7. PER_PR invariants per PR. The close-stale chain ends with
     ``pr.inactive`` — PRs that PASS the chain are stale candidates.
  8. For each stale candidate: fetch comments, look for the gitbulk
     warning marker (``<!-- gitbulk: stale-warning v1 -->``).
     Bifurcate per the design decided in the close-stale interview:
       - no marker → WARN (post heads-up comment, raises ATTENTION)
       - marker + later PR activity → marker is stale; act as if no
         marker (re-warn if still inactive long enough, else no-op)
       - marker + no later activity + cooloff elapsed → CLOSE
       - marker + no later activity + still in cooloff → WAIT (no-op)
     ``stale_policy=warn-only`` suppresses CLOSE; everything else still
     happens. ``stale_policy=never`` is filtered at ``pr.inactive``.
  9. DRY-RUN GATE: without ``--apply``, list what WOULD happen.
 10. Compute exit code (failures → 2; warnings issued → 2 (ATTENTION);
     skips → 3; --skip-check used → 4; else → 0). Write summary.md,
     state.yaml; set ATTENTION on warns/closes that completed.

Branch deletion on close is OFF (delete_branch=False) per the design
decision: stale-closed PRs are often abandoned-but-recoverable, and
keeping the branch costs ~nothing on GitHub.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from gitbulk import paths, sentinel
from gitbulk.config.policy import Policy, load_policy, policy_for
from gitbulk.config.repos import RepoEntry, SkippedEntry, load_repos
from gitbulk.default_branch_cache import prime_default_branches
from gitbulk.filters import (
    apply_pr_filters,
    fetch_author,
    filter_summary_line,
    resolve_filter_spec,
    select_repos,
)
from gitbulk.gh import GHError, ProductionGHClient
from gitbulk.org_members_cache import (
    OrgMembersRefreshError,
    ensure_org_members_fresh,
)
from gitbulk.commands._common import (
    dc_to_dict,
    partition_chain,
    read_repos_text,
)
from gitbulk.invariants import InvariantContext, run_chain, seed_org_members
from gitbulk.locks import (
    LockTimeoutError,
    repo_lock,
    run_state_lock,
    sentinel_lock,
)
from gitbulk.pr_info import PRComment, PRInfo
from gitbulk.runstate import RunState
from gitbulk.util.progress import Progress
from gitbulk.util.style import error_line, summary_line
from gitbulk import subcommands as subcommands_mod

# Exit codes — duplicated here (instead of importing from cli.py) so
# the cli ↔ commands dep stays one-way.
EXIT_OK = 0
EXIT_STRUCTURAL_FAILURE = 1
EXIT_ATTENTION_NEEDED = 2
EXIT_INVARIANT_SKIPPED = 3
EXIT_OVERRIDES_APPLIED = 4

#: Per node ``tmlk5pq3``: mutating subcommands get a 1800s lock budget.
_LOCK_TIMEOUT_SECONDS: float = 1800.0

#: HTML-comment marker embedded in gitbulk's stale-warning body. The
#: closing handler searches PR comments for this exact substring to
#: recover its own prior warning state without a separate cache file.
#: Versioned (``v1``) so a future warning-body change can be detected.
STALE_WARNING_MARKER: str = "<!-- gitbulk: stale-warning v1 -->"


def _build_warning_body(stale_age_days: int, cooloff_days: int) -> str:
    """The warning comment body posted on first-pass stale PRs.

    Includes :data:`STALE_WARNING_MARKER` so future runs can find it.
    The visible text gives a human reader (the PR author or a watcher)
    enough context to react before the cooloff elapses.
    """
    return (
        f"This PR has been inactive for {stale_age_days}+ days. "
        f"gitbulk will close it in {cooloff_days} days unless there is "
        "new activity (any push, comment, review, or label change resets "
        f"the clock).\n\n{STALE_WARNING_MARKER}"
    )


# ─── Internal helpers ─────────────────────────────────────────────────────


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
    }


def _runid_from_run_dir(run_dir: Path) -> str:
    name = run_dir.name
    suffix = "-close-stale"
    if name.endswith(suffix):
        return name[: -len(suffix)]
    head, _, _ = name.rpartition("-")
    return head


def _find_latest_warning(comments: list[PRComment]) -> PRComment | None:
    """Return the most recent gitbulk warning comment (by ``at``), or None.

    Matches :data:`STALE_WARNING_MARKER` as a substring. The author of
    the comment is intentionally NOT checked — anyone posting the marker
    text would be detected, but the practical risk is nil (HTML comment
    is invisible to normal reviewers and no one types it accidentally).
    """
    warnings = [c for c in comments if STALE_WARNING_MARKER in c.body]
    if not warnings:
        return None
    return max(warnings, key=lambda c: c.at)


def _utc_now() -> datetime:
    """Indirection so tests can monkeypatch the clock."""
    return datetime.now(timezone.utc)


# ─── per-PR decision ──────────────────────────────────────────────────────


def _decide_action(
    pr: PRInfo,
    warning: PRComment | None,
    *,
    stale_age_days: int,
    cooloff_days: int,
    stale_policy: str,
    now: datetime,
) -> str:
    """Return one of: 'warn', 'close', 'wait', 'noop'.

    Pure function of inputs — tests exercise it directly. The handler
    converts the decision into an actual gh call (or a dry-run entry).

    Note: ``pr.inactive`` already filtered out PRs touched within
    ``stale_cooloff_days``. The age threshold for the WARN action is
    ``stale_age_days`` (typically larger), enforced here.

    Decision table:

      warning is None
        AND (now - pr.updated_at) >= stale_age_days → 'warn'
        AND (now - pr.updated_at) <  stale_age_days → 'noop'
          (filtered into close-stale by cooloff threshold, but not
          actually stale by the age threshold yet)

      warning is not None AND pr.updated_at > warning.at  (user came back)
        AND (now - pr.updated_at) >= stale_age_days → 'warn' (re-warn)
        AND (now - pr.updated_at) <  stale_age_days → 'noop'

      warning is not None AND pr.updated_at <= warning.at (no later activity)
        AND (now - warning.at) >= cooloff_days
          → 'close' (or 'wait' if stale_policy=='warn-only')
        AND (now - warning.at) <  cooloff_days
          → 'wait' (still in cooloff)
    """
    age_days = (now - pr.updated_at).days
    if warning is None:
        if age_days >= stale_age_days:
            return "warn"
        return "noop"
    if pr.updated_at > warning.at:
        if age_days >= stale_age_days:
            return "warn"
        return "noop"
    elapsed_days = (now - warning.at).days
    if elapsed_days >= cooloff_days:
        if stale_policy == "warn-only":
            return "wait"
        return "close"
    return "wait"


# ─── Public handler ───────────────────────────────────────────────────────


def close_stale_handler(args: argparse.Namespace) -> int:
    """Top-level entry for ``gitbulk close-stale``."""
    policy = load_policy()
    code_root = (
        Path(args.code_root).expanduser() if args.code_root else None
    )
    repos, skipped_entries = load_repos(code_root=code_root)
    repos_text = read_repos_text()

    # Fleet-subset filter (node flt7arg2): prune repos before the lock.
    spec = resolve_filter_spec(args, policy)
    repos, repos_excluded = select_repos(repos, spec)

    # Resource-scoped locking (node rsclk7nq): no global lock. Caches self-lock
    # in their helpers; each per-PR remote mutation takes repo_lock(slug); the
    # terminal writes take sentinel_lock + run_state_lock("close-stale").
    try:
        return _run_under_lock(
            args, policy, repos, repos_text, skipped_entries,
            spec, repos_excluded,
        )
    except LockTimeoutError as e:
        print(
            error_line(f"gitbulk close-stale: timed out acquiring lock: {e}"),
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
        "close-stale",
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
            actions=[],
            apply=bool(args.apply),
            skipped_entries=skipped_entries,
        )

    # Resolve org-members once now that the cache is fresh; carried through
    # every per-PR context so pr.author_known reads it from memory (node 37ic).
    ctx_base = seed_org_members(ctx_base)

    cs_sub = subcommands_mod.by_name("close-stale")
    universal, per_repo, per_pr = partition_chain(cs_sub.invariant_chain)

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
            summary=(
                f"universal preflight failed: "
                f"{universal_result.fail_reason}"
            ),
            policy=policy,
            attention=False,
            all_repos=repos,
            passing_repos=[],
            skipped_repos=[],
            actions=[],
            apply=bool(args.apply),
            skipped_entries=skipped_entries,
        )

    # PER_REPO preflight.
    skipped_repos: list[tuple[str, str]] = []
    passing_repos: list[RepoEntry] = []
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
                summary=(
                    f"per-repo invariant failed on {repo.slug}: "
                    f"{r.fail_reason}"
                ),
                policy=policy,
                attention=False,
                all_repos=repos,
                passing_repos=passing_repos,
                skipped_repos=skipped_repos,
                actions=[],
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

    # Coalesced PR fetch.
    if passing_repos:
        try:
            # close-stale can act on others' PRs (maintainer flow); the
            # author filter may widen (node flt7arg2), default @me.
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
                actions=[],
                apply=bool(args.apply),
                skipped_entries=skipped_entries,
            )
    else:
        prs_by_repo = {}

    # Apply PR-level filters (base, mergeable_state) before invariants.
    prs_by_repo, prs_excluded = apply_pr_filters(prs_by_repo, spec)
    filter_line = filter_summary_line(spec, repos_excluded, prs_excluded)

    # PER_PR invariants → stale candidates.
    stale_candidates: list[tuple[str, PRInfo]] = []
    pr_skips_by_repo: dict[str, list[dict]] = {}
    for repo in passing_repos:
        ctx_repo = replace(ctx_base, repo=repo)
        for pr in prs_by_repo.get(repo.slug, []):
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
                stale_candidates.append((repo.slug, pr))
            else:
                pr_skips_by_repo.setdefault(repo.slug, []).append(
                    {
                        "number": pr.number,
                        "title": pr.title,
                        "url": pr.url,
                        "skips": [list(p) for p in pr_result.skips],
                        "fail_reason": pr_result.fail_reason,
                    }
                )

    # Resolve each stale candidate to an action.
    now = _utc_now()
    actions: list[dict] = []  # per-candidate decision + (apply) outcome
    for slug, pr in stale_candidates:
        effective = policy_for(policy, slug)
        # Fetch this PR's comments so we can find a prior warning marker.
        try:
            comments = gh.fetch_pr_comments(slug, pr.number)
        except GHError as e:
            rs.record_error(
                f"fetch_pr_comments failed for {slug}#{pr.number}: {e}",
                level="ERROR",
                context={"slug": slug, "pr": pr.number, "error": str(e)},
            )
            actions.append(
                {
                    "slug": slug,
                    "number": pr.number,
                    "title": pr.title,
                    "url": pr.url,
                    "decision": "error",
                    "error": f"fetch_pr_comments: {e}",
                }
            )
            continue
        warning = _find_latest_warning(comments)
        decision = _decide_action(
            pr,
            warning,
            stale_age_days=effective.stale_age_days,
            cooloff_days=effective.stale_cooloff_days,
            stale_policy=effective.stale_policy,
            now=now,
        )
        actions.append(
            {
                "slug": slug,
                "number": pr.number,
                "title": pr.title,
                "url": pr.url,
                "decision": decision,
                "stale_policy": effective.stale_policy,
                "warning_at": warning.at.isoformat() if warning else None,
            }
        )

    # DRY-RUN GATE.
    if not args.apply:
        summary_md = _build_summary_md(
            policy,
            all_repos=repos,
            passing_repos=passing_repos,
            skipped_repos=skipped_repos,
            actions=actions,
            apply=False,
            skipped_entries=skipped_entries,
            filter_line=filter_line,
        )
        rs.write_summary(summary_md)
        warn_or_close = sum(
            1 for a in actions if a["decision"] in ("warn", "close")
        )
        if skipped_repos or skipped_entries:
            exit_code = EXIT_INVARIANT_SKIPPED
            attention = True
        elif skip_list:
            exit_code = EXIT_OVERRIDES_APPLIED
            attention = False
        elif warn_or_close > 0:
            # Dry-run that would take real action → ATTENTION so the
            # user sees it before flipping --apply.
            exit_code = EXIT_ATTENTION_NEEDED
            attention = True
        else:
            exit_code = EXIT_OK
            attention = False
        summary_text = (
            f"dry-run: {warn_or_close} PR(s) would be warned or closed; "
            f"{len(skipped_repos)} repos skipped; "
            f"{len(skipped_entries)} entries skipped"
            + (f"; {filter_line}" if filter_line else "")
        )
        for repo in passing_repos:
            rs.record_repo_state(
                repo.slug,
                _state_for_repo(repo.slug, actions, pr_skips_by_repo),
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
            actions=actions,
            apply=False,
            skip_writing_summary=True,
            skipped_entries=skipped_entries,
        )

    # ── --apply path: execute each decision ──
    failure_count = 0
    warn_count = 0
    close_count = 0
    for action in actions:
        slug = action["slug"]
        number = action["number"]
        decision = action["decision"]
        if decision == "warn":
            effective = policy_for(policy, slug)
            body = _build_warning_body(
                effective.stale_age_days, effective.stale_cooloff_days
            )
            try:
                with repo_lock(
                    slug, "exclusive", timeout=_LOCK_TIMEOUT_SECONDS,
                    subcommand="close-stale",
                ):
                    gh.post_comment(slug, number, body)
            except GHError as e:
                failure_count += 1
                rs.record_error(
                    f"post_comment failed for {slug}#{number}: {e}",
                    level="ERROR",
                    context={"slug": slug, "pr": number, "error": str(e)},
                )
                action["error"] = f"post_comment: {e}"
                continue
            action["applied"] = True
            warn_count += 1
        elif decision == "close":
            try:
                with repo_lock(
                    slug, "exclusive", timeout=_LOCK_TIMEOUT_SECONDS,
                    subcommand="close-stale",
                ):
                    gh.close_pr(slug, number, delete_branch=False)
            except GHError as e:
                failure_count += 1
                rs.record_error(
                    f"close_pr failed for {slug}#{number}: {e}",
                    level="ERROR",
                    context={"slug": slug, "pr": number, "error": str(e)},
                )
                action["error"] = f"close_pr: {e}"
                continue
            action["applied"] = True
            close_count += 1
        elif decision == "error":
            # Already recorded during fetch_pr_comments; just count.
            failure_count += 1
        # 'wait' and 'noop' are no-ops on --apply

    for repo in passing_repos:
        rs.record_repo_state(
            repo.slug,
            _state_for_repo(repo.slug, actions, pr_skips_by_repo),
        )

    # Compute exit code.
    if failure_count > 0:
        exit_code = EXIT_ATTENTION_NEEDED
        attention = True
    elif skipped_repos or skipped_entries:
        exit_code = EXIT_INVARIANT_SKIPPED
        attention = True
    elif skip_list:
        exit_code = EXIT_OVERRIDES_APPLIED
        attention = False
    elif warn_count > 0:
        # A warning was issued → ATTENTION so the user knows their PR
        # is on the close countdown.
        exit_code = EXIT_ATTENTION_NEEDED
        attention = True
    else:
        exit_code = EXIT_OK
        attention = False

    summary_md = _build_summary_md(
        policy,
        all_repos=repos,
        passing_repos=passing_repos,
        skipped_repos=skipped_repos,
        actions=actions,
        apply=True,
        skipped_entries=skipped_entries,
        filter_line=filter_line,
    )
    rs.write_summary(summary_md)

    summary_text = (
        f"warned {warn_count}, closed {close_count}, "
        f"{failure_count} failed; {len(skipped_repos)} repos skipped; "
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
        actions=actions,
        apply=True,
        skip_writing_summary=True,
        skipped_entries=skipped_entries,
    )


def _build_summary_md(
    policy: Policy,
    *,
    all_repos: list[RepoEntry],
    passing_repos: list[RepoEntry],
    skipped_repos: list[tuple[str, str]],
    actions: list[dict],
    apply: bool,
    skipped_entries: list[SkippedEntry] | None = None,
    filter_line: str | None = None,
) -> str:
    lines: list[str] = ["# gitbulk close-stale", ""]
    mode = "APPLY" if apply else "DRY-RUN"
    lines.append(f"Mode: **{mode}**")
    lines.append(f"Default stale policy: `{policy.defaults.stale_policy}`")
    if filter_line:
        lines.append(filter_line)
    lines.append(
        f"Configured repos: {len(all_repos)}  "
        f"Reachable: {len(passing_repos)}  "
        f"Skipped: {len(skipped_repos)}  "
        f"Candidates: {len(actions)}"
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
            lines.append(
                f"- line {entry.lineno} (`{entry.raw}`): {entry.reason}"
            )
        lines.append("")

    if not actions:
        lines.append("(no stale candidates)")
        lines.append("")
        return "\n".join(lines)

    # Group by decision for readability.
    by_decision: dict[str, list[dict]] = {}
    for a in actions:
        by_decision.setdefault(a["decision"], []).append(a)
    section_order = ("close", "warn", "wait", "error", "noop")
    section_headers = {
        "close": "## Would close" if not apply else "## Closed",
        "warn": "## Would warn" if not apply else "## Warned",
        "wait": "## Waiting (in cooloff)",
        "error": "## Errors",
        "noop": "## No action",
    }
    for key in section_order:
        rows = by_decision.get(key, [])
        if not rows:
            continue
        lines.append(section_headers[key])
        for a in rows:
            extra = ""
            if apply:
                if a.get("applied"):
                    extra = ""
                elif "error" in a:
                    extra = f" — FAILED: {a['error']}"
            if key == "wait" and a.get("warning_at"):
                extra = f" (warned {a['warning_at']})"
            lines.append(
                f"- `{a['slug']}` #{a['number']} *{a['title']}*{extra}"
            )
        lines.append("")
    return "\n".join(lines)


def _state_for_repo(
    slug: str,
    actions: list[dict],
    pr_skips_by_repo: dict[str, list[dict]],
) -> dict:
    """Per-repo entry recorded into state.yaml."""
    prs_payload: list[dict] = []
    for a in actions:
        if a["slug"] != slug:
            continue
        entry: dict = {
            "number": a["number"],
            "title": a["title"],
            "url": a["url"],
            "decision": a["decision"],
        }
        if a.get("warning_at"):
            entry["warning_at"] = a["warning_at"]
        if "applied" in a:
            entry["applied"] = a["applied"]
        if "error" in a:
            entry["error"] = a["error"]
        prs_payload.append(entry)
    for record in pr_skips_by_repo.get(slug, []):
        prs_payload.append(
            {
                "number": record["number"],
                "title": record["title"],
                "url": record["url"],
                "decision": "skipped-by-invariant",
                "skips": record["skips"],
                "fail_reason": record.get("fail_reason"),
            }
        )
    return {"pr_count": len(prs_payload), "prs": prs_payload}


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
    actions: list[dict],
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
            actions=actions,
            apply=apply,
            skipped_entries=skipped_entries,
        )
        rs.write_summary(f"# gitbulk close-stale (FAILED)\n\n{summary}\n\n{synth}")

    if attention:
        runid = _runid_from_run_dir(rs.run_dir)
        with sentinel_lock(timeout=_LOCK_TIMEOUT_SECONDS, subcommand="close-stale"):
            sentinel.set_attention(exit_code, "close-stale", runid, summary)

    with run_state_lock(
        "close-stale", "exclusive", timeout=_LOCK_TIMEOUT_SECONDS,
        subcommand="close-stale",
    ):
        rs.complete(exit_code, retain_runs=policy.defaults.retain_runs)

    # One-line stdout summary; see report._finish for the rationale.
    print(summary_line(f"gitbulk close-stale: {summary}. View: gitbulk show close-stale", exit_code))
    return exit_code


__all__ = [
    "EXIT_ATTENTION_NEEDED",
    "EXIT_INVARIANT_SKIPPED",
    "EXIT_OK",
    "EXIT_OVERRIDES_APPLIED",
    "EXIT_STRUCTURAL_FAILURE",
    "STALE_WARNING_MARKER",
    "close_stale_handler",
]
