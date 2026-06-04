"""Tests for the config-driven agent backends (this.i agprof4k / agtmpl9k).

Per AGENTS.md "no network in tests": every test here either inspects the
deterministic launch plan / config parsing, or mocks ``subprocess.run``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from gitbulk.agent import (
    PRESETS,
    AgentConfigError,
    AgentProfile,
    CommandAgentBackend,
    backend_for,
    parse_agent_profile,
    parse_agents_config,
    resolve_agent_name,
    resolve_profile,
)
from gitbulk.claude import (
    AgentBackend,
    AgentInvocation,
    ClaudeError,
    ClaudeTimeoutError,
    ProductionClaudeClient,
)
from gitbulk.config.policy import Policy, RepoOverride


@pytest.fixture(autouse=True)
def _which_identity(monkeypatch):
    """Resolve every bare binary name to ``/canonical/<name>`` so argv
    assertions are stable regardless of host PATH. Individual tests override."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: f"/canonical/{name}")


def _profile(**kw) -> AgentProfile:
    base = dict(
        name="t",
        command=("tool", "-p", "{prompt}"),
    )
    base.update(kw)
    return AgentProfile(**base)


# ─── presets ────────────────────────────────────────────────────────────────


def test_presets_present():
    assert set(PRESETS) == {"claude", "gemini", "copilot", "cursor"}


def test_command_backend_satisfies_agentbackend_protocol():
    assert isinstance(CommandAgentBackend(_profile()), AgentBackend)


# ─── binary pinning ─────────────────────────────────────────────────────────


def test_pin_bare_name_resolved_via_which():
    b = CommandAgentBackend(_profile(command=("gemini", "-p", "{prompt}")))
    assert b._binary == "/canonical/gemini"


def test_pin_absolute_path_kept(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: pytest.fail("which called"))
    b = CommandAgentBackend(_profile(command=("/opt/bin/tool", "-p", "{prompt}")))
    assert b._binary == "/opt/bin/tool"


def test_pin_bare_name_unresolved_falls_back(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    b = CommandAgentBackend(_profile(command=("nope", "-p", "{prompt}")))
    assert b._binary == "nope"


def test_pin_relative_path_existing_resolved(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "tool").write_text("#!/bin/sh\n")
    b = CommandAgentBackend(_profile(command=("sub/tool", "-p", "{prompt}")))
    assert b._binary == str((tmp_path / "sub" / "tool").resolve())


def test_pin_relative_path_missing_is_config_error(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(AgentConfigError, match="does not exist"):
        CommandAgentBackend(_profile(command=("sub/missing", "-p", "{prompt}")))


# ─── plan(): arg mode ───────────────────────────────────────────────────────


def test_plan_arg_mode_substitutes_prompt_and_model():
    b = CommandAgentBackend(
        _profile(
            command=("tool", "-p", "{prompt}", "--yes"),
            model_args=("-m", "{model}"),
            model="default-model",
        )
    )
    inv = b.plan("HELLO")
    assert isinstance(inv, AgentInvocation)
    assert inv.argv == [
        "/canonical/tool",
        "-p",
        "HELLO",
        "--yes",
        "-m",
        "default-model",
    ]
    assert inv.use_stdin is False
    assert inv.stdin_data is None


def test_plan_model_override_beats_profile_default():
    b = CommandAgentBackend(
        _profile(model_args=("-m", "{model}"), model="default-model")
    )
    inv = b.plan("p", model="override")
    assert "override" in inv.argv
    assert "default-model" not in inv.argv


def test_plan_omits_model_args_when_no_model():
    b = CommandAgentBackend(_profile(model_args=("-m", "{model}"), model=None))
    inv = b.plan("p")
    assert "-m" not in inv.argv  # no dangling model flag


def test_plan_arg_mode_input_text_goes_to_stdin():
    b = CommandAgentBackend(_profile())
    inv = b.plan("p", input_text="REPORT")
    assert inv.use_stdin is True
    assert inv.stdin_data == "REPORT"


# ─── plan(): stdin mode ─────────────────────────────────────────────────────


def test_plan_stdin_mode_puts_prompt_on_stdin():
    b = CommandAgentBackend(
        _profile(command=("tool", "run"), prompt_via="stdin")
    )
    inv = b.plan("THE PROMPT")
    assert inv.argv == ["/canonical/tool", "run"]
    assert inv.use_stdin is True
    assert inv.stdin_data == "THE PROMPT"


def test_plan_stdin_mode_appends_input_text():
    b = CommandAgentBackend(
        _profile(command=("tool", "run"), prompt_via="stdin")
    )
    inv = b.plan("PROMPT", input_text="EXTRA")
    assert inv.stdin_data == "PROMPT\n\nEXTRA"


# ─── plan(): timeout precedence ─────────────────────────────────────────────


def test_plan_timeout_caller_beats_profile_beats_default():
    b = CommandAgentBackend(_profile(timeout=111.0), default_timeout=999.0)
    assert b.plan("p").timeout == 111.0
    assert b.plan("p", timeout=5.0).timeout == 5.0
    b2 = CommandAgentBackend(_profile(timeout=None), default_timeout=999.0)
    assert b2.plan("p").timeout == 999.0


# ─── plan(): env scoping ────────────────────────────────────────────────────


def test_plan_env_none_inherits(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "secret")
    assert CommandAgentBackend(_profile(env=None)).plan("p").env is None


def test_plan_env_allowlist_filters(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "secret")
    monkeypatch.setenv("MYTOOL_KEY", "k")
    monkeypatch.setenv("PATH", "/bin")
    inv = CommandAgentBackend(_profile(env=("MYTOOL_KEY",))).plan("p")
    assert inv.env is not None
    assert inv.env.get("MYTOOL_KEY") == "k"
    assert inv.env.get("PATH") == "/bin"  # base var kept
    assert "GH_TOKEN" not in inv.env  # secret dropped


# ─── run_prompt() ───────────────────────────────────────────────────────────


class _Completed:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_prompt_happy(monkeypatch):
    with patch(
        "gitbulk.agent.subprocess.run", return_value=_Completed(0, stdout="OUT")
    ) as m:
        out = CommandAgentBackend(_profile()).run_prompt("p", input_text="i")
    assert out == "OUT"
    _, kwargs = m.call_args
    assert kwargs["input"] == "i"
    assert kwargs["check"] is False


def test_run_prompt_nonzero_raises():
    with patch(
        "gitbulk.agent.subprocess.run",
        return_value=_Completed(2, stderr="boom"),
    ):
        with pytest.raises(ClaudeError, match="boom"):
            CommandAgentBackend(_profile()).run_prompt("p")


def test_run_prompt_timeout_raises():
    with patch(
        "gitbulk.agent.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["tool"], timeout=1.0),
    ):
        with pytest.raises(ClaudeTimeoutError):
            CommandAgentBackend(_profile()).run_prompt("p")


def test_run_prompt_working_directory_passed(tmp_path):
    with patch(
        "gitbulk.agent.subprocess.run", return_value=_Completed(0)
    ) as m:
        CommandAgentBackend(_profile()).run_prompt("p", working_directory=tmp_path)
    assert m.call_args[1]["cwd"] == str(tmp_path)


# ─── parse_agent_profile ────────────────────────────────────────────────────


def test_parse_preset_override_merges():
    p = parse_agent_profile("gemini", {"model": "gemini-flash"}, "w")
    assert p.name == "gemini"
    assert p.model == "gemini-flash"
    assert p.command == PRESETS["gemini"].command  # inherited


def test_parse_custom_requires_command():
    with pytest.raises(AgentConfigError, match="must define 'command'"):
        parse_agent_profile("mytool", {"model": "x"}, "w")


def test_parse_custom_full():
    p = parse_agent_profile(
        "mytool",
        {
            "command": ["mytool", "run", "{prompt}"],
            "model_args": ["--model", "{model}"],
            "model": "m",
            "prompt_via": "arg",
            "timeout": 60,
            "env": ["MYTOOL_KEY"],
            "sandbox": "fs-only",
        },
        "w",
    )
    assert p.command == ("mytool", "run", "{prompt}")
    assert p.timeout == 60.0
    assert p.env == ("MYTOOL_KEY",)
    assert p.sandbox == "fs-only"


def test_parse_unknown_key_rejected():
    with pytest.raises(AgentConfigError, match="unknown key"):
        parse_agent_profile("gemini", {"bogus": 1}, "w")


def test_parse_command_wrong_element_type_rejected():
    with pytest.raises(AgentConfigError, match="list of strings"):
        parse_agent_profile("x", {"command": ["x", 7, "{prompt}"]}, "w")


def test_parse_empty_command_rejected():
    with pytest.raises(AgentConfigError, match="non-empty"):
        parse_agent_profile("x", {"command": []}, "w")


def test_parse_non_mapping_rejected():
    with pytest.raises(AgentConfigError, match="expected mapping"):
        parse_agent_profile("gemini", ["x"], "w")


def test_parse_prompt_via_invalid():
    with pytest.raises(AgentConfigError, match="prompt_via"):
        parse_agent_profile("gemini", {"prompt_via": "file"}, "w")


def test_parse_timeout_invalid():
    with pytest.raises(AgentConfigError, match="positive number"):
        parse_agent_profile("gemini", {"timeout": 0}, "w")
    with pytest.raises(AgentConfigError, match="positive number"):
        parse_agent_profile("gemini", {"timeout": True}, "w")


def test_parse_model_invalid_type():
    with pytest.raises(AgentConfigError, match="str or null"):
        parse_agent_profile("gemini", {"model": 5}, "w")


def test_parse_model_null_ok():
    assert parse_agent_profile("gemini", {"model": None}, "w").model is None


def test_parse_sandbox_invalid():
    with pytest.raises(AgentConfigError, match="sandbox"):
        parse_agent_profile("gemini", {"sandbox": "vm"}, "w")


def test_parse_arg_mode_requires_one_prompt_token():
    with pytest.raises(AgentConfigError, match="exactly one"):
        parse_agent_profile("x", {"command": ["x", "run"]}, "w")  # zero tokens
    with pytest.raises(AgentConfigError, match="exactly one"):
        parse_agent_profile(
            "x", {"command": ["x", "{prompt}", "{prompt}"]}, "w"
        )  # two


def test_parse_stdin_mode_forbids_prompt_token():
    with pytest.raises(AgentConfigError, match="no '{prompt}'"):
        parse_agent_profile(
            "x", {"command": ["x", "{prompt}"], "prompt_via": "stdin"}, "w"
        )


def test_parse_stdin_mode_ok():
    p = parse_agent_profile(
        "x", {"command": ["x", "run"], "prompt_via": "stdin"}, "w"
    )
    assert p.prompt_via == "stdin"


def test_parse_agents_config_maps_names():
    cfg = parse_agents_config(
        {"gemini": {"model": "g"}, "mine": {"command": ["mine", "{prompt}"]}},
        "agents",
    )
    assert set(cfg) == {"gemini", "mine"}
    assert cfg["gemini"].model == "g"


def test_parse_agents_config_non_mapping():
    with pytest.raises(AgentConfigError, match="expected mapping"):
        parse_agents_config([], "agents")


# ─── resolution ─────────────────────────────────────────────────────────────


def test_resolve_profile_prefers_configured_over_preset():
    custom = _profile(name="claude", command=("claude", "{prompt}"))
    pol = Policy(agents={"claude": custom})
    assert resolve_profile(pol, "claude") is custom


def test_resolve_profile_falls_back_to_preset():
    assert resolve_profile(Policy(), "gemini") is PRESETS["gemini"]


def test_resolve_profile_unknown_raises():
    with pytest.raises(AgentConfigError, match="unknown agent"):
        resolve_profile(Policy(), "nope")


def test_resolve_agent_name_precedence():
    pol = Policy(
        default_agent="gemini",
        repos={"o/r": RepoOverride(agent="copilot")},
    )
    # explicit --agent wins
    assert resolve_agent_name(pol, "cursor", slug="o/r") == "cursor"
    # per-repo beats default
    assert resolve_agent_name(pol, None, slug="o/r") == "copilot"
    # default when no per-repo
    assert resolve_agent_name(pol, None, slug="o/other") == "gemini"
    # claude when nothing set
    assert resolve_agent_name(Policy(), None) == "claude"


def test_resolve_agent_name_no_slug_uses_default():
    assert resolve_agent_name(Policy(default_agent="gemini"), None) == "gemini"


# ─── backend_for ────────────────────────────────────────────────────────────


def test_backend_for_claude_returns_production_client():
    b = backend_for(Policy(), None)
    assert isinstance(b, ProductionClaudeClient)


def test_backend_for_claude_applies_profile_overrides():
    pol = Policy(
        agents={
            "claude": parse_agent_profile(
                "claude", {"model": "opus", "timeout": 42}, "w"
            )
        }
    )
    b = backend_for(pol, None)
    assert isinstance(b, ProductionClaudeClient)
    assert b._default_model == "opus"
    assert b._default_timeout == 42.0


def test_backend_for_claude_with_null_model_omits_default_model():
    pol = Policy(
        agents={
            "claude": parse_agent_profile(
                "claude", {"model": None, "timeout": 30}, "w"
            )
        }
    )
    b = backend_for(pol, None)
    assert isinstance(b, ProductionClaudeClient)
    assert b._default_timeout == 30.0
    # model not overridden → the client's own class default stands.
    assert b._default_model == "claude-sonnet-4-6"


def test_backend_for_non_claude_returns_command_backend():
    b = backend_for(Policy(default_agent="gemini"), None)
    assert isinstance(b, CommandAgentBackend)
    assert b.profile.name == "gemini"


def test_backend_for_per_repo_override():
    pol = Policy(repos={"o/r": RepoOverride(agent="cursor")})
    b = backend_for(pol, None, slug="o/r")
    assert isinstance(b, CommandAgentBackend)
    assert b.profile.name == "cursor"
