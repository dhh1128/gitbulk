"""Tests for dashboard.py (this.i node dwq3kpn4)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gitbulk import dashboard, paths
from gitbulk.runstate import RunState


@pytest.fixture
def isolated_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    paths.ensure_directories()
    return tmp_path


def _finish_run(subcommand: str, summary: str, *, exit_code: int = 0, when: datetime | None = None) -> Path:
    rs = RunState.begin(subcommand, ["gitbulk", subcommand], {}, when=when)
    rs.write_summary(summary)
    rs.complete(exit_code)
    return rs.run_dir


# ─── Basic composition ─────────────────────────────────────────────────────


def test_dashboard_file_created_at_expected_path(isolated_cache):
    out = dashboard.rewrite_dashboard()
    assert out == paths.dashboard_file()
    assert out.exists()


def test_dashboard_has_section_per_subcommand(isolated_cache):
    out = dashboard.rewrite_dashboard()
    text = out.read_text()
    # Every subcommand from cli.SUBCOMMANDS should appear in the dashboard
    from gitbulk.cli import SUBCOMMANDS
    for name, _ in SUBCOMMANDS:
        assert f"## {name}\n" in text


def test_dashboard_no_runs_yet_placeholder(isolated_cache):
    text = dashboard.rewrite_dashboard().read_text()
    assert "_no runs yet_" in text


# ─── Completed run rendering ───────────────────────────────────────────────


def test_dashboard_renders_completed_run(isolated_cache):
    when = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)
    run_dir = _finish_run("report", "Hello from summary.", exit_code=2, when=when)
    text = dashboard.rewrite_dashboard().read_text()
    assert "## report" in text
    assert "20260527T120000Z" in text
    assert "Exit: `2`" in text
    assert "Hello from summary." in text
    # Completed run must NOT have [INCOMPLETE]
    assert "[INCOMPLETE]" not in text or "[INCOMPLETE]" not in text.split("## report")[1].split("##")[0]


def test_dashboard_truncates_long_summaries(isolated_cache):
    when = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)
    long_summary = "\n".join(f"line {i}" for i in range(100))
    _finish_run("report", long_summary, when=when)
    text = dashboard.rewrite_dashboard().read_text()
    assert "truncated" in text
    # First 15 lines should be present; "line 99" (after truncation point) should not be in the excerpt
    report_section = text.split("## report")[1].split("##")[0] if "##" in text.split("## report")[1] else text.split("## report")[1]
    assert "line 0" in report_section
    assert "line 99" not in report_section.split("truncated")[0]


# ─── Incomplete-run marking ────────────────────────────────────────────────


def test_dashboard_marks_incomplete_runs(isolated_cache):
    """Simulate a crashed run: create the run dir + symlink but never call complete()."""
    when = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)
    rs = RunState.begin("merge", [], {}, when=when)
    rs.write_summary("partial summary before crash")
    # Manually create the latest-* symlink (normally done by complete())
    symlink = paths.latest_run_symlink("merge")
    symlink.symlink_to(rs.run_dir.name)
    text = dashboard.rewrite_dashboard().read_text()
    merge_section = text.split("## merge")[1].split("##")[0]
    assert "[INCOMPLETE]" in merge_section
    assert "partial summary before crash" in merge_section


# ─── Missing summary handling ──────────────────────────────────────────────


def test_dashboard_handles_missing_summary(isolated_cache):
    when = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)
    rs = RunState.begin("report", [], {}, when=when)
    rs.complete(0)  # complete without write_summary
    text = dashboard.rewrite_dashboard().read_text()
    assert "no summary.md written" in text


def test_dashboard_handles_missing_manifest(isolated_cache):
    """Defensive path: run dir exists but manifest.yaml has been deleted."""
    when = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)
    rs = RunState.begin("report", [], {}, when=when)
    rs.complete(0)
    # Delete the manifest after completion to exercise the missing-manifest branch
    (rs.run_dir / "manifest.yaml").unlink()
    text = dashboard.rewrite_dashboard().read_text()
    # Section still renders, with question-mark exit code and no completed_at
    report_section = text.split("## report")[1].split("##")[0]
    assert "Exit: `?`" in report_section


# ─── Broken symlink handling ───────────────────────────────────────────────


def test_dashboard_skips_dangling_symlink(isolated_cache):
    """If latest-* points at a deleted run dir, render the section as 'no runs yet' rather than crash."""
    symlink = paths.latest_run_symlink("report")
    symlink.symlink_to("nonexistent-run-dir")
    text = dashboard.rewrite_dashboard().read_text()
    report_section = text.split("## report")[1].split("##")[0]
    assert "_no runs yet_" in report_section


# ─── Custom subcommands argument ───────────────────────────────────────────


def test_dashboard_accepts_explicit_subcommand_list(isolated_cache):
    out = dashboard.rewrite_dashboard(subcommands=["report", "merge"])
    text = out.read_text()
    assert "## report" in text
    assert "## merge" in text
    # Other subcommands should NOT appear
    assert "## dispatch" not in text


# ─── Atomic write ──────────────────────────────────────────────────────────


def test_dashboard_no_tmp_left_after_write(isolated_cache):
    dashboard.rewrite_dashboard()
    tmp_path = paths.dashboard_file().parent / (paths.dashboard_file().name + ".tmp")
    assert not tmp_path.exists()


def test_dashboard_overwrites_existing(isolated_cache):
    when_a = datetime(2026, 5, 27, 10, 0, 0, tzinfo=timezone.utc)
    _finish_run("report", "version A", when=when_a)
    dashboard.rewrite_dashboard()
    text_a = paths.dashboard_file().read_text()
    assert "version A" in text_a

    when_b = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)
    _finish_run("report", "version B", when=when_b)
    dashboard.rewrite_dashboard()
    text_b = paths.dashboard_file().read_text()
    assert "version B" in text_b
