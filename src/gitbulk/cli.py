"""Command-line entry point for gitbulk.

Phase 1C wires real handlers for ``ack`` and ``invariants``; the remaining
subcommands keep returning EXIT_NOT_IMPLEMENTED until their respective
phases land. Exit-code → ATTENTION sentinel fallback per this.i node
``clip7nm4``.
"""

import argparse
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


_SPECIAL_HANDLERS = {
    "ack": _ack_handler,
    "invariants": _invariants_handler,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gitbulk",
        description="Nightly PR triage and fleet maintenance across many GitHub repositories.",
    )
    parser.add_argument("--version", action="version", version=f"gitbulk {__version__}")
    subparsers = parser.add_subparsers(dest="subcommand", metavar="SUBCOMMAND")
    for sc in KNOWN:
        sp = subparsers.add_parser(sc.name, help=sc.help)
        handler = _SPECIAL_HANDLERS.get(sc.name, _not_implemented(sc.name))
        sp.set_defaults(handler=handler)
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


def main(argv: list[str] | None = None) -> int:
    _check_python_version()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.subcommand:
        parser.print_help()
        return EXIT_OK
    exit_code = args.handler(args)
    _maybe_set_attention(exit_code, args.subcommand)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
