"""Config-driven coding-agent backends (this.i ``agprof4k`` / ``agtmpl9k``).

gitbulk drives a CLI coding agent by shelling out with a prompt. Phase 1
(:mod:`gitbulk.claude`, node ``agbknd7q``) unified the invocation behind an
:class:`~gitbulk.claude.AgentBackend` Protocol whose :meth:`plan` yields one
:class:`~gitbulk.claude.AgentInvocation`. This module adds the pluggable layer:

  - :class:`AgentProfile` — a declarative description of how to launch one
    agent (argv template + knobs), parsed from ``gitbulk.yaml``.
  - :data:`PRESETS` — built-in profiles for the common agents, so the common
    case needs only ``default_agent: gemini``.
  - :class:`CommandAgentBackend` — runs any profile.
  - :func:`backend_for` — resolves the effective backend for a run, honoring
    ``--agent`` / per-repo ``agent:`` / ``default_agent`` / the ``claude``
    default.

Security contract (threat-model §5, this.i ``agtmpl9k`` / ``agdang5k``):

  - **No shell, ever.** ``command`` / ``model_args`` are argv *lists*; a scalar
    string in config is a hard error. Prompt/worktree text (attacker-
    influenceable) only ever lands as a single argv element, so it cannot break
    out — there is no shell to break out of.
  - ``{prompt}`` / ``{model}`` substitute as a whole token (or as a substring
    of one token); either way the result is exactly one argv element.
  - The agent binary (``command[0]``) is pinned via :func:`shutil.which` at
    construction, so a later ``PATH`` prepend cannot substitute it (the
    ``gh``/claude F2 fix, generalized).
  - ``env`` is an allowlist: only the named vars (plus a minimal safe base)
    reach the child, so a backend never inherits ``GH_TOKEN`` / SSH agent /
    cloud creds it was not explicitly granted. ``env: None`` (the default)
    inherits the full environment for backward compatibility.

The ``claude`` default is intentionally served by the native
:class:`~gitbulk.claude.ProductionClaudeClient` so the no-config path stays
byte-identical to pre-feature behavior.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

from gitbulk.claude import (
    AgentInvocation,
    ClaudeError,
    ClaudeTimeoutError,
    ProductionClaudeClient,
)
from gitbulk.config.repos import ConfigError
from gitbulk.sandbox import SANDBOX_NONE, bwrap_available, wrap_argv

_log = logging.getLogger(__name__)

#: How to behave when a profile requests a sandbox the host cannot provide.
SANDBOX_FALLBACK_REFUSE = "refuse"
SANDBOX_FALLBACK_WARN_RUN = "warn-run"
VALID_SANDBOX_FALLBACKS = {SANDBOX_FALLBACK_REFUSE, SANDBOX_FALLBACK_WARN_RUN}

#: The placeholders the template engine substitutes.
PROMPT_PLACEHOLDER = "{prompt}"
MODEL_PLACEHOLDER = "{model}"

#: Default per-call timeout when neither the profile nor the caller sets one.
_DEFAULT_TIMEOUT = 1800.0

#: Minimal environment a CLI agent needs to function, regardless of its
#: ``env`` allowlist. Deliberately excludes every credential-bearing var
#: (``*_TOKEN``, ``AWS_*``, ``SSH_AUTH_SOCK``, …): those must be opted in by
#: name. ``PATH`` is included so the agent can find ``git`` etc.
_ENV_BASE_KEYS: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "TMPDIR",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
)

_VALID_PROMPT_VIA = {"arg", "stdin"}
#: Sandbox policies (semantics land in this.i ``agsbx3k``; validated now so a
#: profile can declare its policy before the enforcement phase ships).
_VALID_SANDBOX = {"none", "fs-only", "fs+no-net"}

_PROFILE_KEYS = {
    "command",
    "model_args",
    "model",
    "prompt_via",
    "timeout",
    "env",
    "sandbox",
}


@dataclass(frozen=True)
class AgentProfile:
    """How to launch one coding agent.

    Attributes:
        name: profile name (the key under ``agents:`` / the preset name).
        command: base argv template; must contain exactly one ``{prompt}``
            token when ``prompt_via == "arg"`` and none when ``"stdin"``.
        model_args: argv fragment appended only when a model is in effect
            (e.g. ``("--model", "{model}")``); omitted entirely otherwise.
        model: default model for this agent, or ``None`` if it takes no model.
        prompt_via: ``"arg"`` (prompt is an argv element) or ``"stdin"``.
        timeout: default per-call timeout, or ``None`` to use the caller's.
        env: allowlist of environment-variable names to pass through (on top
            of the minimal safe base), or ``None`` to inherit the full
            environment (backward-compatible default).
        sandbox: ``"none"`` | ``"fs-only"`` | ``"fs+no-net"`` (enforced in a
            later phase; stored and validated now).
    """

    name: str
    command: tuple[str, ...]
    model_args: tuple[str, ...] = ()
    model: str | None = None
    prompt_via: str = "arg"
    timeout: float | None = None
    env: tuple[str, ...] | None = None
    sandbox: str = "none"


#: Built-in profiles. Flag sets are the intended shape; each must be
#: re-verified non-deprecated against the agent CLI at use time (the user's
#: standing rule). The auto-approve flag in each ``command`` is the dangerous,
#: mandatory enabler of unattended runs (this.i ``agdang5k``) and is kept
#: visible here on purpose.
PRESETS: dict[str, AgentProfile] = {
    "claude": AgentProfile(
        name="claude",
        command=("claude", "-p", PROMPT_PLACEHOLDER, "--dangerously-skip-permissions"),
        model_args=("--model", MODEL_PLACEHOLDER),
        model="claude-sonnet-4-6",
    ),
    "gemini": AgentProfile(
        name="gemini",
        command=("gemini", "-p", PROMPT_PLACEHOLDER, "--yolo"),
        model_args=("-m", MODEL_PLACEHOLDER),
        model="gemini-2.5-pro",
    ),
    "copilot": AgentProfile(
        name="copilot",
        command=("copilot", "-p", PROMPT_PLACEHOLDER, "--allow-all-tools"),
        model_args=("--model", MODEL_PLACEHOLDER),
        model=None,
    ),
    "cursor": AgentProfile(
        name="cursor",
        command=("cursor-agent", "-p", PROMPT_PLACEHOLDER, "--force"),
        model_args=("--model", MODEL_PLACEHOLDER),
        model=None,
    ),
}


class AgentConfigError(ConfigError):
    """Raised when an ``agents:`` profile is malformed or unresolvable."""


# ─── Binary pinning ─────────────────────────────────────────────────────────


def _pin_binary(cmd0: str) -> str:
    """Resolve ``command[0]`` to a trusted, stable path.

    - Absolute path → trusted as-is.
    - Bare name (no ``/``) → :func:`shutil.which`, so a later ``PATH`` prepend
      cannot substitute it; falls back to the bare name when unresolved (a
      missing binary then surfaces as a per-target launch failure rather than
      aborting the whole run — mirrors :class:`ProductionClaudeClient`, and an
      absent binary cannot be PATH-hijacked).
    - Relative *path* (contains ``/`` but not absolute) → resolved against cwd;
      a non-existent one is a config error (it cannot be PATH-resolved, so a
      typo would otherwise fail confusingly mid-run).
    """
    if os.path.isabs(cmd0):
        return cmd0
    if "/" in cmd0:
        resolved = Path(cmd0).resolve()
        if not resolved.exists():
            raise AgentConfigError(
                f"agent command path {cmd0!r} does not exist (resolved to "
                f"{resolved})"
            )
        return str(resolved)
    found = shutil.which(cmd0)
    return found if found is not None else cmd0


# ─── Environment scoping ────────────────────────────────────────────────────


def _scoped_env(
    allowlist: tuple[str, ...] | None,
    *,
    source: dict[str, str] | None = None,
) -> dict[str, str] | None:
    """Build the child environment for an ``env`` allowlist.

    ``None`` → ``None`` (inherit the full parent env: backward compatible).
    A tuple (possibly empty) → only the minimal safe base plus the named vars
    that are actually present in ``source`` (default :data:`os.environ`).
    """
    if allowlist is None:
        return None
    src = os.environ if source is None else source
    keep = set(_ENV_BASE_KEYS) | set(allowlist)
    return {k: v for k, v in src.items() if k in keep}


# ─── Backend ────────────────────────────────────────────────────────────────


def _subst(tokens: tuple[str, ...], placeholder: str, value: str) -> list[str]:
    """Replace ``placeholder`` within each token. Each token stays a single
    argv element (no splitting), so embedded prompt/model text cannot inject
    extra arguments."""
    return [t.replace(placeholder, value) for t in tokens]


class CommandAgentBackend:
    """An :class:`~gitbulk.claude.AgentBackend` driven by an :class:`AgentProfile`."""

    def __init__(
        self,
        profile: AgentProfile,
        *,
        default_timeout: float = _DEFAULT_TIMEOUT,
        sandbox_fallback: str = SANDBOX_FALLBACK_REFUSE,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self.profile = profile
        self._binary = _pin_binary(profile.command[0])
        self._default_timeout = default_timeout
        # Scoped-token injection seam (this.i agtok2n): a caller (e.g. a future
        # per-repo short-lived-token minter) supplies env vars that always reach
        # the child, on top of the ``env`` allowlist. None ⇒ no extra vars.
        self._extra_env = extra_env
        # Resolve the effective sandbox now (this.i agsbx3k). If the profile
        # asks for a sandbox the host can't provide, REFUSE by default rather
        # than silently downgrade (a silent downgrade defeats the purpose);
        # ``warn-run`` opts into running unsandboxed with a loud warning.
        sb = profile.sandbox
        if sb != SANDBOX_NONE and not bwrap_available():
            if sandbox_fallback == SANDBOX_FALLBACK_WARN_RUN:
                _log.warning(
                    "agent %r requests sandbox %r but bubblewrap is "
                    "unavailable; running UNSANDBOXED (sandbox_fallback="
                    "warn-run)",
                    profile.name,
                    sb,
                )
                sb = SANDBOX_NONE
            else:
                raise AgentConfigError(
                    f"agent {profile.name!r} requires sandbox {profile.sandbox!r} "
                    f"but bubblewrap is unavailable on this host. Install bwrap "
                    f"and enable unprivileged user namespaces, or set "
                    f"'sandbox_fallback: warn-run' to run unsandboxed."
                )
        self._sandbox = sb

    def plan(
        self,
        prompt: str,
        *,
        input_text: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        working_directory: Path | None = None,
    ) -> AgentInvocation:
        profile = self.profile
        effective_model = model if model is not None else profile.model

        rest = tuple(profile.command[1:])
        if profile.prompt_via == "stdin":
            argv_tail = list(rest)
            stdin_data = prompt
            if input_text is not None:
                stdin_data = f"{prompt}\n\n{input_text}"
            use_stdin = True
        else:  # "arg"
            argv_tail = _subst(rest, PROMPT_PLACEHOLDER, prompt)
            stdin_data = input_text
            use_stdin = input_text is not None

        if effective_model is not None and profile.model_args:
            argv_tail += _subst(profile.model_args, MODEL_PLACEHOLDER, effective_model)

        if timeout is not None:
            effective_timeout = timeout
        elif profile.timeout is not None:
            effective_timeout = profile.timeout
        else:
            effective_timeout = self._default_timeout

        env = _scoped_env(profile.env)
        if self._extra_env:
            # Injected (e.g. scoped-token) vars always reach the child, on top
            # of the allowlist or — if env is inherited — the full environment.
            base = env if env is not None else dict(os.environ)
            env = {**base, **self._extra_env}

        argv = [self._binary, *argv_tail]
        # Defense-in-depth: wrap in a bwrap sandbox when the profile asks for
        # one and we have a worktree to bind (this.i agsbx3k). cwd is set inside
        # the sandbox by wrap_argv, so the bare argv carries no path assumptions.
        if self._sandbox != SANDBOX_NONE and working_directory is not None:
            argv = wrap_argv(
                argv, worktree=working_directory, policy=self._sandbox
            )

        return AgentInvocation(
            argv=argv,
            use_stdin=use_stdin,
            stdin_data=stdin_data,
            env=env,
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
        command = tuple(inv.argv)
        cwd = str(working_directory) if working_directory is not None else None
        try:
            completed = subprocess.run(
                list(command),
                input=inv.stdin_data,
                capture_output=True,
                text=True,
                timeout=inv.timeout,
                check=False,
                cwd=cwd,
                env=inv.env,
            )
        except subprocess.TimeoutExpired as exc:
            raise ClaudeTimeoutError(
                f"{self.profile.name} timed out after {inv.timeout}s: {exc}",
                command=command,
            )
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            raise ClaudeError(
                f"{self.profile.name} failed (exit {completed.returncode}): {stderr}",
                command=command,
            )
        return completed.stdout


# ─── Config parsing ─────────────────────────────────────────────────────────


def _ensure_str_list_strict(value, where: str) -> tuple[str, ...]:
    """Like the policy str-list check, but a *scalar string is rejected*.

    Critical: a YAML scalar for ``command`` would be a shell-string foot-gun,
    so we refuse it loudly rather than silently wrapping it (this.i
    ``agtmpl9k``)."""
    if isinstance(value, str):
        raise AgentConfigError(
            f"{where}: must be a list of argv tokens, not a single string "
            f"(a string would imply a shell, which gitbulk never uses)"
        )
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise AgentConfigError(f"{where}: expected a list of strings")
    return tuple(value)


def _count_prompt_tokens(tokens: tuple[str, ...]) -> int:
    return sum(1 for t in tokens if PROMPT_PLACEHOLDER in t)


def parse_agent_profile(
    name: str, raw: dict, where: str
) -> AgentProfile:
    """Parse and validate one ``agents.<name>`` block into an :class:`AgentProfile`.

    A block for a known preset *overrides* that preset's fields; a block for an
    unknown name must define ``command``. Validation enforces the security
    contract (argv-list-only, exactly one ``{prompt}`` for arg mode).
    """
    if not isinstance(raw, dict):
        raise AgentConfigError(f"{where}: expected mapping, got {type(raw).__name__}")
    extra = set(raw.keys()) - _PROFILE_KEYS
    if extra:
        raise AgentConfigError(
            f"{where}: unknown key(s) {sorted(extra)!r}; "
            f"allowed: {sorted(_PROFILE_KEYS)!r}"
        )

    base = PRESETS.get(name)
    if base is None and "command" not in raw:
        raise AgentConfigError(
            f"{where}: custom agent {name!r} must define 'command' "
            f"(no built-in preset by that name)"
        )

    profile = base if base is not None else AgentProfile(name=name, command=())
    profile = replace(profile, name=name)

    if "command" in raw:
        profile = replace(
            profile, command=_ensure_str_list_strict(raw["command"], f"{where}.command")
        )
    if "model_args" in raw:
        profile = replace(
            profile,
            model_args=_ensure_str_list_strict(
                raw["model_args"], f"{where}.model_args"
            ),
        )
    if "model" in raw:
        m = raw["model"]
        if m is not None and not isinstance(m, str):
            raise AgentConfigError(f"{where}.model: expected str or null")
        profile = replace(profile, model=m)
    if "prompt_via" in raw:
        pv = raw["prompt_via"]
        if pv not in _VALID_PROMPT_VIA:
            raise AgentConfigError(
                f"{where}.prompt_via: {pv!r} not in {sorted(_VALID_PROMPT_VIA)!r}"
            )
        profile = replace(profile, prompt_via=pv)
    if "timeout" in raw:
        t = raw["timeout"]
        if isinstance(t, bool) or not isinstance(t, (int, float)) or t <= 0:
            raise AgentConfigError(f"{where}.timeout: expected a positive number")
        profile = replace(profile, timeout=float(t))
    if "env" in raw:
        profile = replace(
            profile, env=_ensure_str_list_strict(raw["env"], f"{where}.env")
        )
    if "sandbox" in raw:
        sb = raw["sandbox"]
        if sb not in _VALID_SANDBOX:
            raise AgentConfigError(
                f"{where}.sandbox: {sb!r} not in {sorted(_VALID_SANDBOX)!r}"
            )
        profile = replace(profile, sandbox=sb)

    _validate_profile_template(profile, where)
    return profile


def _validate_profile_template(profile: AgentProfile, where: str) -> None:
    if not profile.command:
        raise AgentConfigError(f"{where}: 'command' must be non-empty")
    n_prompt = _count_prompt_tokens(profile.command)
    if profile.prompt_via == "arg" and n_prompt != 1:
        raise AgentConfigError(
            f"{where}: command must contain exactly one '{PROMPT_PLACEHOLDER}' "
            f"token when prompt_via='arg' (found {n_prompt})"
        )
    if profile.prompt_via == "stdin" and n_prompt != 0:
        raise AgentConfigError(
            f"{where}: command must contain no '{PROMPT_PLACEHOLDER}' token "
            f"when prompt_via='stdin' (found {n_prompt})"
        )


def parse_agents_config(raw, where: str) -> dict[str, AgentProfile]:
    """Parse the top-level ``agents:`` mapping into name → :class:`AgentProfile`."""
    if not isinstance(raw, dict):
        raise AgentConfigError(f"{where}: expected mapping, got {type(raw).__name__}")
    out: dict[str, AgentProfile] = {}
    for name, block in raw.items():
        out[name] = parse_agent_profile(name, block, f"{where}.{name}")
    return out


# ─── Resolution ─────────────────────────────────────────────────────────────


def resolve_profile(policy, name: str) -> AgentProfile:
    """Return the effective :class:`AgentProfile` for ``name``.

    A user ``agents.<name>`` block (already merged over its preset at parse
    time) wins; otherwise the built-in preset; otherwise an error.
    """
    agents = getattr(policy, "agents", {}) or {}
    if name in agents:
        return agents[name]
    if name in PRESETS:
        return PRESETS[name]
    raise AgentConfigError(
        f"unknown agent {name!r}: not defined under 'agents:' and not a "
        f"built-in preset ({sorted(PRESETS)!r})"
    )


def resolve_agent_name(policy, requested: str | None, *, slug: str | None = None) -> str:
    """Resolve which agent name applies: ``--agent`` → per-repo ``agent:`` →
    ``default_agent`` → ``claude``."""
    if requested:
        return requested
    if slug is not None:
        override = (getattr(policy, "repos", {}) or {}).get(slug)
        repo_agent = getattr(override, "agent", None) if override is not None else None
        if repo_agent:
            return repo_agent
    return getattr(policy, "default_agent", None) or "claude"


def backend_for(
    policy,
    requested: str | None = None,
    *,
    slug: str | None = None,
    default_timeout: float = _DEFAULT_TIMEOUT,
    token_env: dict[str, str] | None = None,
):
    """Build the effective :class:`~gitbulk.claude.AgentBackend` for a run/target.

    The ``claude`` default is served by the native
    :class:`ProductionClaudeClient` so the no-config path is byte-identical to
    pre-feature behavior; every other agent uses :class:`CommandAgentBackend`,
    which applies the profile's sandbox (subject to ``policy.sandbox_fallback``)
    and any injected ``token_env`` (scoped-token seam, this.i agtok2n).

    Raises :class:`AgentConfigError` if the profile requires a sandbox the host
    cannot provide and the fallback policy is ``refuse`` (the default).
    """
    name = resolve_agent_name(policy, requested, slug=slug)
    profile = resolve_profile(policy, name)
    if name == "claude":
        # The trusted native path: no generic sandbox/token plumbing.
        kwargs: dict = {}
        if profile.model is not None:
            kwargs["default_model"] = profile.model
        if profile.timeout is not None:
            kwargs["default_timeout"] = profile.timeout
        return ProductionClaudeClient(**kwargs)
    fallback = getattr(policy, "sandbox_fallback", None) or SANDBOX_FALLBACK_REFUSE
    return CommandAgentBackend(
        profile,
        default_timeout=default_timeout,
        sandbox_fallback=fallback,
        extra_env=token_env,
    )


__all__ = [
    "AgentConfigError",
    "AgentProfile",
    "CommandAgentBackend",
    "PRESETS",
    "backend_for",
    "parse_agent_profile",
    "parse_agents_config",
    "resolve_agent_name",
    "resolve_profile",
]
