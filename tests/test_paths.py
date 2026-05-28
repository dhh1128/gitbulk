"""Tests for paths.py — XDG-aware location helpers (this.i node 3pw7qkn2)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gitbulk import paths


@pytest.fixture
def isolated_paths(monkeypatch, tmp_path):
    """Point gitbulk at tmp_path so tests never touch real ~/.config or ~/.cache."""
    config_root = tmp_path / "config-root"
    cache_root = tmp_path / "cache-root"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_root))
    return config_root, cache_root


# ─── XDG resolution ─────────────────────────────────────────────────────────


def test_config_dir_honors_xdg(isolated_paths):
    config_root, _ = isolated_paths
    assert paths.config_dir() == config_root / "gitbulk"


def test_config_dir_default_when_xdg_unset(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert paths.config_dir() == Path.home() / ".config" / "gitbulk"


def test_config_dir_default_when_xdg_empty(monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    assert paths.config_dir() == Path.home() / ".config" / "gitbulk"


def test_cache_dir_honors_xdg(isolated_paths):
    _, cache_root = isolated_paths
    assert paths.cache_dir() == cache_root / "gitbulk"


def test_cache_dir_default_when_xdg_unset(monkeypatch):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    assert paths.cache_dir() == Path.home() / ".cache" / "gitbulk"


def test_cache_dir_default_when_xdg_empty(monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", "")
    assert paths.cache_dir() == Path.home() / ".cache" / "gitbulk"


# ─── Singleton file paths ───────────────────────────────────────────────────


def test_singleton_files(isolated_paths):
    config_root, cache_root = isolated_paths
    gconfig = config_root / "gitbulk"
    gcache = cache_root / "gitbulk"
    assert paths.repos_file() == gconfig / "repos.txt"
    assert paths.policy_file() == gconfig / "gitbulk.yaml"
    assert paths.global_lock_file() == gcache / "run.lock"
    assert paths.attention_sentinel() == gcache / "ATTENTION"
    assert paths.dashboard_file() == gcache / "dashboard.md"


# ─── Subdir paths ───────────────────────────────────────────────────────────


def test_runs_dir_and_locks_dir(isolated_paths):
    _, cache_root = isolated_paths
    gcache = cache_root / "gitbulk"
    assert paths.runs_dir() == gcache / "runs"
    assert paths.locks_dir() == gcache / "locks"
    assert paths.default_worktree_root() == gcache / "worktrees"
    assert paths.org_members_cache_dir() == gcache / "org-members"


def test_org_members_cache_file_path(isolated_paths):
    _, cache_root = isolated_paths
    gcache = cache_root / "gitbulk"
    assert (
        paths.org_members_cache_file("provenant-dev")
        == gcache / "org-members" / "provenant-dev.yaml"
    )


def test_run_dir_composition(isolated_paths):
    expected = paths.runs_dir() / "20260527T120000Z-report"
    assert paths.run_dir("20260527T120000Z", "report") == expected


def test_latest_run_symlink_path(isolated_paths):
    assert paths.latest_run_symlink("merge") == paths.runs_dir() / "latest-merge"


# ─── Slug normalization ─────────────────────────────────────────────────────


def test_repo_lock_file_slug_normalization(isolated_paths):
    assert paths.repo_lock_file("owner/repo") == paths.locks_dir() / "owner__repo.lock"


def test_findings_dir_slug_normalization(isolated_paths):
    _, cache_root = isolated_paths
    gcache = cache_root / "gitbulk"
    assert paths.findings_dir("owner/repo") == gcache / "findings" / "owner__repo"


def test_worktree_dir_default_root(isolated_paths):
    expected = paths.default_worktree_root() / "20260527T000000Z" / "owner__repo"
    assert paths.worktree_dir("20260527T000000Z", "owner/repo") == expected


def test_worktree_dir_custom_root(tmp_path, isolated_paths):
    custom = tmp_path / "custom-worktrees"
    expected = custom / "RID" / "owner__repo"
    assert paths.worktree_dir("RID", "owner/repo", root=custom) == expected


@pytest.mark.parametrize("bad_slug", ["no-slash", "a/b/c", "", "a/", "/b", "/"])
def test_malformed_slug_raises(bad_slug):
    with pytest.raises(ValueError, match="malformed slug"):
        paths.repo_lock_file(bad_slug)


# ─── ensure_directories ─────────────────────────────────────────────────────


def test_ensure_directories_creates_all(isolated_paths):
    paths.ensure_directories()
    assert paths.config_dir().is_dir()
    assert paths.cache_dir().is_dir()
    assert paths.runs_dir().is_dir()
    assert paths.locks_dir().is_dir()
    assert paths.default_worktree_root().is_dir()
    assert paths.org_members_cache_dir().is_dir()


def test_ensure_directories_idempotent(isolated_paths):
    paths.ensure_directories()
    paths.ensure_directories()  # second call must not raise
    assert paths.config_dir().is_dir()
    assert paths.cache_dir().is_dir()


# ─── new_runid ──────────────────────────────────────────────────────────────


def test_new_runid_format():
    rid = paths.new_runid()
    assert re.fullmatch(r"\d{8}T\d{6}Z", rid), f"unexpected format: {rid!r}"


def test_new_runid_sorts_chronologically():
    a = paths.new_runid(datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
    b = paths.new_runid(datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc))
    c = paths.new_runid(datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc))
    assert sorted([c, a, b]) == [a, b, c]


def test_new_runid_honors_when_arg():
    when = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)
    assert paths.new_runid(when) == "20260527T120000Z"


def test_new_runid_always_utc():
    # 12:00 in UTC-7 == 19:00 UTC
    when = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone(timedelta(hours=-7)))
    assert paths.new_runid(when) == "20260527T190000Z"


def test_new_runid_rejects_naive_datetime():
    with pytest.raises(ValueError, match="tzinfo"):
        paths.new_runid(datetime(2026, 5, 27, 12, 0, 0))  # no tzinfo


# ─── Memoization absence ────────────────────────────────────────────────────


def test_no_memoization(monkeypatch, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(first))
    assert paths.config_dir() == first / "gitbulk"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(second))
    assert paths.config_dir() == second / "gitbulk"
