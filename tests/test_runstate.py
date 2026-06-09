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
    # actor is seeded null at begin (stamped later by gh.authenticated); node
    # actrstmp7q. The key is always present so audit consumers get a stable
    # schema, with null meaning "no verified identity recorded".
    assert "actor" in manifest
    assert manifest["actor"] is None
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


def test_begin_same_second_gets_distinct_run_dirs(isolated_cache):
    """Two same-subcommand runs in the same UTC second must not collide.

    Node rsclk7nq Phase 0: the runid is to-the-second, so a second run at the
    same instant used to crash on mkdir(exist_ok=False). begin() now advances
    the timestamp by a second and retries; both runs get distinct dirs whose
    names remain valid `<timestamp>-<subcommand>` (gc/parsers keep working).
    """
    when = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)
    rs1 = RunState.begin("report", [], {}, when=when)
    rs2 = RunState.begin("report", [], {}, when=when)
    assert rs1.run_dir != rs2.run_dir
    assert rs1.run_dir == paths.run_dir("20260527T120000Z", "report")
    assert rs2.run_dir == paths.run_dir("20260527T120001Z", "report")
    # Both names still end in "-report" so gc.prune_runs matches them.
    assert rs1.run_dir.name.endswith("-report")
    assert rs2.run_dir.name.endswith("-report")


def test_begin_raises_after_collision_limit(isolated_cache, monkeypatch):
    """The retry loop is bounded; exhausting it surfaces a FileExistsError."""
    import gitbulk.runstate as runstate_mod

    monkeypatch.setattr(runstate_mod, "_RUNID_COLLISION_LIMIT", 3)
    when = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)
    # Occupy the 3 slots the loop will try (offset 0,1,2).
    for sec in range(3):
        ts = datetime(2026, 5, 27, 12, 0, sec, tzinfo=timezone.utc)
        RunState.begin("report", [], {}, when=ts)
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
    rs.flush_state()
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
    rs.flush_state()
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
    rs.flush_state()
    # No state.yaml.tmp should remain
    assert not (rs.run_dir / "state.yaml.tmp").exists()


# ─── deferred write semantics (node 7gpd / PERF-F1) ─────────────────────────


def test_record_repo_state_defers_write_until_flush(isolated_cache):
    """record_repo_state accumulates in memory; state.yaml stays at the
    begin()-time empty snapshot until flush_state() (O(n^2) → O(n) fix)."""
    rs = RunState.begin("merge", [], {})
    rs.record_repo_state("owner/repo", {"x": 1})
    # Not yet written — still the initial empty state from begin().
    on_disk = yaml.safe_load((rs.run_dir / "state.yaml").read_text())
    assert on_disk == {"schema_version": SCHEMA_VERSION, "repos": {}}
    rs.flush_state()
    flushed = yaml.safe_load((rs.run_dir / "state.yaml").read_text())
    assert flushed["repos"] == {"owner/repo": {"x": 1}}


def test_record_repo_state_deep_copies_payload(isolated_cache):
    """A caller mutating the payload after record_repo_state must not change
    the eventually-flushed snapshot (node 7gpd review — matches set_repos)."""
    rs = RunState.begin("merge", [], {})
    payload = {"prs": [1], "merged": 0}
    rs.record_repo_state("o/r", payload)
    payload["prs"].append(2)  # mutate nested list after recording
    payload["merged"] = 99
    rs.flush_state()
    state = yaml.safe_load((rs.run_dir / "state.yaml").read_text())
    assert state["repos"]["o/r"] == {"prs": [1], "merged": 0}


def test_record_extra_deep_copies_value(isolated_cache):
    """A caller mutating the value after record_extra must not change the
    eventually-flushed snapshot."""
    rs = RunState.begin("report", [], {})
    value = [{"slug": "a/b"}]
    rs.record_extra("recent_merges", value)
    value.append({"slug": "c/d"})  # mutate after recording
    value[0]["slug"] = "mutated"
    rs.flush_state()
    doc = yaml.safe_load((rs.run_dir / "state.yaml").read_text())
    assert doc["recent_merges"] == [{"slug": "a/b"}]


def test_flush_state_is_noop_when_not_dirty(isolated_cache):
    """flush_state() with nothing pending does not rewrite state.yaml."""
    rs = RunState.begin("merge", [], {})
    before = (rs.run_dir / "state.yaml").read_text()
    rs.flush_state()  # nothing recorded → no-op
    assert (rs.run_dir / "state.yaml").read_text() == before


def test_flush_state_second_call_after_flush_is_noop(isolated_cache):
    """A flush clears the dirty flag; a redundant flush writes nothing new."""
    rs = RunState.begin("merge", [], {})
    rs.record_repo_state("o/r", {"x": 1})
    rs.flush_state()
    snapshot = (rs.run_dir / "state.yaml").read_text()
    rs.flush_state()  # not dirty anymore
    assert (rs.run_dir / "state.yaml").read_text() == snapshot


def test_complete_flushes_pending_state(isolated_cache):
    """complete() flushes accumulated state even with no explicit flush."""
    rs = RunState.begin("merge", [], {})
    rs.record_repo_state("owner/repo", {"merged": 1})
    rs.complete(0)
    state = yaml.safe_load((rs.run_dir / "state.yaml").read_text())
    assert state["repos"] == {"owner/repo": {"merged": 1}}


# ─── set_repos() ───────────────────────────────────────────────────────────


def test_set_repos_replaces_whole_map_in_one_write(isolated_cache):
    rs = RunState.begin("prune-branches", [], {})
    rs.record_repo_state("owner/old", {"x": 1})
    rs.set_repos({"owner/a": {"v": 1}, "owner/b": {"v": 2}})
    rs.flush_state()
    state = yaml.safe_load((rs.run_dir / "state.yaml").read_text())
    # The earlier "owner/old" is gone — set_repos REPLACES, not merges.
    assert state["repos"] == {"owner/a": {"v": 1}, "owner/b": {"v": 2}}


def test_set_repos_preserves_extras(isolated_cache):
    rs = RunState.begin("prune-branches", [], {})
    rs.record_extra("prune_plan", {"scope_slugs": ["o/a"]})
    rs.set_repos({"o/a": {"v": 1}})
    rs.flush_state()
    state = yaml.safe_load((rs.run_dir / "state.yaml").read_text())
    assert state["prune_plan"] == {"scope_slugs": ["o/a"]}
    assert state["repos"] == {"o/a": {"v": 1}}


def test_set_repos_deep_copies_input(isolated_cache):
    rs = RunState.begin("prune-branches", [], {})
    payload = {"o/a": {"branches": [1]}}
    rs.set_repos(payload)
    payload["o/a"]["branches"].append(2)  # mutate caller's nested list
    rs.record_extra("k", "v")  # another mutation, still buffered
    rs.flush_state()  # single deferred re-dump of _per_repo
    state = yaml.safe_load((rs.run_dir / "state.yaml").read_text())
    # Deep copy means the caller's later mutation never reached the re-dump.
    assert state["repos"]["o/a"]["branches"] == [1]


# ─── record_extra() ────────────────────────────────────────────────────────


def test_record_extra_writes_top_level_key(isolated_cache):
    """record_extra adds a top-level key alongside repos/schema_version."""
    rs = RunState.begin("report", [], {})
    rs.record_extra("recent_merges", [{"slug": "a/b", "sha": "x"}])
    rs.flush_state()
    doc = yaml.safe_load((rs.run_dir / "state.yaml").read_text())
    assert doc["recent_merges"] == [{"slug": "a/b", "sha": "x"}]
    # repos and schema_version still present.
    assert "repos" in doc
    assert doc["schema_version"] == 1


def test_record_extra_overwrites_on_repeat(isolated_cache):
    rs = RunState.begin("report", [], {})
    rs.record_extra("recent_merges", [{"slug": "a/b"}])
    rs.record_extra("recent_merges", [{"slug": "c/d"}])
    rs.flush_state()
    doc = yaml.safe_load((rs.run_dir / "state.yaml").read_text())
    assert doc["recent_merges"] == [{"slug": "c/d"}]


def test_record_extra_coexists_with_record_repo_state(isolated_cache):
    """Both record_extra and record_repo_state writes survive
    interleaving."""
    rs = RunState.begin("report", [], {})
    rs.record_repo_state("a/b", {"prs": []})
    rs.record_extra("recent_merges", [])
    rs.record_repo_state("c/d", {"prs": []})
    rs.flush_state()
    doc = yaml.safe_load((rs.run_dir / "state.yaml").read_text())
    assert set(doc["repos"].keys()) == {"a/b", "c/d"}
    assert doc["recent_merges"] == []


def test_record_extra_rejects_reserved_keys(isolated_cache):
    """schema_version and repos are reserved; trying to overwrite them
    via record_extra raises ValueError."""
    rs = RunState.begin("report", [], {})
    with pytest.raises(ValueError, match="reserved"):
        rs.record_extra("schema_version", 99)
    with pytest.raises(ValueError, match="reserved"):
        rs.record_extra("repos", {})


# ─── write_summary() ───────────────────────────────────────────────────────


def test_write_summary(isolated_cache):
    rs = RunState.begin("report", [], {})
    rs.write_summary("# Run Summary\n\nAll good.\n")
    assert (rs.run_dir / "summary.md").read_text().startswith("# Run Summary")


# ─── record_actor() ──────────────────────────────────────────────────────────


def test_record_actor_stamps_login_into_manifest(isolated_cache):
    rs = RunState.begin("merge", [], {})
    rs.record_actor("dhh1128")
    manifest = yaml.safe_load((rs.run_dir / "manifest.yaml").read_text())
    assert manifest["actor"] == "dhh1128"


def test_record_actor_none_leaves_actor_null(isolated_cache):
    rs = RunState.begin("merge", [], {})
    rs.record_actor(None)
    manifest = yaml.safe_load((rs.run_dir / "manifest.yaml").read_text())
    assert manifest["actor"] is None


def test_record_actor_preserves_other_manifest_fields(isolated_cache):
    when = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)
    argv = ["gitbulk", "merge", "--apply"]
    config = {"defaults": {"merge_policy": "strict"}}
    rs = RunState.begin("merge", argv, config, when=when)
    rs.record_actor("dhh1128")
    manifest = yaml.safe_load((rs.run_dir / "manifest.yaml").read_text())
    assert manifest["actor"] == "dhh1128"
    assert manifest["subcommand"] == "merge"
    assert manifest["argv"] == argv
    assert manifest["config_snapshot"] == config
    assert "started_at" in manifest


def test_record_actor_survives_complete(isolated_cache):
    # complete() does its own read-modify-write of the manifest; the actor
    # stamped earlier must not be lost.
    rs = RunState.begin("merge", [], {})
    rs.record_actor("dhh1128")
    rs.complete(0)
    manifest = yaml.safe_load((rs.run_dir / "manifest.yaml").read_text())
    assert manifest["actor"] == "dhh1128"
    assert manifest["exit_code"] == 0


# ─── record_timings() (node 5agg / PERF-F3) ─────────────────────────────────


def test_record_timings_stamps_rounded_block_into_manifest(isolated_cache):
    rs = RunState.begin("report", [], {})
    rs.record_timings({"preflight": 0.42119, "per_repo": 3.1, "per_pr": 1.875})
    manifest = yaml.safe_load((rs.run_dir / "manifest.yaml").read_text())
    # Rounded to milliseconds for legibility.
    assert manifest["timings"] == {
        "preflight": 0.421,
        "per_repo": 3.1,
        "per_pr": 1.875,
    }


def test_record_timings_survives_complete(isolated_cache):
    """complete() does its own manifest read-modify-write; timings stamped
    earlier must not be lost."""
    rs = RunState.begin("report", [], {})
    rs.record_timings({"preflight": 1.0})
    rs.complete(0)
    manifest = yaml.safe_load((rs.run_dir / "manifest.yaml").read_text())
    assert manifest["timings"] == {"preflight": 1.0}
    assert manifest["exit_code"] == 0


def test_record_timings_empty_mapping_writes_empty_block(isolated_cache):
    rs = RunState.begin("report", [], {})
    rs.record_timings({})
    manifest = yaml.safe_load((rs.run_dir / "manifest.yaml").read_text())
    assert manifest["timings"] == {}


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


def test_complete_with_retain_runs_prunes_old_dirs(isolated_cache):
    """RunState.complete(retain_runs=N) keeps only the newest N runs of this subcommand."""
    when_a = datetime(2026, 5, 27, 10, 0, 0, tzinfo=timezone.utc)
    when_b = datetime(2026, 5, 27, 11, 0, 0, tzinfo=timezone.utc)
    when_c = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)
    for when in (when_a, when_b, when_c):
        rs = RunState.begin("report", [], {}, when=when)
        rs.complete(0, retain_runs=2)
    remaining = sorted(
        p.name
        for p in paths.runs_dir().iterdir()
        if p.is_dir() and not p.is_symlink() and p.name.endswith("-report")
    )
    assert remaining == [
        "20260527T110000Z-report",
        "20260527T120000Z-report",
    ]


def test_complete_without_retain_runs_does_not_prune(isolated_cache):
    """retain_runs=None (the default) leaves old runs alone."""
    when_a = datetime(2026, 5, 27, 10, 0, 0, tzinfo=timezone.utc)
    when_b = datetime(2026, 5, 27, 11, 0, 0, tzinfo=timezone.utc)
    when_c = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)
    for when in (when_a, when_b, when_c):
        rs = RunState.begin("report", [], {}, when=when)
        rs.complete(0)
    remaining = sorted(
        p.name
        for p in paths.runs_dir().iterdir()
        if p.is_dir() and not p.is_symlink() and p.name.endswith("-report")
    )
    assert len(remaining) == 3


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


def test_atomic_write_symlink_delegates_and_points_at_target(tmp_path):
    """The runstate wrapper delegates to atomicio and writes a relative link.

    (Unique-tmp behaviour and stale-fixed-tmp independence are covered in
    test_atomicio.py; here we only pin that the delegating wrapper works.)
    """
    target_a = tmp_path / "target-a"
    target_b = tmp_path / "target-b"
    target_a.mkdir()
    target_b.mkdir()
    symlink = tmp_path / "latest"
    _atomic_write_symlink(symlink, target_a)
    _atomic_write_symlink(symlink, target_b)  # overwrite
    assert symlink.is_symlink()
    assert symlink.resolve() == target_b.resolve()
    assert os.readlink(symlink) == "target-b"  # relative
