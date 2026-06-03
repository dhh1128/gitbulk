"""``gitbulk summarize`` — feed a prior report into claude for triage.

Reads the latest ``gitbulk report`` run's ``state.yaml`` and pipes it
to ``claude -p`` against the packaged ``prompts/triage.md`` prompt.
Claude's stdout becomes the run's ``summary.md``; if the output names
any TOP ATTENTION items, the ATTENTION sentinel is set and the
process exits 2.

See this.i node ``smprmpt4n`` (Summarize Prompt Design) for the
prompt-format contract, and ``tmlk5pq3`` for the 300s read-only lock
budget.

Pipeline:

  1. resolve the latest report run (latest-report symlink) and its
     state.yaml; structural failure if either is missing.
  2. acquire the global shared lock (300s timeout).
  3. begin a RunState named "summarize".
  4. read prompt text (packaged default or --prompt PATH override).
  5. read state.yaml as the input piped to claude.
  6. invoke ClaudeClient.run_prompt(...). ClaudeError → structural
     failure with the error recorded in errors.log.
  7. write claude's stdout as summary.md.
  8. parse output for a non-empty ``## TOP ATTENTION`` section; if
     present, set the ATTENTION sentinel and exit 2; otherwise exit 0.
  9. complete RunState (prunes runs/ per retain_runs).

Exit codes (subset of design-notes §8 — summarize never produces 3 or
4 because it doesn't run invariants and has no --skip-check surface):

  0  EXIT_OK                 — claude returned, nothing flagged TOP
  1  EXIT_STRUCTURAL_FAILURE — no prior report, missing state.yaml,
                              lock timeout, or ClaudeError
  2  EXIT_ATTENTION_NEEDED   — TOP ATTENTION section had at least one
                              non-empty line
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from gitbulk import paths, sentinel
from gitbulk.claude import ClaudeError, ProductionClaudeClient
from gitbulk.config.policy import Policy, load_policy
from gitbulk.locks import LockTimeoutError, run_state_lock, sentinel_lock
from gitbulk.runstate import RunState
from gitbulk.util.style import error_line, summary_line

EXIT_OK = 0
EXIT_STRUCTURAL_FAILURE = 1
EXIT_ATTENTION_NEEDED = 2

#: Per node ``tmlk5pq3``: read-only subcommands get a 300s lock budget.
_LOCK_TIMEOUT_SECONDS: float = 300.0

#: Per node ``smprmpt4n``: claude call uses the read-only budget too.
_CLAUDE_TIMEOUT_SECONDS: float = 300.0

#: Heading the triage prompt is contracted to emit. The handler parses
#: claude's output for this exact line (per ``smprmpt4n.a``); changing it
#: requires a coupled update to ``prompts/triage.md`` and this constant.
_TOP_ATTENTION_HEADING = "## TOP ATTENTION"


def _default_prompt_path() -> Path:
    """Path to the packaged ``prompts/triage.md``.

    gitbulk runs from a clone (not a pip install); the prompts directory
    sits at the repo root next to ``src/`` per AGENTS.md "Where things
    live". Walking three parents lands us at the repo root from
    ``src/gitbulk/commands/summarize.py``.
    """
    here = Path(__file__).resolve()
    repo_root = here.parent.parent.parent.parent
    return repo_root / "prompts" / "triage.md"


def _runid_from_run_dir(run_dir: Path) -> str:
    """Strip the trailing ``-summarize`` from the run-dir name."""
    name = run_dir.name
    suffix = "-summarize"
    if name.endswith(suffix):
        return name[: -len(suffix)]
    head, _, _ = name.rpartition("-")
    return head


# Regex used by :func:`_top_attention_items`. Matches the heading line,
# then captures every line until the next ``## `` heading (or end-of-text).
# DOTALL is not needed because we run with re.MULTILINE so '^' and '$'
# match per-line.
_TOP_ATTENTION_RE = re.compile(
    r"^##\s+TOP\s+ATTENTION\s*$(.*?)(?=^##\s|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)

#: Sentinel phrase the prompt emits in the empty case. Treated as
#: "nothing to flag" by the attention-detection heuristic; the
#: comparison is case-insensitive on the leading word "nothing".
_EMPTY_TOP_ATTENTION_MARKERS = ("nothing requires attention",)


def _top_attention_items(claude_output: str) -> list[str]:
    """Return the non-empty bullet lines under ``## TOP ATTENTION``.

    Returns an empty list when the section is missing or when its body
    contains only the prompt's documented "nothing requires attention"
    sentinel. The heuristic is intentionally lenient: if the output
    shape is uncertain (parse fails, ambiguous body), the caller falls
    through to EXIT_OK rather than over-flagging.
    """
    m = _TOP_ATTENTION_RE.search(claude_output)
    if not m:
        return []
    body = m.group(1)
    items: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Body may start with bullets ("- foo") or numbered ("1. foo");
        # both count as items. Plain text lines also count, because the
        # prompt allows the explanation line + URL line under each item.
        # The "nothing requires attention" sentinel is the one exception.
        lowered = line.lower()
        if any(m in lowered for m in _EMPTY_TOP_ATTENTION_MARKERS):
            continue
        items.append(line)
    return items


def summarize_handler(args: argparse.Namespace) -> int:
    """Top-level entry for ``gitbulk summarize``.

    Resource-scoped locking (node ``rsclk7nq``): the report's ``state.yaml`` is
    resolved + read into memory under ``run_state_lock("report", shared)`` so a
    concurrent ``report`` run cannot swap its symlink or gc-prune it mid-read.
    The (slow) Claude call then runs with NO lock held; summarize's own
    ``complete()`` and ``set_attention`` take their resource locks afterwards.
    """
    policy = load_policy()

    try:
        with run_state_lock(
            "report", "shared", timeout=_LOCK_TIMEOUT_SECONDS, subcommand="summarize"
        ):
            latest_report = paths.latest_run_symlink("report")
            if not latest_report.exists():
                print(
                    error_line(
                        "gitbulk summarize: no `gitbulk report` run found. "
                        "Run `gitbulk report` first."
                    ),
                    file=sys.stderr,
                )
                return EXIT_STRUCTURAL_FAILURE

            state_path = latest_report / "state.yaml"
            if not state_path.exists():
                print(
                    error_line(
                        "gitbulk summarize: latest report run has no state.yaml."
                    ),
                    file=sys.stderr,
                )
                return EXIT_STRUCTURAL_FAILURE

            # Read into memory while the report lock is held; everything after
            # this (Claude, our own run-state writes) needs neither this lock
            # nor to keep `report` blocked for the Claude duration.
            state_text = state_path.read_text()

        return _run(args, policy, state_path, state_text)
    except LockTimeoutError as e:
        print(
            error_line(f"gitbulk summarize: timed out acquiring lock: {e}"),
            file=sys.stderr,
        )
        return EXIT_STRUCTURAL_FAILURE


def _run(
    args: argparse.Namespace,
    policy: Policy,
    state_path: Path,
    state_text: str,
) -> int:
    """The summarize pipeline after the report state has been read.

    Split out so lock-timeout vs in-run failures stay structurally distinct
    error branches (same rationale as report.py). ``state_text`` is the
    already-read report ``state.yaml`` content; ``state_path`` is recorded in
    the manifest snapshot for forensic reproducibility."""
    # Build a minimal config snapshot for manifest.yaml. Summarize is
    # parameterized only by the prompt path and model name (no repos,
    # no skip set); recording those lets a forensic reader reproduce
    # the run.
    prompt_path = (
        Path(args.prompt).expanduser()
        if getattr(args, "prompt", None)
        else _default_prompt_path()
    )
    model = getattr(args, "model", None)
    config_snapshot = {
        "prompt_path": str(prompt_path),
        "model": model,
        "input_state_yaml": str(state_path),
    }

    rs = RunState.begin(
        "summarize",
        argv=list(sys.argv),
        config_snapshot=config_snapshot,
    )

    if not prompt_path.exists():
        rs.record_error(f"prompt file not found: {prompt_path}")
        print(
            error_line(f"gitbulk summarize: prompt file not found: {prompt_path}"),
            file=sys.stderr,
        )
        return _finish_failure(
            rs,
            EXIT_STRUCTURAL_FAILURE,
            policy=policy,
            summary=f"prompt file not found: {prompt_path}",
        )

    prompt_text = prompt_path.read_text()
    # state_text was read under run_state_lock("report") in the handler.

    claude = ProductionClaudeClient()
    try:
        output = claude.run_prompt(
            prompt_text,
            input_text=state_text,
            model=model,
            timeout=_CLAUDE_TIMEOUT_SECONDS,
        )
    except ClaudeError as e:
        rs.record_error(f"claude invocation failed: {e}")
        print(
            error_line(f"gitbulk summarize: claude failed: {e}"),
            file=sys.stderr,
        )
        return _finish_failure(
            rs,
            EXIT_STRUCTURAL_FAILURE,
            policy=policy,
            summary=f"claude failed: {e}",
        )

    rs.write_summary(output)

    items = _top_attention_items(output)
    if items:
        exit_code = EXIT_ATTENTION_NEEDED
        runid = _runid_from_run_dir(rs.run_dir)
        with sentinel_lock(timeout=_LOCK_TIMEOUT_SECONDS, subcommand="summarize"):
            sentinel.set_attention(
                exit_code,
                "summarize",
                runid,
                f"triage output flagged {len(items)} attention items",
            )
        with run_state_lock(
            "summarize", "exclusive", timeout=_LOCK_TIMEOUT_SECONDS,
            subcommand="summarize",
        ):
            rs.complete(exit_code, retain_runs=policy.defaults.retain_runs)
        print(summary_line(
            f"gitbulk summarize: {len(items)} attention item(s). "
            f"View: gitbulk show summarize",
            exit_code,
        ))
        return exit_code

    with run_state_lock(
        "summarize", "exclusive", timeout=_LOCK_TIMEOUT_SECONDS,
        subcommand="summarize",
    ):
        rs.complete(EXIT_OK, retain_runs=policy.defaults.retain_runs)
    print(summary_line(
        "gitbulk summarize: nothing requires attention. View: gitbulk show summarize",
        EXIT_OK,
    ))
    return EXIT_OK


def _finish_failure(
    rs: RunState,
    exit_code: int,
    *,
    policy: Policy,
    summary: str,
) -> int:
    """Structural-failure terminal path.

    Writes a fallback summary.md so ``gitbulk show`` (Phase 2+) has
    something to read, then completes the run. Summarize never sets the
    ATTENTION sentinel on a structural failure — sentinel is reserved
    for "PRs need a human" per node ``tmlk5pq3`` / ``snk7p4qm``, not
    for tool-side errors which already surface via cron's MAILTO.

    Mirrors report.py's ``_finish`` shape so every run leaves a
    parseable run dir even on the unhappy paths.
    """
    rs.write_summary(f"# gitbulk summarize (FAILED)\n\n{summary}\n")
    with run_state_lock(
        "summarize", "exclusive", timeout=_LOCK_TIMEOUT_SECONDS,
        subcommand="summarize",
    ):
        rs.complete(exit_code, retain_runs=policy.defaults.retain_runs)
    return exit_code


__all__ = [
    "EXIT_ATTENTION_NEEDED",
    "EXIT_OK",
    "EXIT_STRUCTURAL_FAILURE",
    "summarize_handler",
]
