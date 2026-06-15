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
import copy
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import yaml

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
from gitbulk.commands._common import (
    apply_prune_min_age_override,
    dc_to_dict,
    read_repos_text,
    sacred_branch_names,
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
from gitbulk.util.parallel import parallel_map
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
    }


def _runid_from_run_dir(run_dir: Path) -> str:
    name = run_dir.name
    suffix = "-prune-branches"
    if name.endswith(suffix):
        return name[: -len(suffix)]
    head, _, _ = name.rpartition("-")
    return head


# ─── per-branch classification (the guardrails) ────────────────────────────


def _is_no_common_ancestor(err: GHError) -> bool:
    """Whether a compare-API failure is GitHub's "No common ancestor" 404.

    That 404 means the two refs have UNRELATED histories — ``branch`` is an
    ORPHAN with no commit in common with the default branch (the ``tick``
    ledger, an orphan ``gh-pages`` site, a standalone build/artifact branch),
    which is never auto-pruned (node prnorph7). Distinguished from a transient
    or other failure so only a genuine orphan gets the orphan verdict; anything
    else still biases to a plain "could not verify" skip. GitHub's message is
    stable — ``No common ancestor between <base> and <head>.`` — verified
    against the live API 2026-06-13."""
    return "no common ancestor" in str(err).lower()


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
      3. not a sacred branch name — ``main``/``master``, the orphan-convention
         names ``gh-pages``/``tick``, or a configured ``sacred_branches`` entry
         (shared with prune-worktrees via _common)
      4. not the head of any OPEN PR
      5. not the base of any OPEN PR (stacked-PR dependency)
      6. there IS a closed/merged PR for it on the UPSTREAM (not a fork)
      7. that PR is older than the grace period (node prgrc3kp)
      8. not an ORPHAN branch — it shares history with the default branch
         (node prnorph7): the compare API's "No common ancestor" 404 keeps an
         unrelated-history branch even when it carries a stray closed PR
      9. no commit loss (node prdls2nq): the branch tip equals the merged
         PR's recorded head SHA, OR the branch is fully contained in the
         default branch

    Any gh error or inconclusive check biases to ``skip`` (fail safe).

    Guards 1-5 are *cheap* (no network) and 6-8 are *deep* (per-branch gh
    calls). The parallel scan (node prnpf8nq) runs them as two passes via
    :func:`_classify_cheap` / :func:`_classify_deep`; this wrapper preserves
    the original single-call semantics for direct callers and unit tests.
    """
    sacred = sacred_branch_names(policy, slug)
    cheap = _classify_cheap(
        slug, default_branch, branch, open_heads, open_bases, sacred
    )
    if cheap is not None:
        return cheap
    return _classify_deep(gh, policy, slug, default_branch, branch, now)


def _classify_cheap(
    slug: str,
    default_branch: str,
    branch,
    open_heads: set[str],
    open_bases: set[str],
    sacred: frozenset[str],
) -> dict | None:
    """Network-free guards 1-4 (node prnbr4kq) plus the sacred-name backstop.
    Returns a terminal ``skip`` dict when one fires, or ``None`` when the branch
    needs deep classification (guards 5-7). These never surface a delete on their
    own, so a cheap-skipped branch is dropped from the report just as before.

    ``sacred`` is the union of the always-sacred ``main``/``master`` and the
    operator-configured ``sacred_branches`` (see _common). It is the SAME set
    prune-worktrees uses, so a branch a user protects from local deletion is
    equally protected from remote deletion — even when it is not the repo's
    default and carries no GitHub branch protection."""
    name = branch.name
    base = {"slug": slug, "branch": name, "sha": branch.sha}
    if name == default_branch:
        return {**base, "decision": "skip", "reason": "default branch"}
    if branch.protected:
        return {**base, "decision": "skip", "reason": "branch is protected"}
    if name in sacred:
        return {
            **base, "decision": "skip",
            "reason": f"sacred branch name '{name}' (never auto-pruned)",
        }
    if name in open_heads:
        return {**base, "decision": "skip", "reason": "head of an open PR"}
    if name in open_bases:
        return {
            **base,
            "decision": "skip",
            "reason": "base of an open PR (stacked dependency)",
        }
    return None


def _classify_deep(
    gh,
    policy: Policy,
    slug: str,
    default_branch: str,
    branch,
    now: datetime,
) -> dict:
    """Guards 5-9 (node prnbr4kq): the closed-PR lookup, grace period, orphan
    guard (node prnorph7), and data-loss check (node prdls2nq). Each makes
    per-branch gh calls and is the dominant scan cost, so it runs in the
    flattened Pass B of the parallel scan. Any gh error or inconclusive check
    biases to ``skip``."""
    name = branch.name
    base = {"slug": slug, "branch": name, "sha": branch.sha}
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
        # ORPHAN guard (node prnorph7): a branch with NO commit in common with
        # the default branch makes the compare API answer "No common ancestor"
        # (HTTP 404). Recognise it explicitly and KEEP it — an unrelated-history
        # branch (the tick ledger, an orphan gh-pages site, a build branch) is
        # never auto-pruned, mirroring prune-worktrees and the shared sacred-name
        # backstop that already covers the well-known gh-pages/tick names. A real
        # orphan like those never has a merged PR into the default, so it does
        # not reach the tip-unchanged shortcut above; this clause catches the
        # ones that DO carry a stray closed/merged PR. Any OTHER compare failure
        # biases to a plain skip as before (fail safe, prdls2nq).
        if _is_no_common_ancestor(e):
            return {
                **base,
                "decision": "skip",
                "reason": (
                    f"unrelated history to default '{default_branch}' "
                    f"(orphan branch — never auto-pruned)"
                ),
            }
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


# ─── parallel scan (node prnpf8nq) ─────────────────────────────────────────


def _resolve_concurrency(args: argparse.Namespace, policy: Policy) -> int:
    """The branch-scan worker count: ``--concurrency`` if given, else the
    policy default (``prune_scan_concurrency``). Floored at 1 so a bogus 0/
    negative never disables the scan."""
    val = getattr(args, "concurrency", None)
    if val is None:
        val = policy.defaults.prune_scan_concurrency
    return max(1, int(val))


def _scan_repo_cheap(
    gh, policy: Policy, slug: str, prior_entry: dict | None
) -> dict:
    """Pass A for one repo: read-only fetch + network-free cheap triage.

    Returns a dict with ``error`` set (the fetch failed) OR ``default_branch``
    plus ``cached_rows`` (SHA cache hits, no deep calls) and ``needs_deep``
    (branches needing Pass B). Runs in a worker thread and touches only the gh
    network boundary (read-only), never the run state.

    Stores ALL deep-classified branches (surfaced + the "no closed PR" skips)
    so an unchanged tip SHA can be served from cache on a later re-scan
    (node prnsh5kp). Cheap-skipped branches are dropped (re-evaluated free).
    """
    try:
        default_branch = gh.default_branch(slug)
        branches = gh.list_branches(slug)
        open_prs = gh.my_open_prs([slug], author=None).get(slug, [])
    except GHError as e:
        return {"slug": slug, "error": str(e)}
    open_heads = {pr.head_ref for pr in open_prs}
    open_bases = {pr.base_ref for pr in open_prs}
    sacred = sacred_branch_names(policy, slug)
    prior_by_name = {
        b.get("branch"): b for b in (prior_entry or {}).get("branches", [])
    }
    cached_rows: list[dict] = []
    needs_deep = []
    for branch in branches:
        if _classify_cheap(
            slug, default_branch, branch, open_heads, open_bases, sacred
        ) is not None:
            continue  # cheap skip — dropped, never stored
        prior_row = prior_by_name.get(branch.name)
        if (
            prior_row is not None
            and prior_row.get("sha") == branch.sha
            and _is_cacheable(prior_row)
        ):
            # Unchanged SHA + reusable verdict → skip the expensive deep calls.
            cached_rows.append(_reuse_classification(slug, branch, prior_row))
        else:
            needs_deep.append(branch)
    return {
        "slug": slug,
        "error": None,
        "default_branch": default_branch,
        "cached_rows": cached_rows,
        "needs_deep": needs_deep,
    }


def _scan_branches(
    gh,
    policy: Policy,
    rs: RunState,
    passing_repos: list[RepoEntry],
    now: datetime,
    concurrency: int,
    *,
    prior_repos: dict,
    cli_max_age_minutes: int | None,
) -> tuple[list[dict], dict]:
    """Plan-aware two-pass parallel scan (nodes prnpf8nq, prnsh5kp).

    Partitions repos into *reuse* (a plan entry fresh enough per ``--max-age``
    / policy) and *scan*; reused repos contribute their cached rows verbatim
    (no network). Scanned repos go through Pass A (fetch + cheap triage +
    per-branch SHA cache hit) and the flattened Pass B (deep classification of
    the rest). Returns ``(results, materialized_meta)`` where ``results`` holds
    EVERY deep row (incl. the unsurfaced "no closed PR" skips, kept so the next
    run can cache-hit them) and ``materialized_meta`` maps each reused/scanned
    slug → ``{analyzed_at, default_branch}`` for the plan write.
    """
    now_iso = now.isoformat()
    reuse: list[tuple[RepoEntry, dict]] = []
    # to_scan carries (repo, sha_cache_entry): the prior entry to consult for
    # per-branch SHA reuse, or None when the user forced a full re-verify
    # (max_age 0 / --force-scan) — then even unchanged branches are re-classified.
    to_scan: list[tuple[RepoEntry, dict | None]] = []
    for repo in passing_repos:
        prior = prior_repos.get(repo.slug)
        max_age = (
            cli_max_age_minutes
            if cli_max_age_minutes is not None
            else policy_for(policy, repo.slug).prune_plan_max_age_minutes
        )
        if max_age <= 0:
            to_scan.append((repo, None))            # forced full re-verify
        elif prior is not None and _is_fresh(prior, max_age, now):
            reuse.append((repo, prior))             # fresh: skip the repo
        else:
            to_scan.append((repo, prior))           # stale: rescan, SHA-reuse

    # Pass A — over the repos that actually need scanning.
    prog_a = Progress(len(to_scan), prefix="scanning repos: ")
    repo_scans = parallel_map(
        lambda item: _scan_repo_cheap(gh, policy, item[0].slug, item[1]),
        to_scan,
        concurrency=concurrency,
        on_progress=lambda done, total: prog_a.update(done),
    )
    prog_a.done()

    # Pass B — flattened over every branch needing deep classification.
    deep_items: list[tuple[str, str, object]] = []
    for scan in repo_scans:
        if scan["error"] is not None:
            continue
        for branch in scan["needs_deep"]:
            deep_items.append((scan["slug"], scan["default_branch"], branch))

    prog_b = Progress(len(deep_items), prefix="classifying branches: ")
    deep_results = parallel_map(
        lambda item: _classify_deep(gh, policy, item[0], item[1], item[2], now),
        deep_items,
        concurrency=concurrency,
        on_progress=lambda done, total: prog_b.update(done),
    )
    prog_b.done()

    deep_by_slug: dict[str, list[dict]] = {}
    for (slug, _db, _branch), res in zip(deep_items, deep_results):
        deep_by_slug.setdefault(slug, []).append(res)

    results: list[dict] = []
    materialized: dict[str, dict] = {}
    # Reused repos: prior rows verbatim, prior analyzed_at preserved.
    for repo, prior in reuse:
        results.extend(_reuse_rows(repo.slug, prior))
        materialized[repo.slug] = {
            "analyzed_at": prior.get("analyzed_at"),
            "default_branch": prior.get("default_branch"),
        }
    # Scanned repos: cache hits (Pass A) + freshly classified (Pass B).
    for (repo, _sha_entry), scan in zip(to_scan, repo_scans):
        slug = repo.slug
        if scan["error"] is not None:
            rs.record_error(
                f"branch scan failed for {slug}: {scan['error']}",
                level="ERROR",
                context={"slug": slug, "error": scan["error"]},
            )
            results.append(
                {"slug": slug, "branch": None, "decision": "error",
                 "reason": f"scan failed: {scan['error']}"}
            )
            continue
        results.extend(scan["cached_rows"])
        results.extend(deep_by_slug.get(slug, []))
        materialized[slug] = {
            "analyzed_at": now_iso,
            "default_branch": scan["default_branch"],
        }
    return results, materialized


# ─── plan persistence + dispositions (nodes prnpl3kq, prnrv6kq) ────────────

#: Version of the prune-branches *plan payload* (the per-repo/per-branch shape
#: written under state.yaml's ``repos`` + the ``prune_plan`` extra). Tracked
#: independently of runstate's envelope SCHEMA_VERSION because only this
#: subcommand's payload changed; readers tolerate its absence (old plans).
_PLAN_VERSION = 2


#: Branch verdicts that are safe to reuse for an unchanged tip SHA
#: (node prnsh5kp): a ``delete`` only gets more valid with age, and a
#: ``skip`` with NO associated PR ("no merged/closed PR") is stable. A skip
#: that DOES cite a PR (grace-pending, or data-loss "would lose work") is
#: time/state dependent and is re-classified instead.
_DURATION_UNITS = {"m": 1, "h": 60, "d": 60 * 24}


def _parse_duration_minutes(text: str) -> int:
    """Parse ``30m`` / ``6h`` / ``2d`` / bare-minutes (``90``) → minutes.

    Raises :class:`ValueError` on anything else so the CLI can report it."""
    s = text.strip().lower()
    if not s:
        raise ValueError("empty duration")
    unit = 1
    if s[-1] in _DURATION_UNITS:
        unit = _DURATION_UNITS[s[-1]]
        s = s[:-1]
    if not s.isdigit():
        raise ValueError(
            f"invalid --max-age {text!r}; use e.g. 30m, 6h, 2d, or a number "
            "of minutes"
        )
    return int(s) * unit


def _cli_max_age_minutes(args: argparse.Namespace) -> int | None:
    """The CLI freshness override: 0 for ``--force-scan``, the parsed
    ``--max-age``, or ``None`` to fall back to the per-repo policy value."""
    if getattr(args, "force_scan", False):
        return 0
    raw = getattr(args, "max_age", None)
    if raw is None:
        return None
    return _parse_duration_minutes(raw)


def _is_fresh(entry: dict, max_age_minutes: int, now: datetime) -> bool:
    """True if ``entry`` was analysed within ``max_age_minutes`` of ``now``.

    A non-positive window, a missing/unparseable ``analyzed_at``, or a
    future stamp all read as NOT fresh (re-scan) — the conservative default."""
    if max_age_minutes <= 0:
        return False
    stamp = entry.get("analyzed_at")
    if not isinstance(stamp, str):
        return False
    try:
        analyzed = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    age_minutes = (now - analyzed).total_seconds() / 60.0
    return 0 <= age_minutes <= max_age_minutes


def _is_cacheable(row: dict) -> bool:
    """Whether a prior branch verdict can be reused for an unchanged SHA
    (node prnsh5kp)."""
    decision = row.get("decision")
    if decision == "delete":
        return True
    if decision == "skip" and "pr_number" not in row:
        return True
    return False


def _reuse_classification(slug: str, branch, prior_row: dict) -> dict:
    """A fresh result row built from a cached verdict (unchanged SHA) — the
    classification only, with disposition left to re-finalize for this run."""
    out = {
        "slug": slug,
        "branch": branch.name,
        "sha": branch.sha,
        "decision": prior_row["decision"],
        "reason": prior_row.get("reason", ""),
    }
    if "pr_number" in prior_row:
        out["pr_number"] = prior_row["pr_number"]
    if "pr_state" in prior_row:
        out["pr_state"] = prior_row["pr_state"]
    return out


def _reuse_rows(slug: str, prior_entry: dict) -> list[dict]:
    """Whole-repo reuse (per-repo freshness): the prior branch rows verbatim,
    PRESERVING their dispositions (a deleted branch stays deleted)."""
    rows: list[dict] = []
    for branch in prior_entry.get("branches", []):
        rows.append({"slug": slug, **copy.deepcopy(branch)})
    return rows


def _disposition_of(row: dict) -> str:
    """The action state of one branch row. Reads an explicit ``disposition``
    if present (fresh rows, and rows carried forward from a P2+ plan); else
    derives one from the legacy fields so a pre-P2 plan still renders."""
    explicit = row.get("disposition")
    if explicit is not None:
        return explicit
    decision = row.get("decision")
    if decision == "delete":
        if row.get("deleted"):
            return "deleted"
        if "error" in row:
            return "failed"
        return "pending"
    if decision == "error":
        return "error"
    return "kept"


def _finalize_dispositions(results: list[dict]) -> None:
    """Stamp disposition/acted_at/acted_mode on every surfaced row that the
    apply loop didn't already set (pending candidates, kept skips, errors)."""
    for row in results:
        row.setdefault("disposition", _disposition_of(row))
        row.setdefault("acted_at", None)
        row.setdefault("acted_mode", None)


def _load_latest_plan_repos() -> dict:
    """The ``repos`` map of the most recent prune-branches plan, or ``{}``.

    Read from the ``latest-prune-branches`` symlink, which still points at the
    PRIOR run until this run's :meth:`RunState.complete`. Called from two
    places (node prnpl3kq):

      - the handler, BEFORE the scan, as a best-effort freshness heuristic
        (which repos to reuse vs re-scan) — NOT under any lock; a slightly
        stale read here only ever mis-picks a repo to reuse/rescan, which is
        harmless.
      - :func:`_finish`, INSIDE ``run_state_lock``, for the carry-forward
        merge: there the read→merge→write→symlink-advance must be one atomic
        critical section so concurrent subset applies don't lose each other's
        dispositions (rsclk7nq §2 row 1). The lock is a requirement of THAT
        call site, not of this function."""
    symlink = paths.latest_run_symlink("prune-branches")
    try:
        state_path = symlink.resolve(strict=True) / "state.yaml"
        data = yaml.safe_load(state_path.read_text())
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict):
        return {}
    repos = data.get("repos")
    return repos if isinstance(repos, dict) else {}


def _plan_branch(row: dict) -> dict:
    """One branch row as stored in the plan (state.yaml): everything but the
    redundant ``slug`` key, with disposition fields guaranteed present."""
    out = {k: v for k, v in row.items() if k != "slug"}
    out["disposition"] = _disposition_of(out)
    out.setdefault("acted_at", None)
    out.setdefault("acted_mode", None)
    return out


def _merge_plan(
    prior_repos: dict, materialized: dict, results: list[dict]
) -> dict:
    """Carry the prior plan forward, overwriting only the repos THIS run
    materialized — i.e. reused-fresh OR scanned cleanly (nodes prnpl3kq,
    prnsh5kp). Repos out of scope, or whose scan errored, keep their prior
    entry — so subset applies accumulate and a transient failure never wipes
    good data. Each materialized entry's ``analyzed_at`` comes from
    ``materialized`` (preserved for a reused repo, ``now`` for a scanned one)."""
    merged = dict(prior_repos)
    by_slug: dict[str, list[dict]] = {}
    for row in results:
        if row["decision"] == "error":
            continue
        by_slug.setdefault(row["slug"], []).append(row)
    for slug, meta in materialized.items():
        rows = by_slug.get(slug, [])
        merged[slug] = {
            "analyzed_at": meta["analyzed_at"],
            "default_branch": meta["default_branch"],
            "branch_count": len(rows),
            "branches": [_plan_branch(r) for r in rows],
        }
    return merged


def _revalidate_delete(
    gh, slug: str, branch: str, expected_sha: str, open_refs_cache: dict
) -> tuple[str, str | None]:
    """Re-check the governing facts immediately before deleting (node prnrv6kq).

    Returns one of:
      ``("delete", None)``           — safe to delete.
      ``("already-gone", reason)``   — ref already deleted (tolerate as success).
      ``("refused", reason)``        — UNSAFE drift; do not delete.

    The tip is re-GET so a moved tip (post-merge push → would lose work) or a
    deleted ref is caught even when the plan is hours old; a fresh open-PR
    fetch catches a branch that has been reused. Any inability to re-verify
    biases to ``refused`` (fail safe, prdls2nq)."""
    try:
        tip = gh.branch_ref_sha(slug, branch)
    except GHError as e:
        return ("refused", f"could not re-verify tip: {e}")
    if tip is None:
        return ("already-gone", "ref absent on the remote")
    if tip != expected_sha:
        return (
            "refused",
            f"tip moved to {tip[:7]} since analysis — would lose work",
        )
    # Tip unchanged → confirm the branch hasn't been reused by a fresh PR.
    if slug not in open_refs_cache:
        try:
            prs = gh.my_open_prs([slug], author=None).get(slug, [])
            open_refs_cache[slug] = (
                {pr.head_ref for pr in prs},
                {pr.base_ref for pr in prs},
            )
        except GHError as e:
            open_refs_cache[slug] = e
    refs = open_refs_cache[slug]
    if isinstance(refs, GHError):
        return ("refused", f"could not re-verify open PRs: {refs}")
    open_heads, open_bases = refs
    if branch in open_heads:
        return ("refused", "now the head of an open PR — used again")
    if branch in open_bases:
        return ("refused", "now the base of an open PR — used again")
    return ("delete", None)


def _flatten_plan(merged: dict) -> list[dict]:
    """Plan repos → a flat, slug-stamped, slug-sorted row list for the
    summary builder."""
    rows: list[dict] = []
    for slug in sorted(merged):
        for branch in merged[slug].get("branches", []):
            rows.append({"slug": slug, **branch})
    return rows


# ─── public handler ────────────────────────────────────────────────────────


def prune_branches_handler(args: argparse.Namespace) -> int:
    # Validate --max-age up front so a typo fails cleanly before any run state
    # is created (node prnsh5kp).
    try:
        _cli_max_age_minutes(args)
    except ValueError as e:
        print(
            error_line(f"gitbulk prune-branches: {e}"), file=sys.stderr
        )
        return EXIT_STRUCTURAL_FAILURE
    policy = apply_prune_min_age_override(load_policy(), args)
    code_root = Path(args.code_root).expanduser() if args.code_root else None
    repos, skipped_entries = load_repos(code_root=code_root)
    repos_text = read_repos_text()

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
            skipped_repos=[], results=[], apply=bool(args.apply), failed=True,
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
            skipped_repos=[], results=[], apply=bool(args.apply), failed=True,
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
                results=[], apply=bool(args.apply), failed=True,
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
    now_iso = now.isoformat()
    filter_line = filter_summary_line(spec, repos_excluded, 0)
    concurrency = _resolve_concurrency(args, policy)
    # Reuse fresh in-scope plan entries; scan the rest (nodes prnsh5kp, prnpl3kq).
    # This read is a pre-scan heuristic; _finish reloads under lock for the merge.
    cli_max_age = _cli_max_age_minutes(args)
    prior_repos = _load_latest_plan_repos()
    # results: every deep row (incl. unsurfaced "no closed PR" skips, kept for
    # the next run's SHA cache). materialized: slug → {analyzed_at, default_branch}.
    results, materialized = _scan_branches(
        gh, policy, rs, passing_repos, now, concurrency,
        prior_repos=prior_repos, cli_max_age_minutes=cli_max_age,
    )

    # Only PENDING deletes are candidates — a reused row already deleted in a
    # prior apply must not be re-deleted (node prnpl3kq).
    delete_candidates = [
        r for r in results
        if r["decision"] == "delete" and _disposition_of(r) == "pending"
    ]

    if not args.apply:
        _finalize_dispositions(results)
        return _finish_dry_run(
            rs, policy, repos, passing_repos, skipped_repos, results, materialized,
            delete_candidates, skip_list, skipped_entries, filter_line,
        )

    # ── --apply: re-validate, then delete each candidate ──
    failure_count = 0
    deleted_count = 0
    refused_count = 0
    gone_count = 0
    open_refs_cache: dict[str, tuple | GHError] = {}
    for cand in delete_candidates:
        slug = cand["slug"]
        branch = cand["branch"]
        try:
            # repo_lock(slug): serialize this remote mutation against any other
            # gitbulk run touching the SAME repo (node rsclk7nq resource #7).
            # Re-validation runs INSIDE the lock, immediately before the
            # destructive act, so the plan can be stale/reused yet still safe
            # (node prnrv6kq).
            with repo_lock(
                slug, "exclusive", timeout=_LOCK_TIMEOUT_SECONDS,
                subcommand="prune-branches",
            ):
                action, reason = _revalidate_delete(
                    gh, slug, branch, cand["sha"], open_refs_cache
                )
                if action == "already-gone":
                    gone_count += 1
                    cand["disposition"] = "already-gone"
                    cand["acted_at"] = now_iso
                    cand["acted_mode"] = "apply"
                    rs.record_error(
                        f"{slug}:{branch} already gone — no-op ({reason})",
                        level="WARNING",
                        context={"slug": slug, "branch": branch,
                                 "action": "already-gone"},
                    )
                    continue
                if action == "refused":
                    refused_count += 1
                    cand["disposition"] = "refused"
                    cand["refuse_reason"] = reason
                    cand["acted_at"] = now_iso
                    cand["acted_mode"] = "apply"
                    rs.record_error(
                        f"REFUSED delete of {slug}:{branch}: {reason}",
                        level="WARNING",
                        context={"slug": slug, "branch": branch,
                                 "action": "refused", "reason": reason},
                    )
                    continue
                gh.delete_branch_ref(slug, branch)
        except GHError as e:
            failure_count += 1
            cand["error"] = str(e)
            cand["disposition"] = "failed"
            cand["acted_at"] = now_iso
            cand["acted_mode"] = "apply"
            rs.record_error(
                f"delete_branch_ref failed for {slug}:{branch}: {e}",
                level="ERROR",
                context={"slug": slug, "branch": branch, "error": str(e)},
            )
            continue
        cand["deleted"] = True
        cand["disposition"] = "deleted"
        cand["acted_at"] = now_iso
        cand["acted_mode"] = "apply"
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

    _finalize_dispositions(results)

    if failure_count > 0 or refused_count > 0:
        # A refusal means reality diverged from the plan in an UNSAFE way
        # (tip moved / branch reused) — the user should see it (node prnrv6kq).
        exit_code, attention = EXIT_ATTENTION_NEEDED, True
    elif skipped_repos or skipped_entries:
        exit_code, attention = EXIT_INVARIANT_SKIPPED, True
    elif skip_list:
        exit_code, attention = EXIT_OVERRIDES_APPLIED, False
    else:
        exit_code, attention = EXIT_OK, False

    summary_text = (
        f"deleted {deleted_count} of {len(delete_candidates)} branches; "
        f"{failure_count} failed; {refused_count} refused (stale); "
        f"{gone_count} already gone; {len(skipped_repos)} repos skipped; "
        f"{len(skipped_entries)} entries skipped"
        + (f"; {filter_line}" if filter_line else "")
    )
    return _finish(
        rs, exit_code, summary=summary_text, policy=policy, attention=attention,
        all_repos=repos, passing_repos=passing_repos, skipped_repos=skipped_repos,
        results=results, materialized=materialized, apply=True,
        skipped_entries=skipped_entries, filter_line=filter_line,
    )


def _finish_dry_run(
    rs, policy, repos, passing_repos, skipped_repos, results, materialized,
    delete_candidates, skip_list, skipped_entries, filter_line,
) -> int:
    if skipped_repos or skipped_entries:
        exit_code, attention = EXIT_INVARIANT_SKIPPED, True
    elif skip_list:
        exit_code, attention = EXIT_OVERRIDES_APPLIED, False
    else:
        # Pending deletions in a dry run are NOT attention-worthy: routine
        # cleanup the user will confirm by re-running with --apply.
        exit_code, attention = EXIT_OK, False
    summary_text = (
        f"dry-run: {len(delete_candidates)} branches would be deleted; "
        f"{len(skipped_repos)} repos skipped; "
        f"{len(skipped_entries)} entries skipped"
        + (f"; {filter_line}" if filter_line else "")
    )
    return _finish(
        rs, exit_code, summary=summary_text, policy=policy, attention=attention,
        all_repos=repos, passing_repos=passing_repos, skipped_repos=skipped_repos,
        results=results, materialized=materialized, apply=False,
        skipped_entries=skipped_entries, filter_line=filter_line,
    )


def _build_summary_md(
    rows: list[dict],
    *,
    all_repos: list[RepoEntry],
    passing_repos: list[RepoEntry],
    skipped_repos: list[tuple[str, str]],
    apply: bool,
    skipped_entries: list[SkippedEntry] | None = None,
    filter_line: str | None = None,
) -> str:
    """Render the plan as markdown. ``rows`` is the FULL plan (carried-forward
    + freshly scanned, each row carrying a ``disposition``) plus this run's
    transient error rows — so a partial apply shows accumulated dispositions
    (node prnpl3kq)."""
    lines: list[str] = ["# gitbulk prune-branches", ""]
    lines.append(f"Mode: **{'APPLY' if apply else 'DRY-RUN'}**")
    if filter_line:
        lines.append(filter_line)

    delete_rows = [r for r in rows if r["decision"] == "delete"]
    deleted = [
        r for r in delete_rows
        if _disposition_of(r) in ("deleted", "already-gone")
    ]
    failed = [r for r in delete_rows if _disposition_of(r) == "failed"]
    refused = [r for r in delete_rows if _disposition_of(r) == "refused"]
    pending = [r for r in delete_rows if _disposition_of(r) == "pending"]
    # Only skips that cite a PR are interesting ("kept despite a closed PR").
    # The "no closed PR" skips are stored for the SHA cache (prnsh5kp) but are
    # noise in the report — exactly the branches the old scan never surfaced.
    skips = [
        r for r in rows if r["decision"] == "skip" and "pr_number" in r
    ]
    errors = [r for r in rows if r["decision"] == "error"]

    lines.append(
        f"Configured repos: {len(all_repos)}  Reachable: {len(passing_repos)}  "
        f"Skipped repos: {len(skipped_repos)}  "
        f"Delete candidates: {len(delete_rows)}"
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

    def _emit(header: str, group: list[dict], suffix) -> None:
        if not group:
            return
        lines.append(f"## {header}")
        for r in group:
            lines.append(
                f"- `{r['slug']}` `{r['branch']}` @ {r['sha'][:7]} "
                f"({r['reason']}){suffix(r)}"
            )
        lines.append("")

    _emit(
        "Deleted", deleted,
        lambda r: " — already gone"
        if _disposition_of(r) == "already-gone"
        else " — deleted",
    )
    _emit("Failed to delete", failed, lambda r: " — " + r.get("error", ""))
    _emit(
        "Refused (plan stale)", refused,
        lambda r: " — " + r.get("refuse_reason", "unsafe drift"),
    )
    _emit("Would delete", pending, lambda r: "")

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
    if not delete_rows and not skips and not errors:
        lines.append("(no branches matched)")
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
    results: list[dict],
    apply: bool,
    materialized: dict | None = None,
    failed: bool = False,
    skipped_entries: list[SkippedEntry] | None = None,
    filter_line: str | None = None,
) -> int:
    materialized = materialized or {}
    if attention:
        runid = _runid_from_run_dir(rs.run_dir)
        with sentinel_lock(timeout=_LOCK_TIMEOUT_SECONDS, subcommand="prune-branches"):
            sentinel.set_attention(exit_code, "prune-branches", runid, summary)
    # The carry-forward read→merge→write→symlink-advance is one critical
    # section under run_state_lock so two concurrent subset applies accumulate
    # without losing each other's dispositions (node prnpl3kq; rsclk7nq §2 #1).
    with run_state_lock(
        "prune-branches", "exclusive", timeout=_LOCK_TIMEOUT_SECONDS,
        subcommand="prune-branches",
    ):
        merged = _merge_plan(_load_latest_plan_repos(), materialized, results)
        rs.set_repos(merged)
        rs.record_extra(
            "prune_plan",
            {
                "version": _PLAN_VERSION,
                "scope_slugs": sorted(r.slug for r in all_repos),
            },
        )
        # Transient scan-failure rows aren't persisted in the plan (we keep the
        # prior good entry), but they ARE surfaced in this run's summary.
        current_errors = [r for r in results if r["decision"] == "error"]
        body = _build_summary_md(
            _flatten_plan(merged) + current_errors,
            all_repos=all_repos, passing_repos=passing_repos,
            skipped_repos=skipped_repos, apply=apply,
            skipped_entries=skipped_entries, filter_line=filter_line,
        )
        if failed:
            body = f"# gitbulk prune-branches (FAILED)\n\n{summary}\n\n{body}"
        rs.write_summary(body)
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
