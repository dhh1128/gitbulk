"""End-to-end tests for ``gitbulk summarize`` (this.i node ``smprmpt4n``).

The summarize handler pipes a prior report's state.yaml to claude -p
against the packaged triage prompt and saves the response as summary.md.
Per AGENTS.md every test uses :class:`FakeClaudeClient`; no actual
``claude`` invocation ever happens.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
import yaml

from gitbulk import paths, sentinel
from gitbulk.claude import ClaudeError, FakeClaudeClient
from gitbulk.cli import main
from gitbulk.commands import summarize as summarize_mod
from gitbulk.commands.summarize import (
    EXIT_ATTENTION_NEEDED,
    EXIT_OK,
    EXIT_STRUCTURAL_FAILURE,
    _default_prompt_path,
    _runid_from_run_dir,
    _top_attention_items,
    summarize_handler,
)
from gitbulk.locks import LockTimeoutError


# ─── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_xdg(monkeypatch, tmp_path):
    cfg = tmp_path / "config"
    cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    paths.ensure_directories()
    return tmp_path


@pytest.fixture
def write_policy(isolated_xdg):
    """Write a minimal gitbulk.yaml so load_policy() succeeds."""

    def _write():
        cfg = paths.config_dir()
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "gitbulk.yaml").write_text(
            yaml.safe_dump({"defaults": {"retain_runs": 5}})
        )

    return _write


@pytest.fixture
def fake_report_run(isolated_xdg):
    """Create a fake `latest-report` symlink + state.yaml in the cache.

    Returns the run directory path. Tests that want NO prior run skip
    this fixture.
    """

    def _create(state_yaml: dict | None = None):
        runs = paths.runs_dir()
        runs.mkdir(parents=True, exist_ok=True)
        run_dir = runs / "20260528T000000Z-report"
        run_dir.mkdir()
        payload = state_yaml if state_yaml is not None else {
            "schema_version": 1,
            "repos": {},
        }
        (run_dir / "state.yaml").write_text(yaml.safe_dump(payload))
        symlink = paths.latest_run_symlink("report")
        symlink.symlink_to(run_dir)
        return run_dir

    return _create


def _make_args(*, prompt: str | None = None, model: str | None = None):
    return argparse.Namespace(
        subcommand="summarize",
        prompt=prompt,
        model=model,
    )


def _inject_fake_claude(monkeypatch, fake: FakeClaudeClient) -> None:
    """Replace ProductionClaudeClient in the handler with a fake.

    Same pattern as test_report.py's ProductionGHClient injection: the
    handler imports the class by name, so the monkeypatch target is the
    name as it appears at the call site.
    """
    monkeypatch.setattr(
        "gitbulk.commands.summarize.ProductionClaudeClient", lambda: fake
    )


# ─── _top_attention_items: parsing heuristic ───────────────────────────────


def test_top_attention_items_extracts_bullets():
    output = (
        "## TOP ATTENTION\n"
        "- repo/a#1 failing checks\n"
        "- repo/b#2 conflicts\n"
        "\n"
        "## BACKBURNER\n"
        "- repo/c#3 slow review\n"
        "\n"
        "## CLEAN\n"
        "3 PRs are clean.\n"
    )
    items = _top_attention_items(output)
    assert len(items) == 2
    assert "repo/a#1" in items[0]
    assert "repo/b#2" in items[1]


def test_top_attention_items_handles_nothing_sentinel():
    output = (
        "## TOP ATTENTION\n"
        "Nothing requires attention today.\n"
        "\n"
        "## BACKBURNER\n"
        "- something\n"
    )
    assert _top_attention_items(output) == []


def test_top_attention_items_returns_empty_when_section_absent():
    output = "## BACKBURNER\n- foo\n"
    assert _top_attention_items(output) == []


def test_top_attention_items_counts_url_lines_too():
    output = (
        "## TOP ATTENTION\n"
        "- repo/a#1 failing checks\n"
        "https://github.com/repo/a/pull/1\n"
        "## BACKBURNER\n"
    )
    # Two non-empty lines under the heading → two items. The heuristic is
    # lenient by design (smprmpt4n.a) — better to over-count by a bullet's
    # URL line than to silently drop a real attention item.
    assert len(_top_attention_items(output)) == 2


def test_top_attention_items_is_case_insensitive_on_heading():
    output = "## top attention\n- foo\n## CLEAN\n"
    assert _top_attention_items(output) == ["- foo"]


# ─── _runid_from_run_dir ───────────────────────────────────────────────────


def test_runid_from_run_dir_strips_summarize_suffix(tmp_path):
    run_dir = tmp_path / "20260528T000000Z-summarize"
    run_dir.mkdir()
    assert _runid_from_run_dir(run_dir) == "20260528T000000Z"


def test_runid_from_run_dir_falls_back_for_unknown_suffix(tmp_path):
    run_dir = tmp_path / "20260528T000000Z-other"
    run_dir.mkdir()
    # Falls back to rpartition on '-'.
    assert _runid_from_run_dir(run_dir) == "20260528T000000Z"


# ─── _default_prompt_path ─────────────────────────────────────────────────


def test_default_prompt_path_points_at_packaged_triage_md():
    p = _default_prompt_path()
    assert p.name == "triage.md"
    assert p.parent.name == "prompts"
    # Sanity: the file exists in the repo.
    assert p.exists()


# ─── Handler: structural failures BEFORE the lock ──────────────────────────


def test_summarize_no_previous_report_returns_structural_failure(
    isolated_xdg, write_policy, capsys
):
    write_policy()
    # No latest-report symlink in the cache.
    rc = summarize_handler(_make_args())
    assert rc == EXIT_STRUCTURAL_FAILURE
    err = capsys.readouterr().err
    assert "no `gitbulk report` run found" in err


def test_summarize_missing_state_yaml_returns_structural_failure(
    isolated_xdg, write_policy, capsys
):
    """Symlink exists but the run directory has no state.yaml."""
    write_policy()
    runs = paths.runs_dir()
    runs.mkdir(parents=True, exist_ok=True)
    empty_run = runs / "20260528T000000Z-report"
    empty_run.mkdir()
    # NOTE: deliberately do not write state.yaml here.
    paths.latest_run_symlink("report").symlink_to(empty_run)

    rc = summarize_handler(_make_args())
    assert rc == EXIT_STRUCTURAL_FAILURE
    err = capsys.readouterr().err
    assert "no state.yaml" in err


# ─── Handler: happy path ───────────────────────────────────────────────────


def test_summarize_happy_no_top_attention_returns_ok(
    monkeypatch, isolated_xdg, write_policy, fake_report_run
):
    write_policy()
    fake_report_run({"schema_version": 1, "repos": {}})
    fake_claude = FakeClaudeClient(
        lambda prompt, input_text: (
            "## TOP ATTENTION\n"
            "Nothing requires attention today.\n"
            "## BACKBURNER\n"
            "## CLEAN\n"
            "5 PRs are clean.\n"
        )
    )
    _inject_fake_claude(monkeypatch, fake_claude)

    rc = summarize_handler(_make_args())
    assert rc == EXIT_OK
    assert not sentinel.has_attention()
    # The summarize run dir exists and has the claude output as summary.md.
    latest = paths.latest_run_symlink("summarize").resolve()
    summary_md = (latest / "summary.md").read_text()
    assert "TOP ATTENTION" in summary_md
    # Manifest was finalized with exit code 0.
    manifest = yaml.safe_load((latest / "manifest.yaml").read_text())
    assert manifest["exit_code"] == EXIT_OK
    assert manifest["config_snapshot"]["model"] is None
    # Claude was invoked exactly once with state.yaml as input.
    assert fake_claude.call_count == 1
    assert "schema_version" in fake_claude.last_call["input_text"]


def test_summarize_summary_line_colorized_under_force_color(
    monkeypatch, isolated_xdg, write_policy, fake_report_run, capsys
):
    """End-to-end wiring proof: with FORCE_COLOR the success summary line
    carries the green outcome color on stdout (summary_line is reached
    with the right exit code). The glyph character itself (✓ vs ASCII
    fallback) is encoding-dependent and covered in test_style; here we
    assert only the deterministic color escapes plus the message."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    write_policy()
    fake_report_run({"schema_version": 1, "repos": {}})
    fake_claude = FakeClaudeClient(
        lambda prompt, input_text: (
            "## TOP ATTENTION\n"
            "Nothing requires attention today.\n"
            "## BACKBURNER\n"
            "## CLEAN\n"
            "5 PRs are clean.\n"
        )
    )
    _inject_fake_claude(monkeypatch, fake_claude)

    rc = summarize_handler(_make_args())
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "\033[32m" in out  # green (ok) outcome
    assert "\033[0m" in out
    assert "nothing requires attention" in out


def test_summarize_top_attention_sets_sentinel_and_exits_2(
    monkeypatch, isolated_xdg, write_policy, fake_report_run
):
    write_policy()
    fake_report_run()
    fake_claude = FakeClaudeClient(
        {
            "": (
                "## TOP ATTENTION\n"
                "- foo/bar#1 failing checks\n"
                "- foo/baz#9 blocked on review\n"
                "## CLEAN\n"
                "0\n"
            )
        }
    )
    _inject_fake_claude(monkeypatch, fake_claude)

    rc = summarize_handler(_make_args())
    assert rc == EXIT_ATTENTION_NEEDED
    parsed = sentinel.parse_attention()
    assert parsed is not None
    assert parsed["exit_code"] == EXIT_ATTENTION_NEEDED
    assert parsed["subcommand"] == "summarize"
    assert "2 attention items" in parsed["summary"]
    # The runid in the sentinel matches the dir name (minus -summarize).
    latest = paths.latest_run_symlink("summarize").resolve()
    assert parsed["runid"] == _runid_from_run_dir(latest)


# ─── Handler: claude failure → structural failure with errors.log entry ────


def test_summarize_claude_error_returns_structural_failure(
    monkeypatch, isolated_xdg, write_policy, fake_report_run, capsys
):
    write_policy()
    fake_report_run()

    def _boom(prompt, input_text):
        raise ClaudeError("model exploded")

    fake_claude = FakeClaudeClient(_boom)
    _inject_fake_claude(monkeypatch, fake_claude)

    rc = summarize_handler(_make_args())
    assert rc == EXIT_STRUCTURAL_FAILURE
    assert not sentinel.has_attention()
    err = capsys.readouterr().err
    assert "claude failed" in err
    # errors.log records the failure.
    latest = paths.latest_run_symlink("summarize").resolve()
    errors_path = latest / "errors.log"
    assert errors_path.exists()
    errors = [json.loads(line) for line in errors_path.read_text().splitlines()]
    assert any("claude" in e["message"] for e in errors)
    # Fallback summary.md was written.
    summary = (latest / "summary.md").read_text()
    assert "FAILED" in summary


# ─── --prompt override ────────────────────────────────────────────────────


def test_summarize_prompt_override_loads_alternate_file(
    monkeypatch, isolated_xdg, write_policy, fake_report_run, tmp_path
):
    write_policy()
    fake_report_run()
    alt_prompt = tmp_path / "alt-prompt.md"
    alt_prompt.write_text("ALTERNATE PROMPT BODY")

    captured: dict = {}

    def _respond(prompt, input_text):
        captured["prompt"] = prompt
        return "## TOP ATTENTION\nNothing requires attention today.\n"

    fake_claude = FakeClaudeClient(_respond)
    _inject_fake_claude(monkeypatch, fake_claude)

    rc = summarize_handler(_make_args(prompt=str(alt_prompt)))
    assert rc == EXIT_OK
    assert captured["prompt"] == "ALTERNATE PROMPT BODY"
    latest = paths.latest_run_symlink("summarize").resolve()
    manifest = yaml.safe_load((latest / "manifest.yaml").read_text())
    assert manifest["config_snapshot"]["prompt_path"] == str(alt_prompt)


def test_summarize_prompt_override_missing_file_returns_structural_failure(
    monkeypatch, isolated_xdg, write_policy, fake_report_run, capsys
):
    write_policy()
    fake_report_run()
    # No fake claude needed; failure happens before the call. But still
    # inject one so an accidental invocation fails the test loudly.
    _inject_fake_claude(monkeypatch, FakeClaudeClient())

    rc = summarize_handler(_make_args(prompt="/nonexistent/prompt.md"))
    assert rc == EXIT_STRUCTURAL_FAILURE
    err = capsys.readouterr().err
    assert "prompt file not found" in err
    latest = paths.latest_run_symlink("summarize").resolve()
    summary = (latest / "summary.md").read_text()
    assert "FAILED" in summary


# ─── --model override ─────────────────────────────────────────────────────


def test_summarize_model_override_passes_through_to_claude(
    monkeypatch, isolated_xdg, write_policy, fake_report_run
):
    write_policy()
    fake_report_run()
    fake_claude = FakeClaudeClient({"": "## CLEAN\n0\n"})
    _inject_fake_claude(monkeypatch, fake_claude)

    rc = summarize_handler(_make_args(model="opus"))
    assert rc == EXIT_OK
    assert fake_claude.last_call["model"] == "opus"
    latest = paths.latest_run_symlink("summarize").resolve()
    manifest = yaml.safe_load((latest / "manifest.yaml").read_text())
    assert manifest["config_snapshot"]["model"] == "opus"


# ─── Lock timeout ──────────────────────────────────────────────────────────


def test_summarize_lock_timeout_returns_structural_failure(
    monkeypatch, isolated_xdg, write_policy, fake_report_run, capsys
):
    write_policy()
    fake_report_run()

    class _BoomLock:
        def __enter__(self):
            raise LockTimeoutError(
                paths.global_lock_file(),
                {
                    "pid": 999,
                    "started_at": "1970-01-01T00:00:00+00:00",
                    "subcommand": "merge",
                    "alive": False,
                },
            )

        def __exit__(self, *a):  # pragma: no cover — never reached
            return False

    monkeypatch.setattr(
        "gitbulk.commands.summarize.global_lock",
        lambda *a, **kw: _BoomLock(),
    )
    # Inject a fake to make sure no real claude runs even if the lock
    # branch were buggy.
    _inject_fake_claude(monkeypatch, FakeClaudeClient())

    rc = summarize_handler(_make_args())
    assert rc == EXIT_STRUCTURAL_FAILURE
    assert not sentinel.has_attention()
    err = capsys.readouterr().err
    assert "timed out" in err


# ─── CLI integration: argparse wires --prompt / --model through ────────────


def test_cli_summarize_passes_args_to_handler(
    monkeypatch, isolated_xdg, write_policy, fake_report_run
):
    write_policy()
    fake_report_run()
    fake_claude = FakeClaudeClient({"": "## CLEAN\n0\n"})
    _inject_fake_claude(monkeypatch, fake_claude)

    rc = main(["summarize", "--model", "haiku"])
    assert rc == EXIT_OK
    assert fake_claude.last_call["model"] == "haiku"
