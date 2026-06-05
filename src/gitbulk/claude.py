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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol, runtime_checkable


@dataclass(frozen=True)
class AgentInvocation:
    """A fully-resolved plan for launching one coding-agent subprocess.

    This is the single value that both invocation paths agree on — the
    blocking ``run_prompt`` (used by ``summarize``) and the parallel
    ``execute_targets`` kernel (used by ``dispatch``). Centralizing argv
    construction here is the seam that lets gitbulk drive agents other than
    Claude (see docs/pluggable-agents.md, this.i ``agbknd7q``).

    Attributes:
        argv: the exact argv list. ``argv[0]`` is the resolved agent binary
            (absolute when resolvable). NEVER passed to a shell — list-form
            only, so attacker-influenceable prompt/worktree text in later
            elements cannot break out (threat-model §5).
        use_stdin: when True, ``stdin_data`` must be written to the child's
            stdin (the prompt and/or ``input_text`` is delivered there rather
            than as an argv element).
        stdin_data: the exact text to feed on stdin, or ``None`` for no stdin.
            Computed by :meth:`plan` so callers (both ``run_prompt`` and the
            parallel kernel) feed one canonical value regardless of whether
            the backend takes its prompt via argv or stdin.
        env: the exact environment for the child, or ``None`` to inherit the
            parent environment (today's behavior). Per-profile scoping is the
            ``env`` allowlist on a profile (this.i ``agenv6q``).
        timeout: effective per-call timeout in seconds.
    """

    argv: list[str]
    use_stdin: bool = False
    stdin_data: str | None = None
    env: dict[str, str] | None = None
    timeout: float | None = None


@runtime_checkable
class ClaudeClient(Protocol):
    """Read/produce-text boundary against the ``claude`` CLI.

    Retained as the historical name; :class:`AgentBackend` is the
    generalized alias (same surface plus :meth:`plan`). New code should
    type against :class:`AgentBackend`.
    """

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


@runtime_checkable
class AgentBackend(Protocol):
    """Generalized coding-agent boundary (Claude, Gemini, Copilot, …).

    Superset of :class:`ClaudeClient`: same blocking ``run_prompt`` plus
    :meth:`plan`, which yields the :class:`AgentInvocation` the parallel
    ``execute_targets`` kernel needs to launch a child itself (it manages
    its own :class:`subprocess.Popen` for SIGTERM→SIGKILL escalation — see
    this.i ``execk7nm``). Both built-in clients implement it. A minimal
    backend exposing only ``run_prompt`` still works with the kernel via a
    legacy argv fallback, but does not satisfy this Protocol.
    """

    def plan(
        self,
        prompt: str,
        *,
        input_text: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        working_directory: Path | None = None,
    ) -> AgentInvocation:
        """Resolve a launch plan without running anything."""
        ...

    def run_prompt(
        self,
        prompt: str,
        *,
        input_text: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        working_directory: Path | None = None,
    ) -> str:
        """Run the agent and return its stdout."""
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

    def plan(
        self,
        prompt: str,
        *,
        input_text: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        working_directory: Path | None = None,
    ) -> AgentInvocation:
        """Build the same argv shape the production client would.

        Reads ``_claude_path`` / ``_default_model`` off ``self`` if a test
        set them (the kernel's argv-shape tests do), else falls back to the
        production defaults so injected fakes and production stay aligned.
        """
        del working_directory  # not reflected in the claude argv
        claude_path = getattr(self, "_claude_path", "claude")
        effective_model = (
            model
            if model is not None
            else getattr(self, "_default_model", "claude-sonnet-4-6")
        )
        return AgentInvocation(
            argv=[
                claude_path,
                "-p",
                prompt,
                "--model",
                effective_model,
                "--dangerously-skip-permissions",
            ],
            use_stdin=input_text is not None,
            stdin_data=input_text,
            env=None,
            timeout=timeout,
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
        ``"claude"``. A bare name is resolved to an absolute path via
        ``shutil.which`` at construction (security-hawk F2 parity with
        :class:`gitbulk.gh.ProductionGHClient`), so a later ``PATH``
        prepend cannot substitute the binary; an unresolvable name falls
        back to itself (see ``__init__`` for the deliberate divergence
        from the gh client).
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
        import shutil

        # Resolve a bare ``claude_path`` to an absolute path via
        # ``shutil.which`` at construction, mirroring the security-hawk F2
        # fix in :class:`gitbulk.gh.ProductionGHClient`: once resolved, a
        # later ``PATH``-prepend cannot substitute the ``claude`` binary
        # out from under a constructed client. An absolute path is trusted
        # as-is (no lookup).
        #
        # Divergence from ProductionGHClient (deliberate): an unresolvable
        # bare name falls back to itself rather than raising. ``gh`` is
        # essential to every subcommand, so an unresolvable ``gh`` is a
        # loud construction failure; ``claude`` is used only by
        # ``dispatch`` / ``summarize``, where a missing binary already
        # surfaces gracefully (exec.py's per-target "failed to launch"
        # path; summarize's ClaudeError handling) instead of aborting the
        # whole run — and a name that doesn't resolve cannot be
        # PATH-hijacked, so the fallback costs no security.
        if Path(claude_path).is_absolute():
            resolved = claude_path
        else:
            found = shutil.which(claude_path)
            resolved = found if found is not None else claude_path
        self._claude_path = resolved
        self._default_model = default_model
        self._default_timeout = default_timeout

    def plan(
        self,
        prompt: str,
        *,
        input_text: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        working_directory: Path | None = None,
    ) -> AgentInvocation:
        """Resolve the ``claude -p`` launch plan (no subprocess spawned)."""
        del working_directory  # not reflected in the claude argv itself
        effective_model = model if model is not None else self._default_model
        effective_timeout = (
            timeout if timeout is not None else self._default_timeout
        )
        # verified non-deprecated against claude CLI 2026-05-28
        return AgentInvocation(
            argv=[
                self._claude_path,
                "-p",
                prompt,
                "--model",
                effective_model,
                "--dangerously-skip-permissions",
            ],
            use_stdin=input_text is not None,
            stdin_data=input_text,
            env=None,
            timeout=effective_timeout,
        )

    def run_prompt(
        self,
        prompt: str,
        *,
        input_text: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        working_directory: Path | None = None,
    ) -> str:
        inv = self.plan(
            prompt,
            input_text=input_text,
            model=model,
            timeout=timeout,
            working_directory=working_directory,
        )
        command: tuple[str, ...] = tuple(inv.argv)
        effective_timeout = inv.timeout
        cwd = str(working_directory) if working_directory is not None else None
        try:
            completed = subprocess.run(
                list(command),
                input=inv.stdin_data,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                check=False,
                cwd=cwd,
                env=inv.env,
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


#: Generalized aliases (this.i ``agbknd7q``). The ``*ClaudeClient`` names are
#: retained for back-compat; new code should prefer the agent-neutral names.
FakeAgentBackend = FakeClaudeClient
ProductionAgentBackend = ProductionClaudeClient

__all__ = [
    "AgentBackend",
    "AgentInvocation",
    "ClaudeClient",
    "ClaudeError",
    "ClaudeTimeoutError",
    "FakeAgentBackend",
    "FakeClaudeClient",
    "ProductionAgentBackend",
    "ProductionClaudeClient",
]
