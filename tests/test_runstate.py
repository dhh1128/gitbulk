"""Tests for runstate.py (this.i node kp7nw4mq)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from gitbulk import __version__, paths
from gitbulk.runstate import (
    SCHEMA_VERSION,
    RunState,
    _atomic_write_symlink,
    _atomic_write_text,
)


@pytest.fixture
def isolated_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    paths.ensure_directories()
    return tmp_path


# ─── begin() ────────────────────────────────────────────────────────────────


def test_begin_creates_run_directory(isolated_cache):
    when = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)
    rs = RunState.begin("report", ["gitbulk", "report"], {}, when=when)
    assert rs.run_dir.is_dir()
    assert rs.run_dir.name == "20260527T120000Z-report"


def test_begin_writes_manifest_with_expected_fields(isolated_cache):
    when = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)
    argv = ["gitbulk", "report", "--apply"]
    config = {"defaults": {"merge_policy": "strict"}}
    rs = RunState.begin("report", argv, config, when=when)
    manifest = yaml.safe_load((rs.run_dir / "manifest.yaml").read_text())
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["gitbulk_version"] == __version__
    assert manifest["subcommand"] == "report"
    assert manifest["argv"] == argv
    assert manifest["config_snapshot"] == config
    assert "started_at" in manifest
    # completed_at and exit_code only appear after complete()
    assert "completed_at" not in manifest
    assert "exit_code" not in manifest


def test_begin_writes_initial_empty_state_yaml(isolated_cache):
    rs = RunState.begin("merge", [], {})
    state = yaml.safe_load((rs.run_dir / "state.yaml").read_text())
    assert state == {"schema_version": SCHEMA_VERSION, "repos": {}}


def test_begin_run_dir_property_matches_paths_module(isolated_cache):
    when = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)
    rs = RunState.begin("dispatch", [], {}, when=when)
    expected = paths.run_dir("20260527T120000Z", "dispatch")
    assert rs.run_dir == expected


def test_begin_raises_if_run_dir_already_exists(isolated_cache):
    when = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)
    RunState.begin("report", [], {}, when=when)
    with pytest.raises(FileExistsError):
        RunState.begin("report", [], {}, when=when)


# ─── record_invariant() ────────────────────────────────────────────────────


def test_record_invariant_appends_jsonl_event(isolated_cache):
    rs = RunState.begin("report", [], {})
    rs.record_invariant("gh.authenticated", "global", "PASS", None)
    lines = (rs.run_dir / "invariants.log").read_text().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["v"] == SCHEMA_VERSION
    assert event["name"] == "gh.authenticated"
    assert event["target"] == "global"
    assert event["result"] == "PASS"
    assert event["reason"] is None
    assert "ts" in event


def test_record_invariant_multiple_appends(isolated_cache):
    rs = RunState.begin("report", [], {})
    rs.record_invariant("a", "t1", "PASS")
    rs.record_invariant("b", "t2", "SKIP", "reason b")
    rs.record_invariant("c", "t3", "FAIL", "reason c")
    lines = (rs.run_dir / "invariants.log").read_text().splitlines()
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    assert [e["name"] for e in parsed] == ["a", "b", "c"]
    assert [e["result"] for e in parsed] == ["PASS", "SKIP", "FAIL"]
    assert parsed[1]["reason"] == "reason b"


def test_record_invariant_rejects_invalid_result(isolated_cache):
    rs = RunState.begin("report", [], {})
    with pytest.raises(ValueError, match="invalid invariant result"):
        rs.record_invariant("x", "t", "MAYBE")
    # And invariants.log should be empty (no event written before validation)
    assert not (rs.run_dir / "invariants.log").exists()


# ─── record_error() ────────────────────────────────────────────────────────


def test_record_error_appends_jsonl_with_default_level(isolated_cache):
    rs = RunState.begin("report", [], {})
    rs.record_error("something went wrong")
    lines = (rs.run_dir / "errors.log").read_text().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["v"] == SCHEMA_VERSION
    assert event["level"] == "ERROR"
    assert event["message"] == "something went wrong"
    assert event["context"] == {}
    assert "ts" in event


def test_record_error_custom_level(isolated_cache):
    rs = RunState.begin("report", [], {})
    rs.record_error("just a warning", level="WARNING")
    event = json.loads((rs.run_dir / "errors.log").read_text().splitlines()[0])
    assert event["level"] == "WARNING"


def test_record_error_with_context(isolated_cache):
    rs = RunState.begin("report", [], {})
    rs.record_error("repo failure", context={"slug": "owner/repo", "code": 42})
    event = json.loads((rs.run_dir / "errors.log").read_text().splitlines()[0])
    assert event["context"] == {"slug": "owner/repo", "code": 42}


def test_record_error_without_context_uses_empty_dict(isolated_cache):
    rs = RunState.begin("report", [], {})
    rs.record_error("no context")
    event = json.loads((rs.run_dir / "errors.log").read_text().splitlines()[0])
    assert event["context"] == {}


# ─── record_repo_state() ───────────────────────────────────────────────────


def test_record_repo_state_writes_state_yaml(isolated_cache):
    rs = RunState.begin("merge", [], {})
    rs.record_repo_state("owner/repo", {"prs_seen": 3, "merged": 1})
    state = yaml.safe_load((rs.run_dir / "state.yaml").read_text())
    assert state == {
        "schema_version": SCHEMA_VERSION,
        "repos": {"owner/repo": {"prs_seen": 3, "merged": 1}},
    }


def test_record_repo_state_preserves_earlier_repos(isolated_cache):
    rs = RunState.begin("merge", [], {})
    rs.record_repo_state("owner1/repo1", {"x": 1})
    rs.record_repo_state("owner2/repo2", {"y": 2})
    rs.record_repo_state("owner1/repo1", {"x": 99})  # update repo1
    state = yaml.safe_load((rs.run_dir / "state.yaml").read_text())
    assert state == {
        "schema_version": SCHEMA_VERSION,
        "repos": {
            "owner1/repo1": {"x": 99},
            "owner2/repo2": {"y": 2},
        },
    }


def test_record_repo_state_leaves_no_tmp_file(isolated_cache):
    rs = RunState.begin("merge", [], {})
    rs.record_repo_state("owner/repo", {"x": 1})
    # No state.yaml.tmp should remain
    assert not (rs.run_dir / "state.yaml.tmp").exists()


# ─── write_summary() ───────────────────────────────────────────────────────


def test_write_summary(isolated_cache):
    rs = RunState.begin("report", [], {})
    rs.write_summary("# Run Summary\n\nAll good.\n")
    assert (rs.run_dir / "summary.md").read_text().startswith("# Run Summary")


# ─── complete() ────────────────────────────────────────────────────────────


def test_complete_adds_completion_fields_to_manifest(isolated_cache):
    when = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)
    rs = RunState.begin("report", [], {}, when=when)
    rs.complete(exit_code=2)
    manifest = yaml.safe_load((rs.run_dir / "manifest.yaml").read_text())
    assert manifest["exit_code"] == 2
    assert "completed_at" in manifest
    # started_at preserved across rewrite
    assert "started_at" in manifest


def test_complete_creates_latest_symlink(isolated_cache):
    when = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)
    rs = RunState.begin("merge", [], {}, when=when)
    rs.complete(0)
    symlink = paths.latest_run_symlink("merge")
    assert symlink.is_symlink()
    # Resolve through the symlink and check it points to our run dir
    assert symlink.resolve() == rs.run_dir.resolve()


def test_complete_symlink_target_is_relative(isolated_cache):
    when = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)
    rs = RunState.begin("merge", [], {}, when=when)
    rs.complete(0)
    symlink = paths.latest_run_symlink("merge")
    target = os.readlink(symlink)
    # Must not be absolute
    assert not Path(target).is_absolute()
    assert target == "20260527T120000Z-merge"


def test_complete_replaces_existing_latest_symlink(isolated_cache):
    when_a = datetime(2026, 5, 27, 10, 0, 0, tzinfo=timezone.utc)
    when_b = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)
    rs_a = RunState.begin("report", [], {}, when=when_a)
    rs_a.complete(0)
    rs_b = RunState.begin("report", [], {}, when=when_b)
    rs_b.complete(0)
    symlink = paths.latest_run_symlink("report")
    # Should now point at the newer run
    assert symlink.resolve() == rs_b.run_dir.resolve()


# ─── Atomic-write helpers ──────────────────────────────────────────────────


def test_atomic_write_text_creates_file_and_no_tmp(tmp_path):
    target = tmp_path / "x.txt"
    _atomic_write_text(target, "hello")
    assert target.read_text() == "hello"
    assert not (tmp_path / "x.txt.tmp").exists()


def test_atomic_write_symlink_cleans_existing_tmp(tmp_path):
    """Cover the branch where the .tmp symlink already exists from a prior failure."""
    target_a = tmp_path / "target-a"
    target_b = tmp_path / "target-b"
    target_a.mkdir()
    target_b.mkdir()
    symlink = tmp_path / "latest"
    tmp_symlink = tmp_path / "latest.tmp"
    # Simulate a stale .tmp left behind by a previous interrupted call
    tmp_symlink.symlink_to(target_a)
    # Now do a clean atomic write; should clean up the stale tmp
    _atomic_write_symlink(symlink, target_b)
    assert symlink.is_symlink()
    assert symlink.resolve() == target_b.resolve()
    assert not tmp_symlink.exists()
