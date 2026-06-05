"""Shared pytest fixtures for the hermetic test suite.

These three fixtures (``isolated_xdg``, ``code_root``, ``fresh_org_cache``)
were previously copy-pasted, verbatim-but-drifting, into every command test
module (test_merge, test_prune_branches, test_prune_worktrees,
test_close_stale, test_dispatch). Each copy was functionally identical, so
they are lifted here once; pytest auto-discovers conftest fixtures, so no
imports are needed in the consuming modules (TST-F2).

Note: ``write_config`` is intentionally NOT hoisted. Its copies have genuine
per-command requirements that are not safe to merge — different policy
defaults (``min_business_days`` for merge, ``stale_age_days`` for
close-stale, ``prune_min_age_days`` for the prune commands), different keyword
signatures (``bots=`` / ``defaults_extra=`` / ``with_org=`` / ``extra=``), and
prune-worktrees materializes REAL git repos with an ``origin`` remote rather
than empty directories. Each module keeps its own ``write_config`` as a local
fixture; a same-named fixture in a module shadows a conftest one, so there is
no collision risk if a shared version is ever added later.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from gitbulk import paths
from gitbulk.org_members_cache import CachedMembers, save_cache


@pytest.fixture
def isolated_xdg(monkeypatch, tmp_path):
    """Point the XDG config/cache roots at a fresh tmp dir and create the
    gitbulk directory tree, so every test gets an isolated, empty config."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    paths.ensure_directories()
    return tmp_path


@pytest.fixture
def code_root(tmp_path):
    """A ``code/`` dir under tmp_path to act as the local clone root."""
    root = tmp_path / "code"
    root.mkdir()
    return root


@pytest.fixture
def fresh_org_cache():
    """Factory that writes a fresh (non-expired) org-members cache entry."""

    def _save(org, members):
        save_cache(
            CachedMembers(
                org=org,
                fetched_at=datetime.now(timezone.utc),
                members=frozenset(members),
            )
        )

    return _save
