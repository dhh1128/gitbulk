"""Tests for the agent boundary (Protocol + Fake).

Per AGENTS.md "no network in tests", every test in this file uses
:class:`FakeClaudeClient`; no actual agent CLI is ever invoked. The single
production backend (:class:`gitbulk.agent.CommandAgentBackend`, used for
``claude`` and every other agent) is exercised in ``test_agent.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gitbulk.claude import (
    AgentBackend,
    AgentInvocation,
    ClaudeClient,
    ClaudeError,
    FakeAgentBackend,
    FakeClaudeClient,
)


# ─── Protocol satisfaction ─────────────────────────────────────────────────


def test_fake_satisfies_claudeclient_protocol():
    fake = FakeClaudeClient()
    assert isinstance(fake, ClaudeClient)


def test_fake_satisfies_agentbackend_protocol():
    assert isinstance(FakeClaudeClient(), AgentBackend)


def test_agent_backend_alias_points_at_fake_client():
    assert FakeAgentBackend is FakeClaudeClient


# ─── FakeClaudeClient.plan (kernel argv-shape seam) ────────────────────────


def test_fake_plan_builds_claude_argv_and_reads_overrides():
    fake = FakeClaudeClient({"": "x"})
    fake._claude_path = "/usr/local/bin/claude"  # type: ignore[attr-defined]
    fake._default_model = "fake-model"  # type: ignore[attr-defined]
    inv = fake.plan("PROMPT")
    assert isinstance(inv, AgentInvocation)
    assert inv.argv == [
        "/usr/local/bin/claude",
        "-p",
        "PROMPT",
        "--model",
        "fake-model",
        "--dangerously-skip-permissions",
    ]


def test_fake_plan_defaults_when_attrs_unset():
    inv = FakeClaudeClient({"": "x"}).plan("p")
    assert inv.argv[0] == "claude"
    assert inv.argv[4] == "claude-sonnet-4-6"
    assert inv.env is None


def test_fake_plan_use_stdin_when_input_text():
    inv = FakeClaudeClient({"": "x"}).plan("p", input_text="state", timeout=12.0)
    assert inv.use_stdin is True
    assert inv.stdin_data == "state"
    assert inv.timeout == 12.0


# ─── FakeClaudeClient.run_prompt ───────────────────────────────────────────


def test_fake_unconfigured_raises_claudeerror():
    fake = FakeClaudeClient()
    with pytest.raises(ClaudeError, match="no responses configured"):
        fake.run_prompt("anything")


def test_fake_dict_prefix_match_returns_canned_output():
    fake = FakeClaudeClient({"triage:": "TOP ATTENTION\n- a thing"})
    out = fake.run_prompt("triage: please")
    assert out == "TOP ATTENTION\n- a thing"


def test_fake_dict_longest_prefix_wins():
    fake = FakeClaudeClient(
        {
            "tri": "short",
            "triage": "long",
        }
    )
    assert fake.run_prompt("triage: go") == "long"
    assert fake.run_prompt("tribe foo") == "short"


def test_fake_dict_no_match_raises():
    fake = FakeClaudeClient({"foo": "bar"})
    with pytest.raises(ClaudeError, match="no prefix match"):
        fake.run_prompt("zzz unrelated")


def test_fake_callable_receives_prompt_and_input():
    seen: dict[str, Any] = {}

    def respond(prompt: str, input_text: str | None) -> str:
        seen["prompt"] = prompt
        seen["input"] = input_text
        return "hi"

    fake = FakeClaudeClient(respond)
    out = fake.run_prompt("hello", input_text="world")
    assert out == "hi"
    assert seen == {"prompt": "hello", "input": "world"}


def test_fake_tracks_call_count_and_last_call():
    fake = FakeClaudeClient({"": "ok"})
    fake.run_prompt("p1", input_text="i1", model="m", timeout=12.5)
    fake.run_prompt("p2", working_directory=Path("/tmp"))
    assert fake.call_count == 2
    assert fake.last_call == {
        "prompt": "p2",
        "input_text": None,
        "model": None,
        "timeout": None,
        "working_directory": Path("/tmp"),
    }
