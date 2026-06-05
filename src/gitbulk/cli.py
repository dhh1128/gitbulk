"""Command-line entry point for gitbulk.

Phase 1C wires real handlers for ``ack`` and ``invariants``; the remaining
subcommands keep returning EXIT_NOT_IMPLEMENTED until their respective
phases land. Exit-code → ATTENTION sentinel fallback per this.i node
``clip7nm4``.
"""

import argparse
import logging
import os
import sys

from gitbulk import __version__
from gitbulk.subcommands import KNOWN, Subcommand  # noqa: F401  (Subcommand re-export)
from gitbulk.util.style import error_line

EXIT_OK = 0
EXIT_STRUCTURAL_FAILURE = 1
EXIT_ATTENTION_NEEDED = 2
EXIT_INVARIANT_SKIPPED = 3
EXIT_OVERRIDES_APPLIED = 4
EXIT_NOT_IMPLEMENTED = 99

# Back-compat alias. The canonical, typed source is ``gitbulk.subcommands.KNOWN``;
# this exposes the same data shaped as a list of ``(name, help)`` tuples for any
# pre-Phase-1D consumer. New code should import KNOWN.
SUBCOMMANDS = [(s.name, s.help) for s in KNOWN]

_ATTENTION_TRIGGER_CODES = {EXIT_ATTENTION_NEEDED, EXIT_INVARIANT_SKIPPED}


def _check_python_version() -> None:
    if sys.version_info < (3, 10):
        print("gitbulk requires Python 3.10 or later.", file=sys.stderr)
        raise SystemExit(EXIT_STRUCTURAL_FAILURE)


def _configure_logging() -> None:
    """Wire a single stderr handler onto the ``gitbulk`` logger tree.

    The level is INFO by default; the ``GITBULK_LOG_LEVEL`` env var (case-
    insensitive: DEBUG / INFO / WARNING / ERROR / CRITICAL) can override.
    An invalid value silently falls back to INFO rather than raising — a
    misconfigured log level should not block a cron job.

    We attach the handler to ``logging.getLogger("gitbulk")`` (not the root
    logger), and we leave ``propagate=True`` so test runners that hook the
    root logger (pytest's caplog) still see gitbulk's messages. In cron
    production the root logger has no handler, so no duplication occurs.

    Without this configuration, locks.py and any other module that calls
    ``logging.getLogger("gitbulk.<sub>")`` would emit into the void; this
    helper makes ``_log.debug(...)`` actually reach stderr at 2 a.m.
    """
    level_name = os.environ.get("GITBULK_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        level = logging.INFO

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )

    gitbulk_logger = logging.getLogger("gitbulk")
    gitbulk_logger.setLevel(level)
    # Idempotent: if main() is invoked twice in the same process (e.g. in
    # tests), don't stack handlers.
    if not any(
        isinstance(h, logging.StreamHandler) and h.stream is sys.stderr
        for h in gitbulk_logger.handlers
    ):
        gitbulk_logger.addHandler(handler)


def _not_implemented(name: str):
    def handler(_args: argparse.Namespace) -> int:
        print(
            f"gitbulk: subcommand '{name}' is not yet implemented (Phase 0 scaffold).",
            file=sys.stderr,
        )
        return EXIT_NOT_IMPLEMENTED

    return handler


def _ack_handler(_args: argparse.Namespace) -> int:
    from gitbulk import sentinel

    if sentinel.clear_attention():
        print("ATTENTION sentinel cleared.")
    else:
        print("No ATTENTION sentinel was set.")
    return EXIT_OK


def _invariants_handler(_args: argparse.Namespace) -> int:
    from gitbulk import invariants

    registered = invariants.all_invariants()
    if not registered:
        print(
            "No invariants registered yet. "
            "(Concrete invariants land in Phase 2 and later.)"
        )
        return EXIT_OK
    for name in sorted(registered):
        cls = registered[name]
        subs = ", ".join(sorted(cls.subcommands))
        print(f"{name}  [{cls.kind.value}]  applies-to: {subs}")
    return EXIT_OK


def _report_handler(args: argparse.Namespace) -> int:
    # Lazy import keeps cli.py import-light and avoids dragging in
    # gh / runstate / invariants for every `gitbulk --help` invocation.
    from gitbulk.commands.report import report_handler

    return report_handler(args)


def _summarize_handler(args: argparse.Namespace) -> int:
    # Lazy import for the same reason as _report_handler — claude.py
    # and runstate stay out of the --help path.
    from gitbulk.commands.summarize import summarize_handler

    return summarize_handler(args)


def _dispatch_handler(args: argparse.Namespace) -> int:
    # Lazy import keeps the exec kernel + worktree + claude modules
    # out of the --help path (same reason as _report_handler).
    from gitbulk.commands.dispatch import dispatch_handler

    return dispatch_handler(args)


def _merge_handler(args: argparse.Namespace) -> int:
    # Lazy import — keeps the merge pipeline (locks, runstate, gh) out
    # of the --help path. Same pattern as the other handlers.
    from gitbulk.commands.merge import merge_handler

    return merge_handler(args)


def _close_stale_handler(args: argparse.Namespace) -> int:
    # Lazy import for the same reason as the other handlers.
    from gitbulk.commands.close_stale import close_stale_handler

    return close_stale_handler(args)


def _rebase_pr_handler(args: argparse.Namespace) -> int:
    # Lazy import — keeps the worktree/rebase machinery out of --help.
    from gitbulk.commands.rebase_pr import rebase_pr_handler

    return rebase_pr_handler(args)


def _prune_branches_handler(args: argparse.Namespace) -> int:
    # Lazy import — keeps the prune pipeline out of the --help path.
    from gitbulk.commands.prune_branches import prune_branches_handler

    return prune_branches_handler(args)


def _prune_worktrees_handler(args: argparse.Namespace) -> int:
    # Lazy import (worktree + git helpers) for the same reason.
    from gitbulk.commands.prune_worktrees import prune_worktrees_handler

    return prune_worktrees_handler(args)


def _show_handler(args: argparse.Namespace) -> int:
    # Lazy import for the same reason as the other handlers — keeps the
    # locks / paths / runstate-reading machinery out of the --help path.
    from gitbulk.commands.show import show_handler

    return show_handler(args)


def _install_handler(args: argparse.Namespace) -> int:
    # Lazy import — keeps install.py out of the --help path.
    from pathlib import Path

    from gitbulk.install import (
        InstallError,
        PathStatus,
        default_target_dir,
        install_self,
        print_manual_instructions,
        resolve_default_source,
    )

    try:
        source = (
            Path(args.source).resolve() if args.source
            else resolve_default_source(sys.argv[0])
        )
        target_dir = Path(args.dir).expanduser() if args.dir else default_target_dir()
        result = install_self(source=source, target_dir=target_dir)
    except InstallError as exc:
        print(f"install failed: {exc}", file=sys.stderr)
        print_manual_instructions(sys.stderr)
        return EXIT_STRUCTURAL_FAILURE
    print(f"installed {result.target}")
    if result.path_status is PathStatus.NOT_ON_PATH and result.hint:
        print(f"NOTE: {result.target.parent} is not on PATH.")
        print(f"      {result.hint}")
    return EXIT_OK


def _bundle_handler(args: argparse.Namespace) -> int:
    # Lazy import — bundle.py pulls in zipapp/yaml only when actually building.
    from pathlib import Path

    from gitbulk.bundle import build_single_file

    output = build_single_file(Path(args.output))
    print(f"wrote {output}")
    return EXIT_OK


def _update_handler(args: argparse.Namespace) -> int:
    # Lazy import — keeps update.py (subprocess/urllib) out of the --help path.
    from pathlib import Path

    from gitbulk.update import (
        DEFAULT_UPDATE_MANIFEST_URL,
        UpdateError,
        apply_update,
        check_update,
        resolve_update_target,
        running_as_zipapp,
        suggested_update_command,
    )

    manifest_path = args.manifest or DEFAULT_UPDATE_MANIFEST_URL
    target = (
        Path(args.target).resolve() if args.target
        else resolve_update_target(sys.argv[0])
    )

    if args.check:
        try:
            status = check_update(manifest_path)
        except (UpdateError, OSError, ValueError, KeyError) as exc:
            print(f"update check failed: {exc}", file=sys.stderr)
            return EXIT_STRUCTURAL_FAILURE
        if status.update_available:
            print(
                f"A newer version of gitbulk is available: "
                f"{status.current_version} -> {status.latest_version}."
            )
            print(
                "Update with: "
                f"{suggested_update_command(target=args.target, manifest=manifest_path)}"
            )
        else:
            print(f"gitbulk is current: {status.current_version}")
        # --check is purely informational; it does not overload gitbulk's
        # exit-code contract (1=structural, 2=attention, ...). The message
        # carries the available/current state.
        return EXIT_OK

    # Apply. Per node updtg6qn, refuse to clobber a pip/pipx install: only
    # the standalone zipapp is safe to self-replace.
    if not running_as_zipapp(target):
        print(
            "gitbulk looks pip-installed (not the standalone binary), so "
            "`gitbulk update` will not replace it.\n"
            "  Upgrade with:  pip install -U gitbulk   "
            "(or, if you used pipx:  pipx upgrade gitbulk)",
            file=sys.stderr,
        )
        return EXIT_STRUCTURAL_FAILURE
    try:
        status = apply_update(target=target, manifest_path=manifest_path)
    except UpdateError as exc:
        print(f"update failed: {exc}", file=sys.stderr)
        return EXIT_STRUCTURAL_FAILURE
    if status.update_available:
        print(f"updated {target}: {status.current_version} -> {status.latest_version}")
    else:
        print(f"gitbulk is current: {status.current_version}")
    return EXIT_OK


_SPECIAL_HANDLERS = {
    "ack": _ack_handler,
    "invariants": _invariants_handler,
    "report": _report_handler,
    "summarize": _summarize_handler,
    "dispatch": _dispatch_handler,
    "merge": _merge_handler,
    "close-stale": _close_stale_handler,
    "rebase-pr": _rebase_pr_handler,
    "prune-branches": _prune_branches_handler,
    "prune-worktrees": _prune_worktrees_handler,
    "show": _show_handler,
}


def _add_report_args(sp: argparse.ArgumentParser) -> None:
    """Argparse flags specific to the ``report`` subcommand."""
    sp.add_argument(
        "--code-root",
        metavar="PATH",
        default=None,
        help="Override default ~/code/ where local clones live.",
    )
    sp.add_argument(
        "--skip-check",
        metavar="NAME",
        action="append",
        default=None,
        help=(
            "Skip the named invariant for this run (may be passed more "
            "than once). Logs a WARNING and triggers exit-code 4 if no "
            "other concern fires."
        ),
    )
    sp.add_argument(
        "--refresh-org-members",
        action="store_true",
        default=False,
        help=(
            "Force a fresh fetch of the configured humans.org members "
            "even when the cache is still within its TTL. report "
            "auto-refreshes a missing or stale cache on its own; this "
            "flag forces a refetch regardless."
        ),
    )


def _add_summarize_args(sp: argparse.ArgumentParser) -> None:
    """Argparse flags specific to the ``summarize`` subcommand.

    Per this.i node ``smprmpt4n.e`` and ``.f``, the user can A/B test
    alternate prompt files and models without editing the package.
    """
    sp.add_argument(
        "--prompt",
        metavar="PATH",
        default=None,
        help=(
            "Override the default packaged triage prompt with a "
            "different file. Path is read at run time and recorded in "
            "the run's manifest.yaml."
        ),
    )
    sp.add_argument(
        "--model",
        metavar="NAME",
        default=None,
        help=(
            "Override the default claude model "
            "(default: claude-sonnet-4-6). Accepts an alias like "
            "'opus' or a full model name."
        ),
    )
    sp.add_argument(
        "--agent",
        metavar="NAME",
        default=None,
        help=(
            "Coding agent to drive (a built-in preset like 'claude', "
            "'gemini', 'copilot', 'cursor', or a profile defined under "
            "'agents:' in gitbulk.yaml). Overrides 'default_agent'. "
            "Default: claude."
        ),
    )


def _add_show_args(sp: argparse.ArgumentParser) -> None:
    """Argparse flags specific to the ``show`` subcommand.

    Positional ``subcommand`` is optional: omitted ⇒ dashboard. The
    mutually-exclusive flag group selects which artifact to print;
    default (no flag) is ``summary.md``. See commands/show.py for the
    exit-code contract.
    """
    sp.add_argument(
        "show_subcommand",
        metavar="SUBCOMMAND",
        nargs="?",
        default=None,
        help=(
            "Name of the subcommand whose latest run to inspect "
            "(e.g. report, summarize, dispatch). Omit to print the "
            "dashboard."
        ),
    )
    group = sp.add_mutually_exclusive_group()
    group.add_argument(
        "--state",
        action="store_true",
        default=False,
        help="Print state.yaml (full structured per-repo decisions).",
    )
    group.add_argument(
        "--invariants",
        action="store_true",
        default=False,
        help="Print invariants.log (JSONL, one event per check).",
    )
    group.add_argument(
        "--errors",
        action="store_true",
        default=False,
        help="Print errors.log (JSONL, one event per error/warning).",
    )
    group.add_argument(
        "--manifest",
        action="store_true",
        default=False,
        help="Print manifest.yaml (argv, config snapshot, version).",
    )
    group.add_argument(
        "--path",
        action="store_true",
        default=False,
        help="Print the run directory path itself (handy for scripting).",
    )


def _add_dispatch_args(sp: argparse.ArgumentParser) -> None:
    """Argparse flags specific to the ``dispatch`` subcommand.

    Per node ``2vqp4nk6``, dispatch is mutating and defaults to dry-run;
    ``--apply`` is the explicit opt-in. The other flags mirror the
    knobs exposed by :func:`gitbulk.exec.execute_targets` (concurrency,
    per-target timeout) plus the prompt path and the report-style
    ``--code-root`` / ``--skip-check`` controls.
    """
    sp.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help=(
            "Actually run claude against eligible PRs. Without this flag, "
            "dispatch is a dry run and prints what it WOULD do (per "
            "AGENTS.md 'Mutating subcommands default to dry-run')."
        ),
    )
    sp.add_argument(
        "--prompt",
        metavar="PATH",
        default=None,
        help=(
            "Required. Path to the prompt file that will be sent to "
            "claude inside each PR's worktree. Must exist and be "
            "non-empty."
        ),
    )
    sp.add_argument(
        "--concurrency",
        metavar="N",
        type=int,
        default=2,
        help="Bounded-pool size for parallel claude children (default 2).",
    )
    sp.add_argument(
        "--timeout",
        metavar="SECONDS",
        type=float,
        default=1800.0,
        help="Per-target wall-clock timeout (default 1800s).",
    )
    # NB: the real --filter / --org / --repo / ... filter flags are added
    # by the shared _add_filter_args (this.i flt7arg2), which superseded
    # the old reserved-but-ignored dispatch --filter LABEL placeholder.
    sp.add_argument(
        "--code-root",
        metavar="PATH",
        default=None,
        help="Override default ~/code/ where local clones live.",
    )
    sp.add_argument(
        "--skip-check",
        metavar="NAME",
        action="append",
        default=None,
        help=(
            "Skip the named invariant for this run (may be passed more "
            "than once). Logs a WARNING and triggers exit-code 4 if no "
            "other concern fires."
        ),
    )
    sp.add_argument(
        "--refresh-org-members",
        action="store_true",
        default=False,
        help=(
            "Force a fresh fetch of the configured humans.org members "
            "even when the cache is still within its TTL. dispatch "
            "auto-refreshes a missing or stale cache on its own; this "
            "flag forces a refetch regardless."
        ),
    )
    sp.add_argument(
        "--model",
        metavar="NAME",
        default=None,
        help=(
            "Override the agent's default model for this run (e.g. 'opus'). "
            "Applies to whichever agent is selected."
        ),
    )
    sp.add_argument(
        "--agent",
        metavar="NAME",
        default=None,
        help=(
            "Coding agent to drive inside each worktree (a built-in preset "
            "like 'claude', 'gemini', 'copilot', 'cursor', or a profile "
            "defined under 'agents:' in gitbulk.yaml). Overrides "
            "'default_agent' and any per-repo 'agent:'. Default: claude."
        ),
    )
    sp.add_argument(
        "--allow-foreign-authors",
        action="store_true",
        default=False,
        help=(
            "Allow dispatching the agent against PRs NOT authored by you. By "
            "default such PRs are skipped, because the agent reads/operates on "
            "attacker-controllable PR content (SEC-F3). This flag is REFUSED in "
            "unattended/cron mode (no TTY) — it is interactive-only."
        ),
    )


def _add_close_stale_args(sp: argparse.ArgumentParser) -> None:
    """Argparse flags for ``close-stale``. Mirrors merge: --apply opt-in,
    --code-root, --skip-check, --refresh-org-members."""
    sp.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help=(
            "Actually post stale warnings and close PRs whose cooloff "
            "has elapsed. Without this flag, close-stale is a dry run "
            "and prints what it WOULD do (per AGENTS.md 'Mutating "
            "subcommands default to dry-run')."
        ),
    )
    sp.add_argument(
        "--code-root",
        metavar="PATH",
        default=None,
        help="Override default ~/code/ where local clones live.",
    )
    sp.add_argument(
        "--skip-check",
        metavar="NAME",
        action="append",
        default=None,
        help=(
            "Skip the named invariant for this run (may be passed more "
            "than once). Logs a WARNING and triggers exit-code 4 if no "
            "other concern fires."
        ),
    )
    sp.add_argument(
        "--refresh-org-members",
        action="store_true",
        default=False,
        help=(
            "Force a fresh fetch of the configured humans.org members "
            "even when the cache is still within its TTL. close-stale "
            "auto-refreshes a missing or stale cache on its own; this "
            "flag forces a refetch regardless."
        ),
    )


def _add_filter_args(sp: argparse.ArgumentParser) -> None:
    """Shared fleet-subset filter flags (this.i node ``flt7arg2``).

    Applied to every subcommand that fetches + acts on PRs (report,
    merge, close-stale, rebase-pr, dispatch). Repo filters (--org,
    --repo) prune the repo set before the invariant loop; PR filters
    (--base, --mergeable-state) prune after the fetch; --author is
    pushed into the search. --filter names a config-defined set that
    CLI flags then narrow.
    """
    sp.add_argument(
        "--filter", metavar="NAME", default=None,
        help="Use a named filter set from gitbulk.yaml `filters:`; CLI flags narrow it.",
    )
    sp.add_argument(
        "--org", metavar="OWNER", action="append", default=None,
        help="Limit to repos under this GitHub owner (repeatable).",
    )
    sp.add_argument(
        "--repo", metavar="GLOB", action="append", default=None,
        help="Limit to repos whose owner/repo slug matches this glob (repeatable).",
    )
    sp.add_argument(
        "--base", metavar="BRANCH", action="append", default=None,
        help="Limit to PRs targeting this base branch (repeatable).",
    )
    sp.add_argument(
        "--mergeable-state", metavar="STATE", action="append", default=None,
        help="Limit to PRs with this mergeable_state (CLEAN/DIRTY/BEHIND/BLOCKED/...; repeatable).",
    )
    sp.add_argument(
        "--author", metavar="LOGIN", action="append", default=None,
        help=(
            "Limit to PRs by this author (default: you). Widening to other "
            "authors is read-only on report; mutating commands restrict per "
            "their own rules (rebase-pr refuses non-self)."
        ),
    )


def _add_rebase_pr_args(sp: argparse.ArgumentParser) -> None:
    """Argparse flags for ``rebase-pr``. Mirrors merge/close-stale:
    --apply opt-in, --code-root, --skip-check, --refresh-org-members."""
    sp.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help=(
            "Actually rebase eligible PRs and force-push (with lease). "
            "Without this flag, rebase-pr is a dry run and prints what "
            "it WOULD rebase (per AGENTS.md 'Mutating subcommands default "
            "to dry-run')."
        ),
    )
    sp.add_argument(
        "--code-root",
        metavar="PATH",
        default=None,
        help="Override default ~/code/ where local clones live.",
    )
    sp.add_argument(
        "--skip-check",
        metavar="NAME",
        action="append",
        default=None,
        help=(
            "Skip the named invariant for this run (may be passed more "
            "than once). Logs a WARNING and triggers exit-code 4 if no "
            "other concern fires."
        ),
    )
    sp.add_argument(
        "--refresh-org-members",
        action="store_true",
        default=False,
        help=(
            "Force a fresh fetch of the configured humans.org members "
            "even when the cache is still within its TTL. rebase-pr "
            "auto-refreshes a missing or stale cache on its own; this "
            "flag forces a refetch regardless."
        ),
    )


def _add_prune_common_args(sp: argparse.ArgumentParser, *, what: str) -> None:
    """Shared flags for the two prune subcommands: --apply opt-in,
    --code-root, --skip-check, --refresh-org-members. ``what`` fills the
    --apply help text."""
    sp.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help=(
            f"Actually {what}. Without this flag the command is a dry run "
            "and prints what it WOULD do (per AGENTS.md 'Mutating "
            "subcommands default to dry-run'). The guardrails are designed "
            "to make --apply safe to run unattended."
        ),
    )
    sp.add_argument(
        "--code-root",
        metavar="PATH",
        default=None,
        help="Override default ~/code/ where local clones live.",
    )
    sp.add_argument(
        "--skip-check",
        metavar="NAME",
        action="append",
        default=None,
        help=(
            "Skip the named invariant for this run (may be passed more "
            "than once). Logs a WARNING and triggers exit-code 4 if no "
            "other concern fires."
        ),
    )
    sp.add_argument(
        "--refresh-org-members",
        action="store_true",
        default=False,
        help=(
            "Force a fresh fetch of the configured humans.org members even "
            "when the cache is still within its TTL."
        ),
    )


def _add_prune_branches_args(sp: argparse.ArgumentParser) -> None:
    """Flags for ``prune-branches`` (node prnbr4kq)."""
    _add_prune_common_args(
        sp, what="delete remote branches whose only PRs are merged/closed"
    )
    sp.add_argument(
        "--concurrency",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Parallel workers for the branch scan (node prnpf8nq). Defaults "
            "to the policy's prune_scan_concurrency (12). Lower it if GitHub "
            "secondary-rate-limits you; 1 forces a sequential scan."
        ),
    )


def _add_prune_worktrees_args(sp: argparse.ArgumentParser) -> None:
    """Flags for ``prune-worktrees`` (node prnwt5nq)."""
    _add_prune_common_args(
        sp, what="remove local worktrees whose branch's PRs are merged/closed"
    )
    sp.add_argument(
        "--include-untracked",
        action="store_true",
        default=False,
        help=(
            "Also remove a worktree that has untracked (but no tracked-file) "
            "changes. By default an untracked file blocks removal."
        ),
    )


def _add_merge_args(sp: argparse.ArgumentParser) -> None:
    """Argparse flags specific to the ``merge`` subcommand.

    Per node ``2vqp4nk6``, merge is mutating and defaults to dry-run;
    ``--apply`` is the explicit opt-in. Other knobs mirror the report /
    dispatch flag shape (``--code-root``, ``--skip-check``,
    ``--refresh-org-members``).
    """
    sp.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help=(
            "Actually merge eligible PRs via ``gh pr merge``. Without "
            "this flag, merge is a dry run and prints what it WOULD "
            "merge (per AGENTS.md 'Mutating subcommands default to "
            "dry-run')."
        ),
    )
    sp.add_argument(
        "--approve",
        action="store_true",
        default=False,
        help=(
            "Post an approving review (as you) on eligible bot PRs, then "
            "merge. Requires --apply to act; in dry-run it only reports "
            "what it would approve. Auto-approves only bot authors "
            "(policy.bots) unless --approve-author widens it."
        ),
    )
    sp.add_argument(
        "--approve-author",
        metavar="LOGIN",
        action="append",
        default=None,
        help=(
            "Additional non-bot author login(s) that --approve may "
            "auto-approve. Repeatable. Without this, only configured "
            "bots are auto-approved."
        ),
    )
    sp.add_argument(
        "--code-root",
        metavar="PATH",
        default=None,
        help="Override default ~/code/ where local clones live.",
    )
    sp.add_argument(
        "--skip-check",
        metavar="NAME",
        action="append",
        default=None,
        help=(
            "Skip the named invariant for this run (may be passed more "
            "than once). Logs a WARNING and triggers exit-code 4 if no "
            "other concern fires."
        ),
    )
    sp.add_argument(
        "--refresh-org-members",
        action="store_true",
        default=False,
        help=(
            "Force a fresh fetch of the configured humans.org members "
            "even when the cache is still within its TTL. merge "
            "auto-refreshes a missing or stale cache on its own; this "
            "flag forces a refetch regardless."
        ),
    )


# Self-management commands never trigger the update notice: it is pointless
# (and confusing) to advertise an update right before update/install/bundle.
_SELF_MANAGEMENT_COMMANDS = frozenset({"install", "update", "bundle"})


def _stream_isatty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def _configure_lock_status() -> None:
    """Install the interactive lock-status reporter when appropriate.

    Auto-on only when stderr is a TTY (so cron / pipes stay silent); the live
    "waiting on <lock> — Ns left" notice would be garbled or noisy otherwise.
    ``GITBULK_LOCK_STATUS=off`` disables it. Node rsclk7nq UX.
    """
    if os.environ.get("GITBULK_LOCK_STATUS", "").strip().lower() == "off":
        return
    if not _stream_isatty(sys.stderr):
        return
    from gitbulk.locks import set_status_reporter
    from gitbulk.util.lockstatus import TtyLockStatusReporter

    set_status_reporter(TtyLockStatusReporter())


def maybe_print_update_notice(
    subcommand: str,
    *,
    no_update_check: bool = False,
    stream=None,
    isatty: bool | None = None,
    checker=None,
) -> None:
    """Print a TTY-gated 'newer version available' notice (this.i node ``updnc5kr``).

    This never replaces the binary and never fires for the self-management
    commands. The TTY gate — together with ``--no-update-check`` and
    ``GITBULK_NO_UPDATE_CHECK=1`` (which ``bin/gitbulk-cron`` exports) —
    keeps cron logs silent, and keeps the offline-tests rule intact: the
    network check is reached only on an interactive terminal.
    """
    if stream is None:
        stream = sys.stderr
    if subcommand in _SELF_MANAGEMENT_COMMANDS:
        return
    if no_update_check or os.environ.get("GITBULK_NO_UPDATE_CHECK") == "1":
        return
    tty = isatty if isatty is not None else _stream_isatty(stream)
    if not tty:
        return
    try:
        from gitbulk.update import (
            DEFAULT_UPDATE_MANIFEST_URL,
            check_update,
            suggested_update_command,
        )

        check = checker if checker is not None else check_update
        status = check(DEFAULT_UPDATE_MANIFEST_URL)
    except Exception:
        # A failed check (offline, gh missing, malformed manifest) must never
        # block or crash the command the user actually asked for.
        return
    if not status.update_available:
        return
    print(
        f"A newer version of gitbulk is available: "
        f"{status.current_version} -> {status.latest_version}.",
        file=stream,
    )
    print(f"Update with: {suggested_update_command()}", file=stream)


class _HelpFormatter(argparse.HelpFormatter):
    """Truly hide subcommands/flags whose help is SUPPRESS.

    Plain ``help=argparse.SUPPRESS`` still lists a subparser's name in the
    help table on some Python versions; overriding ``_format_action`` drops
    it entirely (e.g. the release-time ``bundle`` command and ``install``'s
    internal ``--source``).
    """

    def _format_action(self, action: argparse.Action) -> str:
        if action.help == argparse.SUPPRESS:
            return ""
        return super()._format_action(action)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gitbulk",
        formatter_class=_HelpFormatter,
        description="Nightly PR triage and fleet maintenance across many GitHub repositories.",
    )
    parser.add_argument("--version", action="version", version=f"gitbulk {__version__}")
    parser.add_argument(
        "--config-root",
        metavar="PATH",
        default=None,
        help=(
            "Override the default ~/.config/gitbulk/ location. Honored by "
            "subcommands that read the user config."
        ),
    )
    parser.add_argument(
        "--no-update-check",
        action="store_true",
        default=False,
        help=(
            "Skip the interactive check for a newer gitbulk release "
            "(also set via GITBULK_NO_UPDATE_CHECK=1)."
        ),
    )
    subparsers = parser.add_subparsers(dest="subcommand", metavar="SUBCOMMAND")
    for sc in KNOWN:
        sp = subparsers.add_parser(sc.name, help=sc.help)
        handler = _SPECIAL_HANDLERS.get(sc.name, _not_implemented(sc.name))
        sp.set_defaults(handler=handler)
        if sc.name == "report":
            _add_report_args(sp)
        elif sc.name == "summarize":
            _add_summarize_args(sp)
        elif sc.name == "dispatch":
            _add_dispatch_args(sp)
        elif sc.name == "merge":
            _add_merge_args(sp)
        elif sc.name == "close-stale":
            _add_close_stale_args(sp)
        elif sc.name == "rebase-pr":
            _add_rebase_pr_args(sp)
        elif sc.name == "prune-branches":
            _add_prune_branches_args(sp)
        elif sc.name == "prune-worktrees":
            _add_prune_worktrees_args(sp)
        elif sc.name == "show":
            _add_show_args(sp)
        # Fleet-subset filters apply to every PR-fetching subcommand.
        if sc.name in (
            "report", "dispatch", "merge", "close-stale", "rebase-pr",
            "prune-branches", "prune-worktrees",
        ):
            _add_filter_args(sp)

    # Self-management commands (this.i node dstbr5kq) are NOT fleet
    # operations: they take no locks, run no invariants, and are absent from
    # KNOWN / the dashboard. They are wired as standalone subparsers here.
    install_sp = subparsers.add_parser(
        "install", help="Copy the running gitbulk binary onto your PATH."
    )
    install_sp.add_argument("--dir", help="Install directory (default: ~/.local/bin).")
    install_sp.add_argument("--source", help=argparse.SUPPRESS)
    install_sp.set_defaults(handler=_install_handler)

    update_sp = subparsers.add_parser(
        "update", help="Update gitbulk to the latest release (or check)."
    )
    update_sp.add_argument("--manifest", help="Override the built-in release manifest URL.")
    update_sp.add_argument("--check", action="store_true", help="Only check; do not replace.")
    update_sp.add_argument("--target", help="Override the running binary path to replace.")
    update_sp.set_defaults(handler=_update_handler)

    # bundle is a release-time internal tool; hidden from the help listing.
    bundle_sp = subparsers.add_parser("bundle", help=argparse.SUPPRESS)
    bundle_sp.add_argument("output", help="Output path for the zipapp.")
    bundle_sp.set_defaults(handler=_bundle_handler)
    return parser


def _maybe_set_attention(exit_code: int, subcommand: str) -> None:
    """If the exit code warrants ATTENTION and no handler set one, write a fallback."""
    if exit_code not in _ATTENTION_TRIGGER_CODES:
        return
    from gitbulk import sentinel

    if sentinel.has_attention():
        return
    sentinel.set_attention(
        exit_code,
        subcommand,
        "?",
        "set by main(); handler did not write its own sentinel",
    )


def _maybe_clear_superseded(exit_code: int, subcommand: str) -> None:
    """A clean run of an attention-producing subcommand clears its own
    stale sentinel (this.i node ``aklr5pq3`` trigger 3).

    Only exit 0 supersedes — a non-zero exit means the run did not complete
    cleanly, so it cannot claim the prior concern is resolved. Only the
    SAME subcommand's sentinel is cleared; a clean ``report`` must not
    dismiss a ``dispatch`` failure (clip7nm4's cross-subcommand concern).
    show/ack/invariants never set attention, so they are excluded.
    """
    if exit_code != EXIT_OK:
        return
    from gitbulk.subcommands import ATTENTION_PRODUCING_NAMES

    if subcommand not in ATTENTION_PRODUCING_NAMES:
        return
    from gitbulk import sentinel

    if sentinel.clear_if_superseded(subcommand) is not None:
        print(
            f"gitbulk {subcommand}: cleared a stale ATTENTION sentinel "
            f"(superseded by this clean run).",
            file=sys.stderr,
        )


def _apply_config_root(config_root: str | None) -> None:
    """If --config-root was passed, pin ``paths.config_dir()`` to it.

    The user's flag value points at the ``gitbulk/`` directory itself
    (mirroring ~/.config/gitbulk). The override is held inside the
    ``paths`` module rather than via ``os.environ["XDG_CONFIG_HOME"]``
    so that child processes — ``gh``, ``claude`` — DO NOT inherit the
    override. Both tools also respect XDG_CONFIG_HOME for their own
    credential / config lookups; mutating that env var would cause
    them to lose their auth (smoke-test finding 2026-05-28).
    """
    if config_root is None:
        return
    from pathlib import Path

    from gitbulk import paths

    paths.set_config_dir_override(Path(os.path.expanduser(config_root)).resolve())


def _set_private_umask() -> None:
    """Apply ``os.umask(0o077)`` so every file gitbulk creates is owner-only.

    Resolves security-hawk F3 (2026-05-28): default umask leaves
    ``~/.cache/gitbulk/`` artifacts world-readable, which is acceptable on
    a single-user dev box but exposed on shared hosts / bind-mounted
    containers. The umask is process-global so this is the right and only
    place to set it: once, at CLI startup.
    """
    os.umask(0o077)


def main(argv: list[str] | None = None) -> int:
    _check_python_version()
    _configure_logging()
    _configure_lock_status()
    _set_private_umask()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.subcommand:
        parser.print_help()
        return EXIT_OK
    maybe_print_update_notice(
        args.subcommand,
        no_update_check=getattr(args, "no_update_check", False),
    )
    _apply_config_root(getattr(args, "config_root", None))
    # User-facing config errors must not stack-trace: they're caused by
    # a missing or malformed config file and the actionable info is the
    # message itself, not a Python frame. Caught here at the top of the
    # CLI so every subcommand inherits the friendly behavior.
    from gitbulk.config.repos import ConfigError as _ConfigError
    try:
        exit_code = args.handler(args)
    except _ConfigError as e:
        print(error_line(f"gitbulk {args.subcommand}: {e}"), file=sys.stderr)
        return EXIT_STRUCTURAL_FAILURE
    _maybe_set_attention(exit_code, args.subcommand)
    _maybe_clear_superseded(exit_code, args.subcommand)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
