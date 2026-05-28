"""``gitbulk merge`` — auto-merge PRs that satisfy the per-repo merge policy.

Phase 5's first mutating subcommand. Pipeline mirrors :mod:`dispatch`:

  1. Load policy + repos (no clone required for merge).
  2. Acquire global EXCLUSIVE lock with 1800s budget (node ``tmlk5pq3``,
     ``2vqp4nk6``).
  3. RunState.begin("merge", ...).
  4. UNIVERSAL preflight; Fail → exit 1.
  5. PER_REPO preflight per repo; Skip drops the repo.
  6. Coalesced ``gh.my_open_prs`` for surviving repos.
  7. PER_PR invariants per PR. The merge chain includes both the standard
     gh-touching baseline (base_is_default, author_known) and the four
     Phase-5 merge-only invariants (mergeable_state_clean,
     required_checks_green, approved_per_policy, age_threshold). PRs
     that PASS the full chain enter the ``eligible_prs`` list.
  8. DRY-RUN GATE: without ``--apply``, write a summary listing what
     WOULD merge and exit 0 / 3 / 4 per the same exit-code ladder.
  9. (``--apply`` path) For each eligible PR, call
     ``gh.merge_pr(slug, number, method=policy_for(slug).merge_method,
     delete_branch=True)``. The default merge method is ``merge`` (true
     merge commit, this.i node ``gji4dyze``); per-repo override via
     ``repos.<slug>.merge_method`` lets individual repos opt for
     ``squash`` or ``rebase``.
 10. Compute exit code (failures → 2; skips → 3; --skip-check used → 4;
     else → 0). Write summary.md, state.yaml; set ATTENTION on 2/3.

The handler does NOT touch local clones in any way (per AGENTS.md
"Local-git safety contract"). All merge work happens through the gh
network boundary; ``gh pr merge`` is the only mutating operation.
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
from gitbulk.gh import GHError, ProductionGHClient
from gitbulk.invariants import InvariantContext, get, run_chain
from gitbulk.invariants.base import Invariant, InvariantKind
from gitbulk.locks import LockTimeoutError, global_lock
from gitbulk.pr_info import PRInfo
from gitbulk.runstate import RunState
from gitbulk.util.progress import Progress
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


# ─── Internal helpers ─────────────────────────────────────────────────────


def _partition_chain(
    chain_names: Iterable[str],
) -> tuple[list[type[Invariant]], list[type[Invariant]], list[type[Invariant]]]:
    """Look up each registered name and split by ``InvariantKind``.

    Identical to the helper in dispatch.py / report.py; kept local so
    the merge handler stays standalone (see the parallel docstring in
    dispatch._partition_chain for the rationale).
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
    policy: Policy, repos_text: str, args: argparse.Namespace
) -> dict:
    """Inline manifest snapshot. Records ``apply`` so a forensic reader
    can tell a dry-run from an apply run.
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
        "apply": bool(getattr(args, "apply", False)),
        "merge_method_default": policy.defaults.merge_method,
    }


def _dc_to_dict(obj) -> dict:
    """Flatten a frozen dataclass into a YAML-friendly dict."""
    from dataclasses import asdict

    out: dict = {}
    for k, v in asdict(obj).items():
        if isinstance(v, tuple):
            out[k] = list(v)
        else:
            out[k] = v
    return out


def _runid_from_run_dir(run_dir: Path) -> str:
    """Extract the timestamp portion of ``<RUNID>-merge``."""
    name = run_dir.name
    suffix = "-merge"
    if name.endswith(suffix):
        return name[: -len(suffix)]
    head, _, _ = name.rpartition("-")
    return head


def _read_repos_text() -> str:
    return paths.repos_file().read_text()


def _build_summary_md(
    policy: Policy,
    *,
    all_repos: list[RepoEntry],
    passing_repos: list[RepoEntry],
    skipped_repos: list[tuple[str, str]],
    eligible_prs: list[tuple[str, PRInfo]],
    merge_results: list[dict] | None,
    apply: bool,
    deferred_prs: list[tuple[str, PRInfo]] | None = None,
    skipped_entries: list[SkippedEntry] | None = None,
) -> str:
    """Human-readable summary.md.

    Two shapes:
      - dry-run: lists actionable PRs (what WOULD merge) + deferred.
      - apply: lists actionable PRs with per-PR outcome + deferred.

    ``eligible_prs`` here is the actionable subset (post-guardrail);
    ``deferred_prs`` are gate-passing PRs that the one-merge-per-repo
    guardrail postponed to the next gitbulk run.
    """
    deferred_prs = deferred_prs or []
    lines: list[str] = ["# gitbulk merge", ""]
    mode = "APPLY" if apply else "DRY-RUN"
    lines.append(f"Mode: **{mode}**")
    lines.append(f"Default merge method: `{policy.defaults.merge_method}`")
    deferred_note = (
        f"  Deferred (same-repo guardrail): {len(deferred_prs)}"
        if deferred_prs
        else ""
    )
    lines.append(
        f"Configured repos: {len(all_repos)}  "
        f"Reachable: {len(passing_repos)}  "
        f"Skipped: {len(skipped_repos)}  "
        f"Eligible PRs: {len(eligible_prs)}{deferred_note}"
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

    if not eligible_prs and not deferred_prs:
        lines.append("(no eligible PRs to merge)")
        lines.append("")
        return "\n".join(lines)

    # Invariant: eligible_prs is non-empty here. The early return above
    # handles the both-empty case; deferred_prs can only be non-empty if
    # eligible_prs is too (the guardrail defers second-and-later PRs in
    # a slug, never the first). The assertion would fire if a caller
    # bypassed the guardrail and built deferred_prs without primaries —
    # cheaper than carrying defensive code in the common path.
    assert eligible_prs, "deferred_prs without eligible_prs violates guardrail invariant"

    if not apply:
        lines.append("## Would merge")
        for slug, pr in eligible_prs:
            method = policy_for(policy, slug).merge_method
            method_note = (
                "" if method == policy.defaults.merge_method
                else f" [method=`{method}`]"
            )
            lines.append(
                f"- `{slug}` #{pr.number} *{pr.title}* "
                f"(head={pr.head_ref}@{pr.head_sha[:7]}){method_note}"
            )
    else:
        by_key = {(r["slug"], r["number"]): r for r in (merge_results or [])}
        lines.append("## Merge results")
        for slug, pr in eligible_prs:
            record = by_key.get((slug, pr.number))
            if record is None:
                status = "no result recorded"
            elif record["merged"]:
                status = "merged"
            else:
                status = f"FAILED: {record.get('error', '?')}"
            lines.append(
                f"- `{slug}` #{pr.number} *{pr.title}* — {status}"
            )
    lines.append("")

    if deferred_prs:
        lines.append("## Deferred to next run (same-repo guardrail)")
        for slug, pr in deferred_prs:
            lines.append(
                f"- `{slug}` #{pr.number} *{pr.title}*"
            )
        lines.append("")

    return "\n".join(lines)


# ─── Public handler ───────────────────────────────────────────────────────


def merge_handler(args: argparse.Namespace) -> int:
    """Top-level entry for ``gitbulk merge``."""
    policy = load_policy()
    code_root = (
        Path(args.code_root).expanduser() if args.code_root else None
    )
    repos, skipped_entries = load_repos(code_root=code_root)
    repos_text = _read_repos_text()

    try:
        with global_lock(
            "exclusive",
            timeout=_LOCK_TIMEOUT_SECONDS,
            subcommand="merge",
        ):
            return _run_under_lock(
                args, policy, repos, repos_text, skipped_entries
            )
    except LockTimeoutError as e:
        print(
            f"gitbulk merge: timed out acquiring lock: {e}",
            file=sys.stderr,
        )
        return EXIT_STRUCTURAL_FAILURE


def _run_under_lock(
    args: argparse.Namespace,
    policy: Policy,
    repos: list[RepoEntry],
    repos_text: str,
    skipped_entries: list[SkippedEntry],
) -> int:
    """Pipeline body that runs while the global EXCLUSIVE lock is held."""
    config_snapshot = _config_snapshot(policy, repos_text, args)
    rs = RunState.begin(
        "merge",
        argv=list(sys.argv),
        config_snapshot=config_snapshot,
    )

    gh = ProductionGHClient()
    ctx_base = InvariantContext(policy=policy, runstate=rs, gh=gh)

    merge_sub = subcommands_mod.by_name("merge")
    universal, per_repo, per_pr = _partition_chain(merge_sub.invariant_chain)

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
            eligible_prs=[],
            merge_results=None,
            apply=bool(args.apply),
            skipped_entries=skipped_entries,
        )

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
                merge_results=None,
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
            prs_by_repo = gh.my_open_prs([r.slug for r in passing_repos])
        except GHError as e:
            rs.record_error(f"my_open_prs failed: {e}")
            return _finish(
                rs,
                EXIT_STRUCTURAL_FAILURE,
                summary=f"gh PR fetch failed: {e}",
                policy=policy,
                attention=False,
                skipped_entries=skipped_entries,
                all_repos=repos,
                passing_repos=passing_repos,
                skipped_repos=skipped_repos,
                eligible_prs=[],
                merge_results=None,
                apply=bool(args.apply),
            )
    else:
        prs_by_repo = {}

    # PER_PR invariants → eligible_prs.
    eligible_prs: list[tuple[str, PRInfo]] = []
    pr_skips_by_repo: dict[str, list[dict]] = {}
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
            else:
                pr_skips_by_repo.setdefault(repo.slug, []).append(
                    {
                        "number": pr.number,
                        "title": pr.title,
                        "url": pr.url,
                        "skips": [
                            list(pair) for pair in pr_result.skips
                        ],
                        "fail_reason": pr_result.fail_reason,
                    }
                )

    # One-merge-per-repo-per-run guardrail. Same-repo PRs targeting the
    # same base can have domino effects on each other: merging A often
    # makes B DIRTY (sibling conflict) and, on repos that dismiss-stale-
    # approvals, also drops B's review_decision. Doing more than one in
    # a single tick would mean acting on state we already know is about
    # to change. Defer second-and-later PRs in any repo to the next
    # gitbulk run, when GitHub has settled mergeable_state and any stale
    # approvals have been dismissed for real. Tie-break: lowest PR number
    # wins (oldest, most likely to have been ready longest).
    eligible_prs.sort(key=lambda t: (t[0], t[1].number))
    seen_slugs: set[str] = set()
    primary_eligible_prs: list[tuple[str, PRInfo]] = []
    deferred_prs: list[tuple[str, PRInfo]] = []
    for slug, pr in eligible_prs:
        if slug in seen_slugs:
            deferred_prs.append((slug, pr))
        else:
            seen_slugs.add(slug)
            primary_eligible_prs.append((slug, pr))

    # DRY-RUN GATE.
    if not args.apply:
        summary_md = _build_summary_md(
            policy,
            all_repos=repos,
            passing_repos=passing_repos,
            skipped_repos=skipped_repos,
            eligible_prs=primary_eligible_prs,
            merge_results=None,
            apply=False,
            deferred_prs=deferred_prs,
            skipped_entries=skipped_entries,
        )
        rs.write_summary(summary_md)
        if skipped_repos or skipped_entries:
            exit_code = EXIT_INVARIANT_SKIPPED
            attention = True
        elif skip_list:
            exit_code = EXIT_OVERRIDES_APPLIED
            attention = False
        else:
            exit_code = EXIT_OK
            attention = False
        summary_text = (
            f"dry-run: {len(primary_eligible_prs)} PRs would merge; "
            f"{len(deferred_prs)} deferred; "
            f"{len(skipped_repos)} repos skipped; "
            f"{len(skipped_entries)} entries skipped"
        )
        # Record per-repo summary state in state.yaml so `gitbulk show
        # merge --state` is informative on dry runs too. Include
        # primary + deferred so the record reflects all gate-passing PRs.
        for slug, pr in primary_eligible_prs + deferred_prs:
            rs.record_repo_state(
                slug,
                _state_for_repo(
                    slug,
                    primary_eligible_prs + deferred_prs,
                    pr_skips_by_repo,
                    [],
                    apply=False,
                ),
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
            eligible_prs=primary_eligible_prs,
            merge_results=None,
            apply=False,
            skip_writing_summary=True,
            deferred_prs=deferred_prs,
            skipped_entries=skipped_entries,
        )

    # ── --apply path ──
    merge_results: list[dict] = []
    failure_count = 0
    for slug, pr in primary_eligible_prs:
        method = policy_for(policy, slug).merge_method
        try:
            response = gh.merge_pr(
                slug,
                pr.number,
                method=method,
                delete_branch=True,
            )
        except GHError as e:
            failure_count += 1
            rs.record_error(
                f"merge_pr failed for {slug}#{pr.number}: {e}",
                level="ERROR",
                context={
                    "slug": slug,
                    "pr": pr.number,
                    "error": str(e),
                },
            )
            merge_results.append(
                {
                    "slug": slug,
                    "number": pr.number,
                    "title": pr.title,
                    "url": pr.url,
                    "head_sha": pr.head_sha,
                    "merged": False,
                    "error": str(e),
                    "method": method,
                }
            )
            # CONTINUE — the other PRs still deserve a shot. A single
            # un-mergeable PR must not block the whole run.
            continue
        # Capture the resulting merge commit SHA via a follow-up gh call.
        # Failure here is non-fatal: the merge succeeded, we just won't
        # be able to surface its CD status in the post-merge watchdog.
        merge_commit_sha: str | None = None
        try:
            merge_commit_sha = gh.fetch_merge_commit_sha(slug, pr.number)
        except GHError as e:
            rs.record_error(
                f"fetch_merge_commit_sha failed for {slug}#{pr.number}: {e}",
                level="WARNING",
                context={"slug": slug, "pr": pr.number, "error": str(e)},
            )
        merge_results.append(
            {
                "slug": slug,
                "number": pr.number,
                "title": pr.title,
                "url": pr.url,
                "head_sha": pr.head_sha,
                "merge_commit_sha": merge_commit_sha,
                "merged": True,
                "method": method,
                "response": response,
            }
        )

    # Record per-repo state. Include primary + deferred so the record
    # reflects all gate-passing PRs, not just the one that fired.
    all_actionable = primary_eligible_prs + deferred_prs
    for repo in passing_repos:
        repo_results = [r for r in merge_results if r["slug"] == repo.slug]
        repo_skips = pr_skips_by_repo.get(repo.slug, [])
        if repo_results or repo_skips or any(
            slug == repo.slug for slug, _ in all_actionable
        ):
            rs.record_repo_state(
                repo.slug,
                _state_for_repo(
                    repo.slug,
                    all_actionable,
                    pr_skips_by_repo,
                    repo_results,
                    apply=True,
                ),
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
    else:
        exit_code = EXIT_OK
        attention = False

    summary_md = _build_summary_md(
        policy,
        all_repos=repos,
        passing_repos=passing_repos,
        skipped_repos=skipped_repos,
        eligible_prs=primary_eligible_prs,
        merge_results=merge_results,
        apply=True,
        deferred_prs=deferred_prs,
        skipped_entries=skipped_entries,
    )
    rs.write_summary(summary_md)

    summary_text = (
        f"merged {len(merge_results) - failure_count} of "
        f"{len(primary_eligible_prs)} primary-eligible PRs; "
        f"{failure_count} failed; "
        f"{len(deferred_prs)} deferred; "
        f"{len(skipped_repos)} repos skipped; "
        f"{len(skipped_entries)} entries skipped"
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
        eligible_prs=primary_eligible_prs,
        merge_results=merge_results,
        apply=True,
        skip_writing_summary=True,
        deferred_prs=deferred_prs,
        skipped_entries=skipped_entries,
    )


def _state_for_repo(
    slug: str,
    eligible_prs: list[tuple[str, PRInfo]],
    pr_skips_by_repo: dict[str, list[dict]],
    repo_merge_results: list[dict],
    *,
    apply: bool,
) -> dict:
    """Build the per-repo entry recorded into state.yaml.

    Combines eligible-PR identity, per-PR skip records (for PRs that
    didn't qualify), and merge results (when ``apply``). Stable shape
    so ``gitbulk show merge --state`` is grep-friendly.
    """
    prs_payload: list[dict] = []
    # Detect which entries are deferred via the one-merge-per-repo
    # guardrail: a same-slug PR that appears AFTER another same-slug PR
    # in eligible_prs is deferred. (eligible_prs is sorted by (slug, num).)
    seen_in_slug: set[int] = set()
    deferred_numbers: set[int] = set()
    for s, pr in eligible_prs:
        if s != slug:
            continue
        if seen_in_slug:
            deferred_numbers.add(pr.number)
        seen_in_slug.add(pr.number)
    for s, pr in eligible_prs:
        if s != slug:
            continue
        entry: dict = {
            "number": pr.number,
            "title": pr.title,
            "url": pr.url,
            "head_sha": pr.head_sha,
            "eligible": True,
        }
        if pr.number in deferred_numbers:
            entry["deferred"] = "same-repo guardrail (one merge per repo per run)"
        elif apply:
            match = next(
                (r for r in repo_merge_results if r["number"] == pr.number),
                None,
            )
            if match is not None:
                entry["merged"] = match["merged"]
                if not match["merged"]:
                    entry["error"] = match.get("error", "")
                if match.get("merge_commit_sha"):
                    entry["merge_commit_sha"] = match["merge_commit_sha"]
        prs_payload.append(entry)
    for record in pr_skips_by_repo.get(slug, []):
        prs_payload.append(
            {
                "number": record["number"],
                "title": record["title"],
                "url": record["url"],
                "eligible": False,
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
    eligible_prs: list[tuple[str, PRInfo]],
    merge_results: list[dict] | None,
    apply: bool,
    skip_writing_summary: bool = False,
    deferred_prs: list[tuple[str, PRInfo]] | None = None,
    skipped_entries: list[SkippedEntry] | None = None,
) -> int:
    """Terminal-stage write: summary.md (if not already), sentinel,
    runstate.complete().
    """
    if not skip_writing_summary:
        synth = _build_summary_md(
            policy,
            all_repos=all_repos,
            passing_repos=passing_repos,
            skipped_repos=skipped_repos,
            eligible_prs=eligible_prs,
            merge_results=merge_results,
            apply=apply,
            deferred_prs=deferred_prs,
            skipped_entries=skipped_entries,
        )
        rs.write_summary(f"# gitbulk merge (FAILED)\n\n{summary}\n\n{synth}")

    if attention:
        runid = _runid_from_run_dir(rs.run_dir)
        sentinel.set_attention(exit_code, "merge", runid, summary)

    rs.complete(exit_code, retain_runs=policy.defaults.retain_runs)

    # One-line stdout summary so the user knows what happened without
    # having to know about ~/.cache/gitbulk/runs/. See report._finish
    # for the rationale.
    print(f"gitbulk merge: {summary}. View: gitbulk show merge")
    return exit_code


__all__ = [
    "EXIT_ATTENTION_NEEDED",
    "EXIT_INVARIANT_SKIPPED",
    "EXIT_OK",
    "EXIT_OVERRIDES_APPLIED",
    "EXIT_STRUCTURAL_FAILURE",
    "merge_handler",
]
