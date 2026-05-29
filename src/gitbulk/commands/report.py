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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import yaml

from gitbulk import paths, sentinel
from gitbulk.config.policy import Policy, load_policy
from gitbulk.config.repos import RepoEntry, SkippedEntry, load_repos
from gitbulk.default_branch_cache import prime_default_branches
from gitbulk.filters import (
    apply_pr_filters,
    fetch_author,
    filter_summary_line,
    resolve_filter_spec,
    select_repos,
)
from gitbulk.gh import GHClient, GHError, ProductionGHClient
from gitbulk.invariants import (
    InvariantContext,
    get,
    run_chain,
)
from gitbulk.invariants.base import Invariant, InvariantKind
from gitbulk.locks import LockTimeoutError, global_lock
from gitbulk.org_members_cache import refresh_cache
from gitbulk.pr_info import CheckRun, PRInfo
from gitbulk.runstate import RunState
from gitbulk.util.progress import Progress
from gitbulk.watchdog_ack import load_acked, record_ack
from gitbulk import subcommands as subcommands_mod

#: Check-run conclusions that count as failures the user should see.
#: ``neutral`` is excluded (often a no-op CI rerun). ``skipped`` excluded
#: too (intentional). Everything else that's "completed and not green"
#: counts. ``status != completed`` (e.g. in_progress) is not a failure
#: yet — surface separately as "still running" if at all.
_FAILURE_CONCLUSIONS: frozenset[str] = frozenset(
    {"failure", "cancelled", "timed_out", "action_required", "stale"}
)

#: Check-run conclusions that count as "green" for the ack cache. A
#: merge is acked-and-forgotten only when every check is completed AND
#: every conclusion is in this set.
_PASSING_CONCLUSIONS: frozenset[str] = frozenset(
    {"success", "skipped", "neutral"}
)

#: Window for the post-merge watchdog: how far back to scan run-state.
#: 24 hours covers nightly cron + a comfortable margin. Bounded so the
#: watchdog doesn't unboundedly accumulate (state pruning per
#: ``retain_runs`` will eventually drop older runs anyway).
_WATCHDOG_WINDOW = timedelta(hours=24)

#: Cap on number of merge SHAs to check per run, defensive against an
#: unexpectedly long history. Each check costs one gh REST call.
_WATCHDOG_MAX_MERGES = 50

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
    watchdog_records: list[dict] | None = None,
    skipped_entries: list[SkippedEntry] | None = None,
    filter_line: str | None = None,
) -> str:
    """Human-readable summary.md (this.i tp4kq2nr layer 3)."""
    watchdog_records = watchdog_records or []
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
    if filter_line:
        lines.append(filter_line)
    if policy.humans.org:
        lines.append(f"Humans org: {policy.humans.org}")
    lines.append("")

    # Post-merge watchdog: surface CD failures BEFORE the open-PR list,
    # because they represent breakage from yesterday's merges and the
    # user likely cares about them first.
    if watchdog_records:
        lines.append("## Recent merges (last 24h)")
        for rec in watchdog_records:
            sha7 = rec["merge_commit_sha"][:7] if rec.get("merge_commit_sha") else "?"
            if rec.get("error"):
                tag = f"check-fetch FAILED: {rec['error']}"
            elif rec.get("has_failure"):
                names = ", ".join(c.name for c in rec.get("failures", []))
                tag = f"FAILING checks: {names}"
            else:
                tag = "checks OK"
            lines.append(
                f"- `{rec['slug']}` #{rec['number']} *{rec['title']}* "
                f"(merge={sha7}) — {tag}"
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

    # Open PRs — flat, one fully self-describing line per PR, sorted by
    # (repo, number). No per-repo ### headers: the grouping made it hard
    # to grep across repo + status + number combinations. Each line
    # carries the URL (which encodes repo + number), the structured
    # fields, a STATUS tag, and the title last. So:
    #   grep ATTENTION          → the triage subset
    #   grep checks=FAILURE     → red CI
    #   grep provenant-dev/     → one org
    #   grep 'base=dev'         → PRs targeting dev
    # all work without the headers getting in the way.
    flat: list[tuple[PRInfo, dict]] = []
    for repo in passing_repos:
        repo_prs = prs_by_repo.get(repo.slug, [])
        records = pr_records_by_repo.get(repo.slug, [])
        for pr, record in zip(repo_prs, records):
            flat.append((pr, record))
    flat.sort(key=lambda pair: (pair[0].slug, pair[0].number))

    lines.append(f"## Open PRs ({len(flat)})")
    if not flat:
        lines.append("(no open PRs across the reachable repos)")
        lines.append("")
        return "\n".join(lines)
    for pr, record in flat:
        if record["invariants_passed"]:
            status = "ATTENTION"
        else:
            skips = record.get("invariants_skips") or []
            status = (
                "SKIP(" + "; ".join(f"{n}: {r}" for n, r in skips) + ")"
                if skips
                else "SKIP"
            )
        draft = " [DRAFT]" if pr.is_draft else ""
        lines.append(
            f"{pr.url}  "
            f"base={pr.base_ref} "
            f"checks={pr.checks_status or 'n/a'} "
            f"review={pr.review_decision or 'n/a'} "
            f"mergeable={pr.mergeable_state or 'n/a'}{draft}  "
            f"{status}  — {pr.title}"
        )
    lines.append("")
    return "\n".join(lines)


def _scan_recent_merges(now: datetime) -> list[dict]:
    """Return per-PR merge records from merge runs in the last 24 hours.

    Walks ``~/.cache/gitbulk/runs/<TIMESTAMP>-merge/state.yaml`` for each
    timestamp within the window. The TIMESTAMP prefix is the canonical
    sort key (lexicographic == chronological because it's ISO-8601).

    Returns a list of dicts with keys: ``slug``, ``number``, ``title``,
    ``url``, ``merge_commit_sha``, ``run_id``. Drops entries that lack
    a ``merge_commit_sha`` (dry-runs and pre-watchdog merges have none).
    """
    runs_root = paths.runs_dir()
    if not runs_root.exists():
        return []
    cutoff = now - _WATCHDOG_WINDOW
    merges: list[dict] = []
    # Descending so the most recent runs are scanned first; the cap is
    # enforced by truncating the result list.
    candidate_dirs = sorted(
        (p for p in runs_root.iterdir() if p.is_dir() and p.name.endswith("-merge")),
        reverse=True,
    )
    for run_dir in candidate_dirs:
        # Timestamp prefix is everything before "-merge", format
        # YYYYMMDDTHHMMSSZ. Parse defensively — a non-conforming dir
        # name is silently skipped.
        ts_str = run_dir.name[: -len("-merge")]
        try:
            run_at = datetime.strptime(ts_str, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        if run_at < cutoff:
            # Older than the window → and since we're walking newest
            # first, everything older lives after this in iteration
            # order. Break early.
            break
        state_path = run_dir / "state.yaml"
        if not state_path.exists():
            continue
        try:
            doc = yaml.safe_load(state_path.read_text())
        except yaml.YAMLError:
            continue
        if not isinstance(doc, dict):
            continue
        for slug, repo_payload in (doc.get("repos") or {}).items():
            if not isinstance(repo_payload, dict):
                continue
            for pr_entry in repo_payload.get("prs") or []:
                if not isinstance(pr_entry, dict):
                    continue
                sha = pr_entry.get("merge_commit_sha")
                if not sha:
                    continue
                merges.append(
                    {
                        "slug": slug,
                        "number": pr_entry.get("number"),
                        "title": pr_entry.get("title") or "",
                        "url": pr_entry.get("url") or "",
                        "merge_commit_sha": sha,
                        "run_id": ts_str,
                    }
                )
                if len(merges) >= _WATCHDOG_MAX_MERGES:
                    return merges
    return merges


def _serialize_watchdog_record(record: dict) -> dict:
    """Flatten a watchdog record (containing CheckRun dataclass instances)
    into a YAML-friendly dict shape for state.yaml.

    Drops ``check_runs`` and ``failures`` from the unfailing case (the
    record's ``has_failure`` flag is the load-bearing signal); for
    failing or errored records, include the failing check details so
    the LLM prompt can name them in its triage output.
    """
    out: dict = {
        "slug": record["slug"],
        "number": record.get("number"),
        "title": record.get("title", ""),
        "url": record.get("url", ""),
        "merge_commit_sha": record["merge_commit_sha"],
        "run_id": record.get("run_id"),
        "has_failure": record.get("has_failure", False),
    }
    if record.get("error"):
        out["error"] = record["error"]
    failures = record.get("failures") or []
    if failures:
        out["failing_checks"] = [
            {"name": c.name, "details_url": c.details_url}
            for c in failures
        ]
    return out


def _is_ackable(check_runs: list[CheckRun]) -> bool:
    """True iff every check-run is completed AND in a passing conclusion.

    Empty check-runs list returns True — a repo with no CI has nothing
    to wait on. ``status != completed`` (anything still queued or in
    progress) keeps the watchdog watching. A non-passing conclusion
    (failure, cancelled, timed_out, action_required, stale, or even an
    unrecognized future value) keeps the watchdog watching too — we
    refuse to ack uncertainty.
    """
    for c in check_runs:
        if c.status != "completed":
            return False
        if c.conclusion not in _PASSING_CONCLUSIONS:
            return False
    return True


def _check_recent_merges(
    gh: GHClient,
    rs: RunState,
    now: datetime,
) -> tuple[list[dict], bool]:
    """For each recent merge not already ack'd, fetch its check-runs
    and classify.

    Returns ``(records, any_failure)``. Each record describes one
    still-watched merge; acked merges are skipped entirely and do not
    appear in the returned records. A check-runs fetch failure is
    recorded as a WARNING on ``rs`` and the record carries
    ``error: <message>`` instead of check_runs; it does NOT count
    toward ``any_failure`` because we don't know.

    Ack-on-clean (this.i node ``yhwagcvw``): when ``_is_ackable``
    returns True, persist the (slug, sha) pair via
    :func:`gitbulk.watchdog_ack.record_ack` so future reports skip it.
    """
    acked = load_acked()
    records: list[dict] = []
    any_failure = False
    for m in _scan_recent_merges(now):
        key = (m["slug"], m["merge_commit_sha"])
        if key in acked:
            continue
        try:
            check_runs = gh.fetch_check_runs(m["slug"], m["merge_commit_sha"])
        except GHError as e:
            rs.record_error(
                f"fetch_check_runs failed for {m['slug']}@{m['merge_commit_sha'][:7]}: {e}",
                level="WARNING",
                context={
                    "slug": m["slug"],
                    "sha": m["merge_commit_sha"],
                    "error": str(e),
                },
            )
            records.append({**m, "check_runs": [], "has_failure": False, "error": str(e)})
            continue
        failures = [c for c in check_runs if c.conclusion in _FAILURE_CONCLUSIONS]
        has_failure = bool(failures)
        if has_failure:
            any_failure = True
        else:
            # No failures: maybe ackable. Only ack when all checks are
            # completed (no in_progress remaining) so we don't ack early
            # before async workflows like cd.yml have started.
            if _is_ackable(check_runs):
                record_ack(m["slug"], m["merge_commit_sha"], now)
                # Acked-this-run: don't surface in the report either —
                # it would just be noise saying "watched-then-cleared."
                continue
        records.append(
            {
                **m,
                "check_runs": check_runs,
                "failures": failures,
                "has_failure": has_failure,
            }
        )
    return records, any_failure


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
    # 1. Load configuration. Per-entry parse failures in repos.txt are
    # captured as SkippedEntry records (so one typo doesn't block the
    # whole run) rather than raised; we surface them in summary.md and
    # bump the exit code to 3 if anything else passes cleanly.
    policy = load_policy()
    code_root = Path(args.code_root).expanduser() if args.code_root else None
    repos, skipped_entries = load_repos(code_root=code_root)
    repos_text = _read_repos_text()

    # Resolve the fleet-subset filter (CLI flags narrow a named config
    # set) and prune the repo list before the lock / invariant loop —
    # a repo filter makes the run cheaper, not just narrower. A bad
    # --filter name raises ConfigError here, which main() renders as a
    # clean one-liner (no half-finished run dir). (node flt7arg2)
    spec = resolve_filter_spec(args, policy)
    repos, repos_excluded = select_repos(repos, spec)

    # 2. Acquire global lock (shared, 300s timeout per tmlk5pq3). The
    # contextmanager raises LockTimeoutError on __enter__; per tmlk5pq3
    # the timeout is surfaced as exit 1 + stderr message, no ATTENTION
    # sentinel.
    #
    # NB: --refresh-org-members runs INSIDE the lock per security-hawk
    # F4 (2026-05-28). The network call + cache write are within the
    # audit envelope, and a parallel gitbulk run cannot race on the
    # cache file.
    try:
        with global_lock(
            "shared",
            timeout=_LOCK_TIMEOUT_SECONDS,
            subcommand="report",
        ):
            return _run_under_lock(
                args, policy, repos, repos_text, skipped_entries,
                spec, repos_excluded,
            )
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
    skipped_entries: list[SkippedEntry],
    spec,
    repos_excluded: int,
) -> int:
    """The portion of the pipeline that runs while the lock is held.

    Split out for clarity and so lock-timeout vs in-run errors are
    structurally distinct branches.
    """
    # 3. Begin RunState (must happen before any refresh so the audit
    # trail captures everything inside the lock per security-hawk F4).
    config_snapshot = _config_snapshot(policy, repos_text)
    rs = RunState.begin(
        "report",
        argv=list(sys.argv),
        config_snapshot=config_snapshot,
    )

    # Build gh client + base context.
    gh = ProductionGHClient()
    ctx_base = InvariantContext(policy=policy, runstate=rs, gh=gh)

    # 4. Optional --refresh-org-members. Runs INSIDE the lock per
    # security-hawk F4 (2026-05-28): the network call + cache write
    # are within the audit envelope and another gitbulk process cannot
    # race on the cache file.
    if args.refresh_org_members and policy.humans.org:
        try:
            refresh_cache(gh, policy.humans.org)
        except GHError as e:
            rs.record_error(f"--refresh-org-members failed: {e}")
            return _finish(
                rs,
                EXIT_STRUCTURAL_FAILURE,
                summary=f"--refresh-org-members failed: {e}",
                policy=policy,
                attention=False,
                all_repos=repos,
                passing_repos=[],
                skipped_repos=[],
                prs_by_repo={},
                pr_records_by_repo={},
                attention_count=0,
            skipped_entries=skipped_entries,
            )

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
            skipped_entries=skipped_entries,
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
    # Prime the default-branch cache before the per-repo loop. This
    # seeds gh's in-process cache from the on-disk cache (warm entries
    # cost nothing) and only GraphQL-prefetches the stale/missing slugs.
    # github.reachable + pr.base_is_default both call gh.default_branch.
    # The cold prefetch reports progress (multi-second for a big fleet);
    # an all-warm run does no network and shows nothing.
    prefetch_prog = Progress(
        len(repos), prefix="prefetching default branches: "
    )
    prime_default_branches(
        gh,
        [r.slug for r in repos],
        on_progress=lambda done, total: prefetch_prog.update(done),
    )
    prefetch_prog.done()
    # The per-repo invariant chain runs ``github.reachable``. With the
    # prefetch above each call is a cache hit, so this loop is now bound
    # by Python overhead rather than network. Progress kept regardless.
    progress = Progress(len(repos), prefix="per-repo checks: ")
    for i, repo in enumerate(repos, start=1):
        progress.update(i, repo.slug)
        ctx_repo = replace(ctx_base, repo=repo)
        r = run_chain(
            per_repo, ctx_repo, skip_set=skip_set, target=repo.slug
        )
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
                prs_by_repo={},
                pr_records_by_repo={},
                attention_count=0,
                skipped_entries=skipped_entries,
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
    progress.done()

    # 7. Coalesced PR fetch. One GraphQL call regardless of repo count,
    # but it can take a few seconds for a fleet — print a status line
    # so the user knows we're waiting on the network, not stuck.
    if passing_repos:
        if sys.stderr.isatty():
            print(
                f"Fetching open PRs across {len(passing_repos)} repos...",
                file=sys.stderr,
                end="",
                flush=True,
            )
        try:
            # report is read-only, so the author filter may widen beyond
            # the user's own PRs (per flt7arg2). fetch_author defaults to
            # @me when no --author was given.
            prs_by_repo = gh.my_open_prs(
                [r.slug for r in passing_repos], author=fetch_author(spec)
            )
        except GHError as e:
            if sys.stderr.isatty():
                sys.stderr.write("\r" + " " * 80 + "\r")
                sys.stderr.flush()
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
                skipped_entries=skipped_entries,
            )
        if sys.stderr.isatty():
            sys.stderr.write("\r" + " " * 80 + "\r")
            sys.stderr.flush()
    else:
        prs_by_repo = {}

    # Apply PR-level filters (base, mergeable_state) after the fetch.
    prs_by_repo, prs_excluded = apply_pr_filters(prs_by_repo, spec)

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

    # 8b. Post-merge watchdog: scan recent merge runs for CD failures
    # on the resulting merge commits. Surfaces them in the summary and
    # forces ATTENTION if any are red.
    watchdog_records, any_watchdog_failure = _check_recent_merges(
        gh, rs, datetime.now(timezone.utc)
    )
    # Persist the watchdog findings into state.yaml so downstream
    # consumers (notably ``gitbulk summarize``'s LLM prompt) see them
    # alongside the per-repo PR data. CheckRun is a dataclass — flatten
    # to plain dicts for YAML serialization.
    if watchdog_records:
        rs.record_extra(
            "recent_merges",
            [_serialize_watchdog_record(r) for r in watchdog_records],
        )

    # 9. Summary markdown.
    fline = filter_summary_line(spec, repos_excluded, prs_excluded)
    summary_md = _build_summary_md(
        policy,
        repos,
        passing_repos,
        skipped_repos,
        prs_by_repo,
        pr_records_by_repo,
        attention_count,
        watchdog_records=watchdog_records,
        skipped_entries=skipped_entries,
        filter_line=fline,
    )
    rs.write_summary(summary_md)

    # 10. Compute exit code. Skipped repos.txt entries count toward
    # EXIT_INVARIANT_SKIPPED (3) — they're things the user can fix that
    # gitbulk routed around rather than acted on.
    if attention_count > 0 or any_watchdog_failure:
        exit_code = EXIT_ATTENTION_NEEDED
    elif skipped_repos or skipped_entries:
        exit_code = EXIT_INVARIANT_SKIPPED
    elif skip_list:
        exit_code = EXIT_OVERRIDES_APPLIED
    else:
        exit_code = EXIT_OK

    wd_failed = sum(1 for r in watchdog_records if r.get("has_failure"))
    summary_text = (
        f"{attention_count} PRs need attention; "
        f"{len(skipped_repos)} repos skipped; "
        f"{len(skipped_entries)} entries skipped; "
        f"{wd_failed} recent-merge CD failure(s)"
    )
    if fline:
        summary_text = f"{summary_text}; {fline}"
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
        skipped_entries=skipped_entries,
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
    skipped_entries: list[SkippedEntry] | None = None,
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
            skipped_entries=skipped_entries,
        )
        # Append the failure summary so the operator sees the failure first.
        rs.write_summary(f"# gitbulk report (FAILED)\n\n{summary}\n\n{synth}")

    if attention:
        runid = _runid_from_run_dir(rs.run_dir)
        sentinel.set_attention(exit_code, "report", runid, summary)

    rs.complete(exit_code, retain_runs=policy.defaults.retain_runs)

    # Tell the user what happened. Without this line, a successful
    # report produces no stdout — the user has to know to look at
    # ~/.cache/gitbulk/runs/latest-report/ on their own. Form is
    # deliberately terse: one line summary + how to read more.
    print(f"gitbulk report: {summary}. View: gitbulk show report")
    return exit_code


__all__ = [
    "EXIT_ATTENTION_NEEDED",
    "EXIT_INVARIANT_SKIPPED",
    "EXIT_OK",
    "EXIT_OVERRIDES_APPLIED",
    "EXIT_STRUCTURAL_FAILURE",
    "report_handler",
]
