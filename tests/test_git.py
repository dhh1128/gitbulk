"""Tests for the central pinned ``git`` resolver (SEC-F3).

Mirrors the bare-name branches of :func:`gitbulk.agent._pin_binary`'s tests in
``test_agent.py``: a :func:`shutil.which` hit pins the absolute path; a miss
falls back to the bare ``"git"``. Coverage here must not depend on the host
actually having ``git`` — ``shutil.which`` is monkeypatched in both directions.
"""

from __future__ import annotations

import shutil

import gitbulk.git as gitmod
from gitbulk.git import GIT, resolve_git


def test_resolve_git_uses_which_absolute(monkeypatch):
    """A which hit pins the absolute path it returns (PATH-hijack proof)."""
    monkeypatch.setattr(shutil, "which", lambda name: f"/canonical/{name}")
    assert resolve_git() == "/canonical/git"


def test_resolve_git_unresolved_falls_back_to_bare(monkeypatch):
    """A which miss falls back to the bare name so an absent binary surfaces as
    a normal launch failure rather than an import-time abort."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert resolve_git() == "git"


def test_resolve_git_resolves_git_specifically(monkeypatch):
    """resolve_git always asks ``which`` for ``git`` (not some other name)."""
    seen: list[str] = []

    def fake_which(name):
        seen.append(name)
        return f"/usr/bin/{name}"

    monkeypatch.setattr(shutil, "which", fake_which)
    assert resolve_git() == "/usr/bin/git"
    assert seen == ["git"]


def test_module_level_git_is_resolve_git_value():
    """The cached :data:`GIT` is what :func:`resolve_git` produced at import."""
    assert GIT == gitmod.GIT
    # Either an absolute path (which hit at import) or the bare fallback.
    assert GIT == "git" or GIT.endswith("/git")
