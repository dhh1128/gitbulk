"""Claude CLI boundary for gitbulk.

The :class:`ClaudeClient` / :class:`AgentBackend` Protocols are the only
legitimate surface between gitbulk code and a coding-agent CLI. This module
holds the agent-neutral *boundary*: the Protocols, the
:class:`AgentInvocation` launch plan, the error types, and
:class:`FakeClaudeClient` (canned responses; used by every test in the
project per AGENTS.md "no network in tests").

The single *production* implementation is :class:`gitbulk.agent.CommandAgentBackend`,
which drives any agent — ``claude`` included (SEC-F1) — from its
:class:`~gitbulk.agent.AgentProfile`. There is no agent-specific production
client here. The two consumers are the Phase 3 ``summarize`` subcommand
(pipes a report's ``state.yaml`` to the agent) and the Phase 4 ``dispatch``
subcommand (runs per-PR prompts inside worktrees); both resolve their backend
through :func:`gitbulk.agent.backend_for`.

Design note (carried over from the gh client, this.i node ``ghclmp7n``): NO
retry policy. A failed agent invocation is almost always a thinking problem
(the prompt or the model is wrong), not a transient infrastructure problem.
Retrying with the same prompt would burn API budget without changing the
outcome. The caller decides whether to re-prompt with a different shape.
"""

from __future__ import annotations

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
        """Build a representative ``claude``-shaped launch plan for tests.

        Reads ``_claude_path`` / ``_default_model`` off ``self`` if a test
        set them (the kernel's argv-shape tests do), else falls back to the
        same defaults as the ``claude`` preset so injected fakes and the real
        :class:`~gitbulk.agent.CommandAgentBackend` stay aligned. (Flag *order*
        here is illustrative — claude is order-insensitive — and need not match
        the preset's exactly.)
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


#: Generalized alias (this.i ``agbknd7q``). The ``FakeClaudeClient`` name is
#: retained for back-compat; new code should prefer the agent-neutral name.
FakeAgentBackend = FakeClaudeClient

__all__ = [
    "AgentBackend",
    "AgentInvocation",
    "ClaudeClient",
    "ClaudeError",
    "ClaudeTimeoutError",
    "FakeAgentBackend",
    "FakeClaudeClient",
]
