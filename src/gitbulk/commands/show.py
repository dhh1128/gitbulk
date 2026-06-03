"""``gitbulk show`` — inspect the most recent run of a given subcommand.

Read-only surface over the per-run audit trail that :mod:`gitbulk.runstate`
writes. Operators read the dashboard or a specific artifact (summary.md,
state.yaml, invariants.log, errors.log, manifest.yaml, or just the
run-dir path) without having to remember the cache layout themselves.

Per this.i node ``tmlk5pq3``, this is a read-only subcommand: it takes
the global *shared* lock with a 300-second timeout so a concurrent
mutating run cannot swap the ``latest-<subcommand>`` symlink under us
mid-read. It does NOT call :meth:`RunState.begin` — no new run dir is
created; the handler purely consumes prior runs (node ``kp7nw4mq``).

Exit codes:
  0 EXIT_OK                 — printed the requested artifact
  1 EXIT_STRUCTURAL_FAILURE — unknown subcommand, no prior run, missing
                              artifact, or lock timeout
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from gitbulk import paths, sentinel
from gitbulk.locks import LockTimeoutError, global_lock
from gitbulk.subcommands import NAMES
from gitbulk.util.style import error_line

# Exit codes — duplicated here (instead of importing from cli.py) so the
# cli → commands dep stays one-way (same pattern as report.py).
EXIT_OK = 0
EXIT_STRUCTURAL_FAILURE = 1

#: Per node ``tmlk5pq3``: read-only subcommands get a 300s lock budget.
_LOCK_TIMEOUT_SECONDS: float = 300.0

#: Mapping of CLI flag → artifact filename inside the run dir. ``--path``
#: is handled separately (it doesn't read a file, just prints the dir).
_ARTIFACT_FILES: dict[str, str] = {
    "summary": "summary.md",
    "state": "state.yaml",
    "invariants": "invariants.log",
    "errors": "errors.log",
    "manifest": "manifest.yaml",
}


def _selected_artifact(args: argparse.Namespace) -> str:
    """Resolve the mutually-exclusive flag group to a single artifact key.

    Argparse already enforces at-most-one via the mutually-exclusive group;
    this helper picks the matching key or defaults to ``"summary"``.
    ``--path`` is treated as its own pseudo-artifact handled by the caller.
    """
    for key in ("state", "invariants", "errors", "manifest", "path"):
        if getattr(args, key, False):
            return key
    return "summary"


def _resolve_latest_run_dir(subcommand: str) -> Path | None:
    """Follow the ``latest-<subcommand>`` symlink to the actual run dir.

    Returns ``None`` if the symlink is absent or dangling. Mirrors the
    defensive resolution dashboard.py uses (a symlink can outlive its
    target if the user nukes ~/.cache/gitbulk/runs/ manually).
    """
    symlink = paths.latest_run_symlink(subcommand)
    if not (symlink.is_symlink() or symlink.exists()):
        return None
    try:
        return symlink.resolve(strict=True)
    except (FileNotFoundError, OSError):
        return None


def _runid_from_run_dir(run_dir: Path, subcommand: str) -> str | None:
    """Strip the ``-<subcommand>`` suffix off a run dir name to recover the
    runid, or None if the name does not carry that suffix (defensive)."""
    name = run_dir.name
    suffix = f"-{subcommand}"
    return name[: -len(suffix)] if name.endswith(suffix) else None


def _note_cleared(payload: dict) -> None:
    """Emit the one-line 'sentinel cleared' note to STDERR.

    Per node ``aklr5pq3``: notes go to stderr so they never corrupt an
    artifact piped from stdout (e.g. ``gitbulk show report --state | yq``).
    """
    sub = payload.get("subcommand", "?")
    code = payload.get("exit_code", "?")
    print(
        f"gitbulk show: cleared the ATTENTION sentinel (set by {sub}, exit {code}).",
        file=sys.stderr,
    )


def _emit_dashboard() -> int:
    """Print ~/.cache/gitbulk/dashboard.md if present; otherwise a short hint.

    The dashboard is written by the dashboard module after every run; if
    no run has happened yet it simply hasn't been created. We treat that
    as exit 0 (not an error) because "no runs yet" is the expected first-
    install state, not a structural failure.

    Per node ``aklr5pq3`` trigger 2: viewing the dashboard (which aggregates
    every subcommand's latest-run summary) clears whatever parseable
    sentinel is outstanding. If no dashboard exists yet, nothing was viewed,
    so the sentinel is left intact.
    """
    dash = paths.dashboard_file()
    if dash.exists():
        sys.stdout.write(dash.read_text())
        cleared = sentinel.clear_and_describe()
        if cleared is not None:
            _note_cleared(cleared)
        return EXIT_OK
    print(
        "gitbulk show: no dashboard yet (no runs have completed). "
        "Run a subcommand first (e.g. `gitbulk report`).",
        file=sys.stderr,
    )
    return EXIT_OK


def _emit_for_subcommand(subcommand: str, artifact: str) -> int:
    """Print the requested artifact for ``subcommand``'s latest run."""
    if subcommand not in NAMES:
        print(
            error_line(
                f"gitbulk show: unknown subcommand {subcommand!r}; "
                f"expected one of {', '.join(sorted(NAMES))}"
            ),
            file=sys.stderr,
        )
        return EXIT_STRUCTURAL_FAILURE

    run_dir = _resolve_latest_run_dir(subcommand)
    if run_dir is None:
        print(
            error_line(
                f"gitbulk show: no {subcommand} runs yet "
                f"(missing {paths.latest_run_symlink(subcommand)})."
            ),
            file=sys.stderr,
        )
        return EXIT_STRUCTURAL_FAILURE

    if artifact == "path":
        print(run_dir)
        _clear_if_viewing_flagged_run(subcommand, run_dir)
        return EXIT_OK

    filename = _ARTIFACT_FILES[artifact]
    target = run_dir / filename
    if not target.exists():
        print(
            error_line(
                f"gitbulk show: no {filename} for the latest {subcommand} run "
                f"(checked {target})."
            ),
            file=sys.stderr,
        )
        return EXIT_STRUCTURAL_FAILURE

    sys.stdout.write(target.read_text())
    _clear_if_viewing_flagged_run(subcommand, run_dir)
    return EXIT_OK


def _clear_if_viewing_flagged_run(subcommand: str, run_dir: Path) -> None:
    """Clear the ATTENTION sentinel iff it was raised by the run just shown.

    Per node ``aklr5pq3`` trigger 1. No-op when the runid cannot be
    recovered from the run dir name, or the sentinel was set by a different
    run/subcommand (those cases are left for ``ack``).
    """
    runid = _runid_from_run_dir(run_dir, subcommand)
    if runid is None:
        return
    cleared = sentinel.clear_if_matches(subcommand, runid)
    if cleared is not None:
        _note_cleared(cleared)


def show_handler(args: argparse.Namespace) -> int:
    """Top-level entry for ``gitbulk show``.

    The shared global lock guards us against a concurrent mutating run
    swapping the ``latest-<subcommand>`` symlink mid-read; without it
    we could resolve a path that no longer exists by the time we open
    it. Timeout per node ``tmlk5pq3`` (300s for read-only subcommands).
    """
    try:
        with global_lock(
            "shared",
            timeout=_LOCK_TIMEOUT_SECONDS,
            subcommand="show",
        ):
            sub = getattr(args, "show_subcommand", None)
            if not sub:
                return _emit_dashboard()
            artifact = _selected_artifact(args)
            return _emit_for_subcommand(sub, artifact)
    except LockTimeoutError as e:
        print(
            error_line(f"gitbulk show: timed out acquiring lock: {e}"),
            file=sys.stderr,
        )
        return EXIT_STRUCTURAL_FAILURE


__all__ = [
    "EXIT_OK",
    "EXIT_STRUCTURAL_FAILURE",
    "show_handler",
]
