"""Tests for gc.py — retention sweep (this.i node jw3kpn4q Track A)."""

from __future__ import annotations

import pytest

from gitbulk import gc, paths


@pytest.fixture
def isolated_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    paths.ensure_directories()
    return tmp_path


def _make_run(subcommand: str, runid: str) -> None:
    d = paths.runs_dir() / f"{runid}-{subcommand}"
    d.mkdir(parents=True)
    (d / "manifest.yaml").write_text("schema_version: 1\n")


def test_prune_runs_keeps_newest_retain(isolated_cache):
    for runid in (
        "20260101T120000Z",
        "20260102T120000Z",
        "20260103T120000Z",
        "20260104T120000Z",
        "20260105T120000Z",
    ):
        _make_run("report", runid)
    deleted = gc.prune_runs("report", retain=2)
    remaining = sorted(p.name for p in paths.runs_dir().iterdir() if p.is_dir())
    assert remaining == [
        "20260104T120000Z-report",
        "20260105T120000Z-report",
    ]
    deleted_names = sorted(p.name for p in deleted)
    assert deleted_names == [
        "20260101T120000Z-report",
        "20260102T120000Z-report",
        "20260103T120000Z-report",
    ]


def test_prune_runs_no_op_when_under_retain(isolated_cache):
    for runid in ("20260101T120000Z", "20260102T120000Z"):
        _make_run("report", runid)
    deleted = gc.prune_runs("report", retain=10)
    assert deleted == []
    assert len(list(paths.runs_dir().iterdir())) == 2


def test_prune_runs_no_op_when_exactly_retain(isolated_cache):
    for runid in ("20260101T120000Z", "20260102T120000Z", "20260103T120000Z"):
        _make_run("report", runid)
    deleted = gc.prune_runs("report", retain=3)
    assert deleted == []


def test_prune_runs_ignores_other_subcommands(isolated_cache):
    _make_run("report", "20260101T120000Z")
    _make_run("merge", "20260102T120000Z")
    _make_run("merge", "20260103T120000Z")
    _make_run("merge", "20260104T120000Z")
    deleted = gc.prune_runs("merge", retain=2)
    assert len(deleted) == 1
    # report run untouched
    assert (paths.runs_dir() / "20260101T120000Z-report").is_dir()
    # only oldest merge deleted
    assert not (paths.runs_dir() / "20260102T120000Z-merge").exists()
    assert (paths.runs_dir() / "20260103T120000Z-merge").is_dir()
    assert (paths.runs_dir() / "20260104T120000Z-merge").is_dir()


def test_prune_runs_preserves_latest_symlink_target(isolated_cache):
    """If latest-<sub> points at an older run, that run must survive prune."""
    _make_run("report", "20260101T120000Z")
    _make_run("report", "20260102T120000Z")
    _make_run("report", "20260103T120000Z")
    # Symlink at an OLD run (anomalous but defensible — e.g., user did show + symlink)
    symlink = paths.latest_run_symlink("report")
    symlink.symlink_to("20260101T120000Z-report")
    deleted = gc.prune_runs("report", retain=1)
    # Newest run kept by the top-N rule; oldest also kept because the symlink
    # targets it.
    assert (paths.runs_dir() / "20260103T120000Z-report").is_dir()
    assert (paths.runs_dir() / "20260101T120000Z-report").is_dir()
    # Middle run deleted.
    assert not (paths.runs_dir() / "20260102T120000Z-report").exists()
    assert [p.name for p in deleted] == ["20260102T120000Z-report"]


def test_prune_runs_tolerates_dangling_latest_symlink(isolated_cache):
    _make_run("report", "20260101T120000Z")
    _make_run("report", "20260102T120000Z")
    _make_run("report", "20260103T120000Z")
    symlink = paths.latest_run_symlink("report")
    symlink.symlink_to("never-existed-run")
    deleted = gc.prune_runs("report", retain=1)
    # Dangling symlink does not block deletion of the older two.
    assert sorted(p.name for p in deleted) == [
        "20260101T120000Z-report",
        "20260102T120000Z-report",
    ]


def test_prune_runs_runs_dir_missing(isolated_cache, monkeypatch, tmp_path):
    """If the runs dir doesn't exist, prune is a no-op (no error)."""
    nonexistent = tmp_path / "missing"
    deleted = gc.prune_runs("report", retain=5, runs_root=nonexistent)
    assert deleted == []


def test_prune_runs_rejects_retain_below_one():
    with pytest.raises(ValueError, match="retain must be >= 1"):
        gc.prune_runs("report", retain=0)


def test_prune_runs_with_explicit_runs_root(tmp_path):
    """The runs_root override lets callers (and tests) target an arbitrary dir."""
    root = tmp_path / "custom-runs"
    root.mkdir()
    for runid in ("20260101T120000Z", "20260102T120000Z", "20260103T120000Z"):
        (root / f"{runid}-report").mkdir()
    deleted = gc.prune_runs("report", retain=1, runs_root=root)
    assert len(deleted) == 2
    assert (root / "20260103T120000Z-report").is_dir()
