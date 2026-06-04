"""Tests for util/atomicio.py (this.i node rsclk7nq, Phase 0 hardening).

The load-bearing property is *unique temp names per call*: a fixed
``<name>.tmp`` sidecar means two concurrent writers of the same target
collide on the temp path and one ``os.replace`` races to ENOENT. These
tests pin the unique-name behaviour, the atomic overwrite, and the
error-path cleanup.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gitbulk.util import atomicio


# ─── atomic_write_text ──────────────────────────────────────────────────────


def test_write_text_creates_file_with_content(tmp_path):
    target = tmp_path / "x.txt"
    atomicio.atomic_write_text(target, "hello")
    assert target.read_text() == "hello"


def test_write_text_overwrites_existing(tmp_path):
    target = tmp_path / "x.txt"
    target.write_text("old")
    atomicio.atomic_write_text(target, "new")
    assert target.read_text() == "new"


def test_write_text_leaves_no_tmp_behind(tmp_path):
    target = tmp_path / "x.txt"
    atomicio.atomic_write_text(target, "hello")
    assert list(tmp_path.iterdir()) == [target]


def test_write_text_uses_unique_tmp_per_call(tmp_path, monkeypatch):
    """Two calls must rename DIFFERENT temp paths, both in the target dir."""
    seen: list[Path] = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(Path(src))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    target = tmp_path / "x.txt"
    atomicio.atomic_write_text(target, "a")
    atomicio.atomic_write_text(target, "b")

    assert seen[0] != seen[1]                       # unique per call
    assert seen[0].parent == target.parent          # same dir → rename is atomic
    assert seen[1].parent == target.parent
    assert target.read_text() == "b"


def test_write_text_survives_stale_fixed_tmp(tmp_path):
    """A leftover fixed-name ``<name>.tmp`` (legacy/interrupted) is irrelevant."""
    target = tmp_path / "x.txt"
    (tmp_path / "x.txt.tmp").write_text("stale")
    atomicio.atomic_write_text(target, "fresh")
    assert target.read_text() == "fresh"


def test_write_text_cleans_tmp_on_replace_failure(tmp_path, monkeypatch):
    target = tmp_path / "x.txt"

    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomicio.atomic_write_text(target, "data")
    # No temp file may survive a failed write.
    assert list(tmp_path.iterdir()) == []
    assert not target.exists()


# ─── atomic_write_symlink ───────────────────────────────────────────────────


def test_write_symlink_points_at_target(tmp_path):
    target = tmp_path / "run-dir"
    target.mkdir()
    link = tmp_path / "latest"
    atomicio.atomic_write_symlink(link, target)
    assert link.is_symlink()
    assert link.resolve() == target.resolve()


def test_write_symlink_target_is_relative(tmp_path):
    target = tmp_path / "run-dir"
    target.mkdir()
    link = tmp_path / "latest"
    atomicio.atomic_write_symlink(link, target)
    assert os.readlink(link) == "run-dir"           # relative, relocatable


def test_write_symlink_overwrites_existing(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    link = tmp_path / "latest"
    atomicio.atomic_write_symlink(link, a)
    atomicio.atomic_write_symlink(link, b)
    assert link.resolve() == b.resolve()


def test_write_symlink_uses_unique_tmp_per_call(tmp_path, monkeypatch):
    seen: list[Path] = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(Path(src))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    link = tmp_path / "latest"
    atomicio.atomic_write_symlink(link, a)
    atomicio.atomic_write_symlink(link, b)
    assert seen[0] != seen[1]
    assert seen[0].parent == link.parent


def test_write_symlink_survives_stale_fixed_tmp(tmp_path):
    target = tmp_path / "run-dir"
    target.mkdir()
    link = tmp_path / "latest"
    (tmp_path / "latest.tmp").symlink_to(tmp_path / "nowhere")  # stale fixed-name tmp
    atomicio.atomic_write_symlink(link, target)
    assert link.resolve() == target.resolve()


def test_write_symlink_cleans_tmp_on_symlink_failure(tmp_path, monkeypatch):
    target = tmp_path / "run-dir"
    target.mkdir()
    link = tmp_path / "latest"

    def boom(src, dst):
        raise OSError("symlink failed")

    monkeypatch.setattr(os, "symlink", boom)
    with pytest.raises(OSError):
        atomicio.atomic_write_symlink(link, target)
    assert not link.exists()
    # no temp leftover (only the target dir remains)
    assert list(tmp_path.iterdir()) == [target]
