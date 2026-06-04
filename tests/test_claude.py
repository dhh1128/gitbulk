"""Tests for the claude client surface (Protocol + Fake + Production).

Per AGENTS.md "no network in tests", every test in this file either
uses :class:`FakeClaudeClient` or mocks :func:`subprocess.run` so no
actual ``claude`` invocation ever happens.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gitbulk.claude import (
    AgentBackend,
    AgentInvocation,
    ClaudeClient,
    ClaudeError,
    ClaudeTimeoutError,
    FakeAgentBackend,
    FakeClaudeClient,
    ProductionAgentBackend,
    ProductionClaudeClient,
)


@pytest.fixture(autouse=True)
def _mock_shutil_which(monkeypatch):
    """Make ``shutil.which`` resolve every name to itself.

    ProductionClaudeClient resolves a bare ``claude_path`` through
    ``shutil.which`` at construction (security-hawk F2 parity with
    ProductionGHClient). For unit tests we don't want the host's
    ``claude`` presence (or absence, e.g. on CI) to leak into
    ``_claude_path`` / argv assertions, so we stub the resolver to be the
    identity function. The dedicated resolution tests below override this
    fixture per test as needed.
    """
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: name)


# ─── Protocol satisfaction ─────────────────────────────────────────────────


def test_fake_satisfies_claudeclient_protocol():
    fake = FakeClaudeClient()
    assert isinstance(fake, ClaudeClient)


def test_production_satisfies_claudeclient_protocol():
    prod = ProductionClaudeClient()
    assert isinstance(prod, ClaudeClient)


# ─── AgentBackend generalization (this.i agbknd7q) ─────────────────────────


def test_both_clients_satisfy_agentbackend_protocol():
    assert isinstance(FakeClaudeClient(), AgentBackend)
    assert isinstance(ProductionClaudeClient(), AgentBackend)


def test_agent_backend_aliases_point_at_claude_clients():
    assert FakeAgentBackend is FakeClaudeClient
    assert ProductionAgentBackend is ProductionClaudeClient


def test_production_plan_builds_claude_argv():
    inv = ProductionClaudeClient().plan("do a thing")
    assert isinstance(inv, AgentInvocation)
    assert inv.argv == [
        "claude",
        "-p",
        "do a thing",
        "--model",
        "claude-sonnet-4-6",
        "--dangerously-skip-permissions",
    ]
    assert inv.use_stdin is False
    assert inv.env is None
    assert inv.timeout == 300.0


def test_production_plan_use_stdin_and_model_override():
    inv = ProductionClaudeClient().plan(
        "p", input_text="state", model="opus", timeout=12.0
    )
    assert inv.use_stdin is True
    assert "opus" in inv.argv
    assert inv.timeout == 12.0


def test_fake_plan_mirrors_production_argv_and_reads_overrides():
    fake = FakeClaudeClient({"": "x"})
    fake._claude_path = "/usr/local/bin/claude"  # type: ignore[attr-defined]
    fake._default_model = "fake-model"  # type: ignore[attr-defined]
    inv = fake.plan("PROMPT")
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


# ─── FakeClaudeClient ──────────────────────────────────────────────────────


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


# ─── ProductionClaudeClient: argv shape + happy path ───────────────────────


class _CompletedFake:
    """Stand-in for :class:`subprocess.CompletedProcess` returned by mocks."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_production_default_constructor():
    p = ProductionClaudeClient()
    assert p._claude_path == "claude"
    assert p._default_model == "claude-sonnet-4-6"
    assert p._default_timeout == 300.0


def test_production_constructor_overrides():
    p = ProductionClaudeClient(
        claude_path="/usr/local/bin/claude",
        default_model="opus",
        default_timeout=60.0,
    )
    assert p._claude_path == "/usr/local/bin/claude"
    assert p._default_model == "opus"
    assert p._default_timeout == 60.0


# ─── ProductionClaudeClient: claude_path resolution (F2 parity) ────────────


def test_constructor_resolves_bare_name_via_shutil_which(monkeypatch):
    """A bare claude_path is resolved to an absolute path via
    shutil.which at construction, so a later PATH-prepend cannot
    substitute the binary (mirrors the gh F2 fix)."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: f"/canonical/{name}")
    p = ProductionClaudeClient()
    assert p._claude_path == "/canonical/claude"


def test_constructor_absolute_path_skips_which_lookup(monkeypatch):
    """Absolute paths are taken as-is — shutil.which is not consulted."""
    import shutil

    called = []
    monkeypatch.setattr(
        shutil, "which", lambda name: called.append(name) or "/should/not/use"
    )
    p = ProductionClaudeClient(claude_path="/explicit/claude")
    assert p._claude_path == "/explicit/claude"
    assert called == []


def test_constructor_unresolvable_name_falls_back_to_bare(monkeypatch):
    """Divergence from ProductionGHClient: an unresolvable bare name
    falls back to itself (no raise). dispatch/summarize degrade
    gracefully on a missing claude, and an absent binary can't be
    PATH-hijacked, so the fallback costs no security."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    p = ProductionClaudeClient()
    assert p._claude_path == "claude"


def test_production_happy_path_argv_and_stdout():
    completed = _CompletedFake(0, stdout="triage output\n")
    with patch(
        "gitbulk.claude.subprocess.run", return_value=completed
    ) as mock_run:
        out = ProductionClaudeClient().run_prompt(
            "do a triage", input_text="state yaml here"
        )
    assert out == "triage output\n"
    args, kwargs = mock_run.call_args
    argv = args[0]
    assert argv[0] == "claude"
    assert "-p" in argv
    assert "do a triage" in argv
    assert "--model" in argv
    assert "claude-sonnet-4-6" in argv
    assert "--dangerously-skip-permissions" in argv
    assert kwargs["input"] == "state yaml here"
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 300.0
    assert kwargs["check"] is False
    assert kwargs["cwd"] is None


def test_production_respects_per_call_model_and_timeout():
    completed = _CompletedFake(0, stdout="ok")
    with patch(
        "gitbulk.claude.subprocess.run", return_value=completed
    ) as mock_run:
        ProductionClaudeClient().run_prompt(
            "x", model="opus", timeout=7.5
        )
    _, kwargs = mock_run.call_args
    argv = mock_run.call_args[0][0]
    assert "opus" in argv
    assert kwargs["timeout"] == 7.5


def test_production_working_directory_passed_as_cwd(tmp_path):
    completed = _CompletedFake(0, stdout="ok")
    with patch(
        "gitbulk.claude.subprocess.run", return_value=completed
    ) as mock_run:
        ProductionClaudeClient().run_prompt(
            "x", working_directory=tmp_path
        )
    _, kwargs = mock_run.call_args
    assert kwargs["cwd"] == str(tmp_path)


def test_production_uses_custom_claude_path():
    completed = _CompletedFake(0, stdout="ok")
    with patch(
        "gitbulk.claude.subprocess.run", return_value=completed
    ) as mock_run:
        ProductionClaudeClient(claude_path="/opt/bin/claude").run_prompt("x")
    argv = mock_run.call_args[0][0]
    assert argv[0] == "/opt/bin/claude"


# ─── ProductionClaudeClient: failure modes ─────────────────────────────────


def test_production_timeout_raises_claudetimeouterror():
    with patch(
        "gitbulk.claude.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=1.0),
    ):
        with pytest.raises(ClaudeTimeoutError) as exc:
            ProductionClaudeClient(default_timeout=1.0).run_prompt("x")
    # ClaudeTimeoutError IS a ClaudeError and a TimeoutError
    assert isinstance(exc.value, ClaudeError)
    assert isinstance(exc.value, TimeoutError)
    assert exc.value.command is not None
    assert exc.value.command[0] == "claude"
    assert "1.0s" in str(exc.value)


def test_production_nonzero_exit_raises_claudeerror_with_stderr():
    with patch(
        "gitbulk.claude.subprocess.run",
        return_value=_CompletedFake(2, stdout="", stderr="model not found"),
    ):
        with pytest.raises(ClaudeError) as exc:
            ProductionClaudeClient().run_prompt("x")
    assert "exit 2" in str(exc.value)
    assert "model not found" in str(exc.value)
    assert exc.value.command is not None
    # not a timeout error
    assert not isinstance(exc.value, ClaudeTimeoutError)


def test_production_nonzero_exit_with_empty_stderr_still_raises():
    with patch(
        "gitbulk.claude.subprocess.run",
        return_value=_CompletedFake(3, stdout="", stderr=""),
    ):
        with pytest.raises(ClaudeError) as exc:
            ProductionClaudeClient().run_prompt("x")
    assert "exit 3" in str(exc.value)


def test_production_no_input_text_passes_none_to_subprocess():
    completed = _CompletedFake(0, stdout="ok")
    with patch(
        "gitbulk.claude.subprocess.run", return_value=completed
    ) as mock_run:
        ProductionClaudeClient().run_prompt("x")
    _, kwargs = mock_run.call_args
    assert kwargs["input"] is None
