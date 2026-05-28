"""Claude CLI boundary for gitbulk.

The :class:`ClaudeClient` Protocol is the only legitimate surface
between gitbulk code and the ``claude`` CLI. Two implementations live
here:

  - :class:`ProductionClaudeClient` subprocesses to ``claude -p`` for
    real.
  - :class:`FakeClaudeClient` returns canned responses; used by every
    test in the project per AGENTS.md "no network in tests."

This module mirrors the shape of :mod:`gitbulk.gh` (see this.i node
``ghclmp7n``): Protocol + Fake + Production. The two consumers in this
codebase are the Phase 3 ``summarize`` subcommand (pipes a report's
``state.yaml`` to ``claude -p``) and the Phase 4 ``dispatch``
subcommand (runs per-PR prompts inside worktrees).

Design departure from :class:`ProductionGHClient`: NO retry policy.
A failed claude invocation is almost always a thinking problem (the
prompt or the model is wrong), not a transient infrastructure
problem. Retrying with the same prompt would burn API budget without
changing the outcome. The caller decides whether to re-prompt with a
different shape.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Mapping, Protocol, runtime_checkable


@runtime_checkable
class ClaudeClient(Protocol):
    """Read/produce-text boundary against the ``claude`` CLI."""

    def run_prompt(
        self,
        prompt: str,
        *,
        input_text: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        working_directory: Path | None = None,
    ) -> str:
        """Run ``claude -p`` with ``prompt`` and return its stdout.

        ``input_text``, if given, is piped to claude's stdin (the
        idiomatic shape for "summarize this report"-style calls).
        ``model`` and ``timeout`` override the client's defaults.
        ``working_directory``, if given, is the cwd for the subprocess
        — used by ``dispatch`` to run a prompt inside a worktree.

        Raises :class:`ClaudeError` on non-zero exit;
        :class:`ClaudeTimeoutError` (a subclass of both
        :class:`ClaudeError` and :class:`TimeoutError`) on timeout.
        """
        ...


class ClaudeError(RuntimeError):
    """Raised when a claude invocation fails.

    Carries the original ``command`` tuple for diagnostic logging.
    No retry is attempted — see the module docstring for rationale.
    """

    def __init__(
        self, message: str, *, command: tuple[str, ...] | None = None
    ) -> None:
        super().__init__(message)
        self.command = command


class ClaudeTimeoutError(ClaudeError, TimeoutError):
    """Raised when claude exceeds the per-call timeout."""


# ─── FakeClaudeClient ───────────────────────────────────────────────────────


class FakeClaudeClient:
    """In-memory :class:`ClaudeClient` for tests.

    Configure with either:

      - A ``Mapping[str, str]`` keyed on a prompt-prefix → canned output.
        The first key that is a prefix of the actual prompt wins; ties
        are broken by longest prefix first (so a more specific stub
        beats a generic one).
      - A ``Callable[[str, str | None], str]`` that receives ``(prompt,
        input_text)`` and returns the output.
      - ``None`` (the default) — every call raises :class:`ClaudeError`
        so tests fail loudly when they exercise an unconfigured path.

    Tracks ``call_count`` (int) and ``last_call`` (a dict with the
    prompt/input/model/timeout/working_directory that the last call
    actually used). Both are public attributes; tests assert on them.
    """

    def __init__(
        self,
        responses: Mapping[str, str]
        | Callable[[str, str | None], str]
        | None = None,
    ) -> None:
        self._responses = responses
        self.call_count: int = 0
        self.last_call: dict | None = None

    def run_prompt(
        self,
        prompt: str,
        *,
        input_text: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        working_directory: Path | None = None,
    ) -> str:
        self.call_count += 1
        self.last_call = {
            "prompt": prompt,
            "input_text": input_text,
            "model": model,
            "timeout": timeout,
            "working_directory": working_directory,
        }
        responses = self._responses
        if responses is None:
            raise ClaudeError("FakeClaudeClient: no responses configured")
        if callable(responses):
            return responses(prompt, input_text)
        # Mapping case: prefix match, longest-first.
        for key in sorted(responses.keys(), key=len, reverse=True):
            if prompt.startswith(key):
                return responses[key]
        raise ClaudeError(
            f"FakeClaudeClient: no prefix match for prompt "
            f"(first 60 chars): {prompt[:60]!r}"
        )


# ─── ProductionClaudeClient ─────────────────────────────────────────────────


class ProductionClaudeClient:
    """Real :class:`ClaudeClient` that subprocesses to ``claude -p``.

    Default model = ``claude-sonnet-4-6`` (matches multiprompt.py's
    default per ``../origin-platform/scripts/multiprompt-spec.md`` —
    the same model expectation gitbulk's Phase 4 dispatch will share).
    Default timeout 300s, generous enough for a typical triage prompt
    + the model's first-token latency.

    Constructor knobs (all keyword-only):

      - ``claude_path``: path to the ``claude`` executable; default
        ``"claude"`` (picked up from PATH).
      - ``default_model``: model alias or full name passed via
        ``--model`` when the caller doesn't override.
      - ``default_timeout``: per-call timeout seconds when caller
        passes ``timeout=None``.

    All invocations include ``--dangerously-skip-permissions`` because
    gitbulk runs unattended from cron; an interactive permission prompt
    would deadlock the cron job. The user has already consented by
    configuring gitbulk to run claude in the first place.

    Flag verification: ``claude -p ... --model <m>
    --dangerously-skip-permissions`` was checked for deprecation
    warnings on 2026-05-28; see the comment at the call site.
    """

    def __init__(
        self,
        *,
        claude_path: str = "claude",
        default_model: str = "claude-sonnet-4-6",
        default_timeout: float = 300.0,
    ) -> None:
        self._claude_path = claude_path
        self._default_model = default_model
        self._default_timeout = default_timeout

    def run_prompt(
        self,
        prompt: str,
        *,
        input_text: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        working_directory: Path | None = None,
    ) -> str:
        effective_model = model if model is not None else self._default_model
        effective_timeout = (
            timeout if timeout is not None else self._default_timeout
        )
        # verified non-deprecated against claude CLI 2026-05-28
        command: tuple[str, ...] = (
            self._claude_path,
            "-p",
            prompt,
            "--model",
            effective_model,
            "--dangerously-skip-permissions",
        )
        cwd = str(working_directory) if working_directory is not None else None
        try:
            completed = subprocess.run(
                list(command),
                input=input_text,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                check=False,
                cwd=cwd,
            )
        except subprocess.TimeoutExpired as exc:
            raise ClaudeTimeoutError(
                f"claude timed out after {effective_timeout}s: {exc}",
                command=command,
            )
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            raise ClaudeError(
                f"claude failed (exit {completed.returncode}): {stderr}",
                command=command,
            )
        return completed.stdout


__all__ = [
    "ClaudeClient",
    "ClaudeError",
    "ClaudeTimeoutError",
    "FakeClaudeClient",
    "ProductionClaudeClient",
]
