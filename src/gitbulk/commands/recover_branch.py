"""``gitbulk recover-branch`` — restore branches that prune-branches deleted.

Reads the durable audit trail of a prior ``prune-branches`` run (its
``state.yaml``: every ``disposition: deleted`` row carries the branch's tip
``sha``) and re-creates the refs via the GitHub git-ref API. The recovery
mechanics live in :mod:`gitbulk.recover`; this module is the CLI wrapper:
source-run resolution, the dry-run/apply gate, its own audit trail, and the
exit-code contract.

Scope is controlled by arguments on this one command (tick 6lui):

  * ``gitbulk recover-branch`` — restore every branch the latest
    prune-branches run deleted.
  * ``gitbulk recover-branch <slug> [<branch>]`` — narrow to one repo, or
    one branch in it.
  * ``--run <runid>`` — read a specific prune-branches run instead of the
    latest.

Mutating, so it defaults to dry-run; ``--apply`` is the explicit opt-in
(node 2vqp4nk6). Recovery is safe because prune-branches only deletes a
branch whose tip is pinned by ``refs/pull/N/head`` or contained in the
default branch, so the recorded SHA is never GC'd (verified 2026-06-06).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from gitbulk import paths
from gitbulk.gh import ProductionGHClient
from gitbulk.recover import RecoverOutcome, collect_deleted, recover_one
from gitbulk.runstate import RunState

#: The subcommand whose runs we read deletions from.
_SOURCE_SUBCOMMAND = "prune-branches"

EXIT_OK = 0
EXIT_STRUCTURAL_FAILURE = 1
EXIT_ATTENTION_NEEDED = 2


def _resolve_source_run_dir(runid: str | None) -> Path | None:
    """Locate the prune-branches run to recover from.

    ``runid`` ⇒ that exact run; ``None`` ⇒ the latest prune-branches run via
    its ``latest-`` symlink. Returns ``None`` if the run dir (or its
    ``state.yaml``) is absent or the symlink is dangling.
    """
    if runid is not None:
        run_dir = paths.run_dir(runid, _SOURCE_SUBCOMMAND)
        return run_dir if (run_dir / "state.yaml").is_file() else None
    symlink = paths.latest_run_symlink(_SOURCE_SUBCOMMAND)
    try:
        run_dir = symlink.resolve(strict=True)
    except OSError:
        return None
    return run_dir if (run_dir / "state.yaml").is_file() else None


def _load_repos(run_dir: Path) -> dict:
    """The ``repos`` map of a run's ``state.yaml``, or ``{}`` if unreadable —
    a corrupt/partial audit file yields no recoveries, never a crash."""
    try:
        data = yaml.safe_load((run_dir / "state.yaml").read_text())
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict):
        return {}
    repos = data.get("repos")
    return repos if isinstance(repos, dict) else {}


def _outcome_line(o: RecoverOutcome) -> str:
    glyph = {"recovered": "+", "already-present": "=", "failed": "!"}.get(o.status, "?")
    tail = f" — {o.detail}" if o.detail else ""
    return f"  {glyph} {o.slug} {o.branch} @ {o.sha[:12]} [{o.status}]{tail}"


def recover_branch_handler(args: argparse.Namespace) -> int:
    slug = getattr(args, "slug", None)
    branch = getattr(args, "branch", None)
    runid = getattr(args, "run", None)
    apply = bool(getattr(args, "apply", False))

    if branch is not None and slug is None:
        # argparse positionals make this unreachable in normal use; guard the
        # programmatic/Namespace path so a stray branch can't widen scope.
        print(
            "gitbulk recover-branch: a branch requires a slug "
            "(usage: recover-branch <slug> <branch>).",
            file=sys.stderr,
        )
        return EXIT_STRUCTURAL_FAILURE

    run_dir = _resolve_source_run_dir(runid)
    if run_dir is None:
        where = f"run {runid!r}" if runid else "the latest prune-branches run"
        print(
            f"gitbulk recover-branch: no readable state.yaml for {where}. "
            f"Nothing to recover from.",
            file=sys.stderr,
        )
        return EXIT_STRUCTURAL_FAILURE

    deleted = collect_deleted(_load_repos(run_dir), slug=slug, branch=branch)
    scope = f" matching {slug}" + (f" {branch}" if branch else "") if slug else ""
    if not deleted:
        print(f"No deleted branches{scope} recorded in {run_dir.name}.")
        return EXIT_OK

    if not apply:
        print(
            f"Would recover {len(deleted)} branch(es) from {run_dir.name} "
            f"(dry-run; re-run with --apply):"
        )
        for db in deleted:
            print(f"  + {db.slug} {db.branch} @ {db.sha[:12]}")
        return EXIT_OK

    gh = ProductionGHClient()
    rs = RunState.begin(
        "recover-branch",
        argv=list(sys.argv),
        config_snapshot={
            "source_run": run_dir.name,
            "slug": slug,
            "branch": branch,
        },
    )
    outcomes = [recover_one(gh, db) for db in deleted]
    _record(rs, outcomes)

    print(f"Recovered from {run_dir.name}:")
    for o in outcomes:
        print(_outcome_line(o))
    recovered = sum(1 for o in outcomes if o.status == "recovered")
    present = sum(1 for o in outcomes if o.status == "already-present")
    failed = [o for o in outcomes if o.status == "failed"]
    print(
        f"{recovered} recovered, {present} already present, "
        f"{len(failed)} failed."
    )

    exit_code = EXIT_ATTENTION_NEEDED if failed else EXIT_OK
    rs.complete(exit_code)
    return exit_code


def _record(rs: RunState, outcomes: list[RecoverOutcome]) -> None:
    """Persist outcomes to this recover run's audit trail: per-repo rows in
    state.yaml, an errors.log event per failed recovery, and a summary."""
    by_slug: dict[str, list[dict]] = {}
    for o in outcomes:
        by_slug.setdefault(o.slug, []).append(
            {
                "branch": o.branch,
                "sha": o.sha,
                "status": o.status,
                "detail": o.detail,
            }
        )
        if o.status == "failed":
            rs.record_error(
                f"failed to recover {o.slug} {o.branch}: {o.detail}",
                context={
                    "action": "recover-branch",
                    "slug": o.slug,
                    "branch": o.branch,
                    "sha": o.sha,
                },
            )
    rs.set_repos(
        {slug: {"branch_count": len(rows), "branches": rows} for slug, rows in by_slug.items()}
    )
    lines = ["# recover-branch", ""]
    for o in outcomes:
        lines.append(_outcome_line(o))
    rs.write_summary("\n".join(lines) + "\n")
