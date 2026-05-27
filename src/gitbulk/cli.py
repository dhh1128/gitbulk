"""Command-line entry point for gitbulk.

Phase 0 scaffold: all subcommands are wired into argparse but raise
NotImplementedError. Real behavior arrives in subsequent phases.
"""

import argparse
import sys

from gitbulk import __version__

EXIT_OK = 0
EXIT_STRUCTURAL_FAILURE = 1
EXIT_ATTENTION_NEEDED = 2
EXIT_INVARIANT_SKIPPED = 3
EXIT_OVERRIDES_APPLIED = 4
EXIT_NOT_IMPLEMENTED = 99

SUBCOMMANDS = [
    ("report", "Summarize the state of your open PRs across all repos."),
    ("summarize", "Run Claude over a previous report to prioritize attention."),
    ("dispatch", "Launch headless Claude agents against PRs matching a filter."),
    ("merge", "Auto-merge PRs that satisfy the per-repo merge policy."),
    ("rebase-onto-default", "Rebase your PRs onto their repo's default branch."),
    ("close-stale", "Close PRs that are inactive past the configured threshold."),
    ("show", "Show the latest run of a given subcommand."),
    ("ack", "Clear the ATTENTION sentinel after you have reviewed it."),
    ("invariants", "List the invariant registry and which subcommands use them."),
]


def _check_python_version() -> None:
    if sys.version_info < (3, 10):
        print("gitbulk requires Python 3.10 or later.", file=sys.stderr)
        raise SystemExit(EXIT_STRUCTURAL_FAILURE)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gitbulk",
        description="Nightly PR triage across many GitHub repositories.",
    )
    parser.add_argument("--version", action="version", version=f"gitbulk {__version__}")
    subparsers = parser.add_subparsers(dest="subcommand", metavar="SUBCOMMAND")
    for name, help_text in SUBCOMMANDS:
        sp = subparsers.add_parser(name, help=help_text)
        sp.set_defaults(handler=_not_implemented(name))
    return parser


def _not_implemented(name: str):
    def handler(_args: argparse.Namespace) -> int:
        print(
            f"gitbulk: subcommand '{name}' is not yet implemented (Phase 0 scaffold).",
            file=sys.stderr,
        )
        return EXIT_NOT_IMPLEMENTED

    return handler


def main(argv: list[str] | None = None) -> int:
    _check_python_version()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.subcommand:
        parser.print_help()
        return EXIT_OK
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
