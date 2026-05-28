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


def _show_handler(args: argparse.Namespace) -> int:
    # Lazy import for the same reason as the other handlers — keeps the
    # locks / paths / runstate-reading machinery out of the --help path.
    from gitbulk.commands.show import show_handler

    return show_handler(args)


_SPECIAL_HANDLERS = {
    "ack": _ack_handler,
    "invariants": _invariants_handler,
    "report": _report_handler,
    "summarize": _summarize_handler,
    "dispatch": _dispatch_handler,
    "merge": _merge_handler,
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
            "before running the report."
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
    sp.add_argument(
        "--filter",
        metavar="LABEL",
        default=None,
        help=(
            "Reserved for Phase 5+: filter eligible PRs by label. "
            "Accepted but currently ignored."
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
            "before running merge."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gitbulk",
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
        elif sc.name == "show":
            _add_show_args(sp)
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
    _set_private_umask()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.subcommand:
        parser.print_help()
        return EXIT_OK
    _apply_config_root(getattr(args, "config_root", None))
    exit_code = args.handler(args)
    _maybe_set_attention(exit_code, args.subcommand)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
