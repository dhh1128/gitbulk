"""``gitbulk report`` — end-to-end PR triage report.

Wires the four Phase 2 pillars together (invariants framework, gh
client, classifier, RunState) into a single read-only subcommand.

Pipeline (this.i nodes ``scinv4qm``, ``tmlk5pq3``, ``r4nzp7kq``,
``kp7nw4mq``, ``tp4kq2nr``):

  1. load policy + repos (with --code-root override)
  2. optionally refresh org-members cache (--refresh-org-members)
  3. acquire shared global lock with bounded 300s timeout
  4. begin RunState (writes manifest.yaml with inline config snapshot)
  5. run UNIVERSAL preflight; Fail → exit 1
  6. run PER_REPO preflight per repo; Skip drops the repo, Fail aborts
  7. coalesced gh.my_open_prs(...) for surviving repos
  8. run PER_PR invariants per PR
  9. record state.yaml + write summary.md
 10. set ATTENTION sentinel iff exit ∈ {2, 3}
 11. complete RunState (which also prunes runs/ per retain_runs)

Exit-code rule (per design-notes §8 and node ``tp4kq2nr``):
  0  EXIT_OK                 — all clean
  1  EXIT_STRUCTURAL_FAILURE — lock timeout, universal Fail,
                              per-repo Fail, or gh fetch failure
  2  EXIT_ATTENTION_NEEDED   — at least one PR fully passed (i.e.
                              would surface in the triage list)
  3  EXIT_INVARIANT_SKIPPED  — no attention-PRs but at least one
                              repo got Skipped during PER_REPO
  4  EXIT_OVERRIDES_APPLIED  — none of the above but the user
                              passed --skip-check
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path
from typing import Iterable

from gitbulk import paths, sentinel
from gitbulk.config.policy import Policy, load_policy
from gitbulk.config.repos import RepoEntry, load_repos
from gitbulk.gh import GHError, ProductionGHClient
from gitbulk.invariants import (
    InvariantContext,
    get,
    run_chain,
)
from gitbulk.invariants.base import Invariant, InvariantKind
from gitbulk.locks import LockTimeoutError, global_lock
from gitbulk.org_members_cache import refresh_cache
from gitbulk.pr_info import PRInfo
from gitbulk.runstate import RunState
from gitbulk import subcommands as subcommands_mod

# Exit codes — duplicated here (instead of importing from cli.py) so
# the cli ↔ commands dep stays one-way: cli imports commands, never
# the reverse.
EXIT_OK = 0
EXIT_STRUCTURAL_FAILURE = 1
EXIT_ATTENTION_NEEDED = 2
EXIT_INVARIANT_SKIPPED = 3
EXIT_OVERRIDES_APPLIED = 4

#: Per node ``tmlk5pq3``: read-only subcommands get a 300s lock budget.
_LOCK_TIMEOUT_SECONDS: float = 300.0


# ─── Internal helpers ─────────────────────────────────────────────────────


def _partition_chain(
    chain_names: Iterable[str],
) -> tuple[list[type[Invariant]], list[type[Invariant]], list[type[Invariant]]]:
    """Look up each registered name and split by ``InvariantKind``."""
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


def _config_snapshot(policy: Policy, repos_text: str) -> dict:
    """Inline snapshot recorded into manifest.yaml.

    Per node ``kp7nw4mq.a`` the snapshot must be self-contained so a
    later forensic read of the run dir does not require the user's
    ~/.config/gitbulk/ to exist or be unchanged.
    """
    # Re-emit the Policy fields rather than pickling so the YAML is
    # human-grepable. Worktree_root and repo overrides are dataclasses;
    # convert to plain dicts.
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
    }


def _dc_to_dict(obj) -> dict:
    """Flatten a frozen dataclass into a YAML-friendly dict.

    Tuples become lists (PyYAML emits ``!!python/tuple`` otherwise).
    Other scalar types pass through; if a Path ever leaks into one of
    the policy dataclasses, fix the caller — _config_snapshot already
    str()-stringifies the top-level ``worktree_root``.
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
    """Extract the timestamp portion of ``<RUNID>-report``.

    Phase 2: this handler always names its run dirs with the literal
    ``-report`` suffix, so we strip exactly that. A later subcommand
    handler can reuse the helper by parameterizing the suffix.
    """
    name = run_dir.name
    suffix = "-report"
    if name.endswith(suffix):
        return name[: -len(suffix)]
    # Defensive fallback for unknown subcommand suffixes (rebase-onto-
    # default, etc.). Splits on the last '-', which loses information
    # for multi-hyphen subcommands; documented in the test.
    head, _, _ = name.rpartition("-")
    return head


def _build_summary_md(
    policy: Policy,
    all_repos: list[RepoEntry],
    passing_repos: list[RepoEntry],
    skipped_repos: list[tuple[str, str]],
    prs_by_repo: dict[str, list[PRInfo]],
    pr_records_by_repo: dict[str, list[dict]],
    attention_count: int,
) -> str:
    """Human-readable summary.md (this.i tp4kq2nr layer 3)."""
    lines: list[str] = ["# gitbulk report", ""]

    if not all_repos:
        lines.append("No repos configured in repos.txt.")
        lines.append("")
        return "\n".join(lines)

    lines.append(
        f"Configured repos: {len(all_repos)}  "
        f"Reachable: {len(passing_repos)}  "
        f"Skipped: {len(skipped_repos)}  "
        f"PRs needing attention: {attention_count}"
    )
    if policy.humans.org:
        lines.append(f"Humans org: {policy.humans.org}")
    lines.append("")

    if skipped_repos:
        lines.append("## Skipped repos")
        for slug, reason in skipped_repos:
            lines.append(f"- `{slug}` — {reason}")
        lines.append("")

    lines.append("## Open PRs")
    any_pr = False
    for repo in passing_repos:
        repo_prs = prs_by_repo.get(repo.slug, [])
        if not repo_prs:
            continue
        any_pr = True
        lines.append(f"### `{repo.slug}`")
        for pr, record in zip(repo_prs, pr_records_by_repo.get(repo.slug, [])):
            tag = "ATTENTION" if record["invariants_passed"] else "skipped"
            draft = " [DRAFT]" if pr.is_draft else ""
            lines.append(
                f"- #{pr.number}{draft} *{pr.title}* "
                f"by @{pr.author} — base={pr.base_ref} "
                f"checks={pr.checks_status or 'n/a'} "
                f"review={pr.review_decision or 'n/a'} "
                f"({tag})"
            )
            if not record["invariants_passed"] and record["invariants_skips"]:
                for inv_name, reason in record["invariants_skips"]:
                    lines.append(f"    - skip {inv_name}: {reason}")
        lines.append("")
    if not any_pr:
        lines.append("(no open PRs across the reachable repos)")
        lines.append("")
    return "\n".join(lines)


def _read_repos_text() -> str:
    """Return the raw text of repos.txt. Called after ``load_repos``
    has already validated the file exists; a separate exists() check
    here would be dead code."""
    return paths.repos_file().read_text()


# ─── Public handler ───────────────────────────────────────────────────────


def report_handler(args: argparse.Namespace) -> int:
    """Top-level entry for ``gitbulk report``.

    See module docstring for the pipeline. Returns the process exit
    code; the CLI layer's ATTENTION fallback (cli._maybe_set_attention)
    is intentionally redundant — this handler writes its own sentinel
    with richer content for exit codes 2 and 3.
    """
    # 1. Load configuration
    policy = load_policy()
    code_root = Path(args.code_root).expanduser() if args.code_root else None
    repos = load_repos(code_root=code_root)
    repos_text = _read_repos_text()

    # 2. Optional org-members refresh (cmdline gesture; runs BEFORE the
    #    org.members.fresh invariant checks freshness).
    if args.refresh_org_members and policy.humans.org:
        refresh_gh = ProductionGHClient()
        try:
            refresh_cache(refresh_gh, policy.humans.org)
        except GHError as e:
            # Refresh failure is structural; surface to stderr. We do not
            # have a RunState yet, so write to stderr directly.
            print(
                f"gitbulk report: --refresh-org-members failed: {e}",
                file=sys.stderr,
            )
            return EXIT_STRUCTURAL_FAILURE

    # 3. Acquire global lock (shared, 300s timeout per tmlk5pq3). The
    # contextmanager raises LockTimeoutError on __enter__; per tmlk5pq3
    # the timeout is surfaced as exit 1 + stderr message, no ATTENTION
    # sentinel.
    try:
        with global_lock(
            "shared",
            timeout=_LOCK_TIMEOUT_SECONDS,
            subcommand="report",
        ):
            return _run_under_lock(args, policy, repos, repos_text)
    except LockTimeoutError as e:
        print(
            f"gitbulk report: timed out acquiring lock: {e}",
            file=sys.stderr,
        )
        return EXIT_STRUCTURAL_FAILURE


def _run_under_lock(
    args: argparse.Namespace,
    policy: Policy,
    repos: list[RepoEntry],
    repos_text: str,
) -> int:
    """The portion of the pipeline that runs while the lock is held.

    Split out for clarity and so lock-timeout vs in-run errors are
    structurally distinct branches.
    """
    # 4. Begin RunState.
    config_snapshot = _config_snapshot(policy, repos_text)
    rs = RunState.begin(
        "report",
        argv=list(sys.argv),
        config_snapshot=config_snapshot,
    )

    # Build gh client + base context.
    gh = ProductionGHClient()
    ctx_base = InvariantContext(policy=policy, runstate=rs, gh=gh)

    # 5/6/8. Partition the report chain.
    report_sub = subcommands_mod.by_name("report")
    universal, per_repo, per_pr = _partition_chain(report_sub.invariant_chain)

    # Effective skip set: cmdline --skip-check wins per r4nzp7kq.
    skip_list = list(args.skip_check or [])
    skip_set = frozenset(skip_list)
    if skip_list:
        rs.record_error(
            f"--skip-check applied: {sorted(skip_list)}",
            level="WARNING",
            context={"skipped_invariants": sorted(skip_list)},
        )

    # 5. UNIVERSAL preflight.
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
            prs_by_repo={},
            pr_records_by_repo={},
            attention_count=0,
        )

    # 6. PER_REPO preflight. Each repo gets its own context.
    #
    # Skip-vs-skip distinction:
    #   - An INVARIANT Skip (intrinsic — e.g. github.reachable can't
    #     reach the repo) means "drop this repo, count toward exit 3."
    #   - A CMDLINE Skip (--skip-check NAME) means "bypass the
    #     invariant, proceed normally." The audit signal is exit 4
    #     (set elsewhere), not exit 3.
    # We discriminate by checking which invariant names sit in the
    # caller's skip_set.
    skipped_repos: list[tuple[str, str]] = []
    passing_repos: list[RepoEntry] = []
    for repo in repos:
        ctx_repo = replace(ctx_base, repo=repo)
        r = run_chain(
            per_repo, ctx_repo, skip_set=skip_set, target=repo.slug
        )
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
                prs_by_repo={},
                pr_records_by_repo={},
                attention_count=0,
            )
        # Filter out cmdline-driven skips when deciding repo disposition.
        intrinsic_skips = [
            (n, reason) for n, reason in r.skips if n not in skip_set
        ]
        if intrinsic_skips:
            reason = "; ".join(reason for _, reason in intrinsic_skips)
            skipped_repos.append((repo.slug, reason))
        else:
            passing_repos.append(repo)

    # 7. Coalesced PR fetch.
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
                prs_by_repo={},
                pr_records_by_repo={},
                attention_count=0,
            )
    else:
        prs_by_repo = {}

    # 8. PER_PR invariants + structured state.
    attention_count = 0
    pr_records_by_repo: dict[str, list[dict]] = {}
    for repo in passing_repos:
        ctx_repo = replace(ctx_base, repo=repo)
        repo_prs = prs_by_repo.get(repo.slug, [])
        pr_records: list[dict] = []
        for pr in repo_prs:
            ctx_pr = replace(ctx_repo, pr=pr)
            target = f"{repo.slug}#{pr.number}"
            pr_result = run_chain(
                per_pr, ctx_pr, skip_set=skip_set, target=target
            )
            # A PR counts as "needs attention" only if no Fail AND no
            # INTRINSIC skip (skips from --skip-check bypass don't
            # disqualify the PR; they are an audit signal, exit 4).
            intrinsic_pr_skips = [
                (n, reason) for n, reason in pr_result.skips
                if n not in skip_set
            ]
            is_attention = pr_result.passed and not intrinsic_pr_skips
            pr_records.append(
                {
                    "number": pr.number,
                    "title": pr.title,
                    "url": pr.url,
                    "author": pr.author,
                    "state": pr.state,
                    "is_draft": pr.is_draft,
                    "base_ref": pr.base_ref,
                    "head_ref": pr.head_ref,
                    "mergeable_state": pr.mergeable_state,
                    "review_decision": pr.review_decision,
                    "checks_status": pr.checks_status,
                    "labels": list(pr.labels),
                    "invariants_passed": is_attention,
                    "invariants_skips": [
                        list(pair) for pair in pr_result.skips
                    ],
                    "invariants_fail_reason": pr_result.fail_reason,
                }
            )
            if is_attention:
                attention_count += 1
        pr_records_by_repo[repo.slug] = pr_records
        rs.record_repo_state(
            repo.slug,
            {"pr_count": len(repo_prs), "prs": pr_records},
        )

    # 9. Summary markdown.
    summary_md = _build_summary_md(
        policy,
        repos,
        passing_repos,
        skipped_repos,
        prs_by_repo,
        pr_records_by_repo,
        attention_count,
    )
    rs.write_summary(summary_md)

    # 10. Compute exit code.
    if attention_count > 0:
        exit_code = EXIT_ATTENTION_NEEDED
    elif skipped_repos:
        exit_code = EXIT_INVARIANT_SKIPPED
    elif skip_list:
        exit_code = EXIT_OVERRIDES_APPLIED
    else:
        exit_code = EXIT_OK

    summary_text = (
        f"{attention_count} PRs need attention; "
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
        prs_by_repo=prs_by_repo,
        pr_records_by_repo=pr_records_by_repo,
        attention_count=attention_count,
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
    prs_by_repo: dict[str, list[PRInfo]],
    pr_records_by_repo: dict[str, list[dict]],
    attention_count: int,
) -> int:
    """Final-stage write: summary.md (if not already written), sentinel,
    and runstate.complete().

    Called from every terminal branch so that EVERY run gets a finished
    manifest.yaml + a latest-report symlink, even on structural-failure
    paths.
    """
    # If the caller didn't already write a summary (structural-failure
    # branches), synthesize one so `gitbulk show` has something to read.
    summary_path = rs.run_dir / "summary.md"
    if not summary_path.exists():
        synth = _build_summary_md(
            policy,
            all_repos,
            passing_repos,
            skipped_repos,
            prs_by_repo,
            pr_records_by_repo,
            attention_count,
        )
        # Append the failure summary so the operator sees the failure first.
        rs.write_summary(f"# gitbulk report (FAILED)\n\n{summary}\n\n{synth}")

    if attention:
        runid = _runid_from_run_dir(rs.run_dir)
        sentinel.set_attention(exit_code, "report", runid, summary)

    rs.complete(exit_code, retain_runs=policy.defaults.retain_runs)
    return exit_code


__all__ = [
    "EXIT_ATTENTION_NEEDED",
    "EXIT_INVARIANT_SKIPPED",
    "EXIT_OK",
    "EXIT_OVERRIDES_APPLIED",
    "EXIT_STRUCTURAL_FAILURE",
    "report_handler",
]
