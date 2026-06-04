"""Tests for gitbulk.sandbox (bubblewrap wrapping + capability probe).

Hermetic: ``shutil.which`` / ``subprocess.run`` are mocked; no real ``bwrap``
is spawned (AGENTS.md 'no network in tests'). The argv-composition tests assert
the security-relevant shape, not a real namespace.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from gitbulk import sandbox
from gitbulk.sandbox import (
    SANDBOX_FS_NO_NET,
    SANDBOX_FS_ONLY,
    SANDBOX_NONE,
    bwrap_available,
    wrap_argv,
)


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    bwrap_available.cache_clear()
    yield
    bwrap_available.cache_clear()


class _Completed:
    def __init__(self, returncode):
        self.returncode = returncode


# ─── bwrap_available probe ──────────────────────────────────────────────────


def test_probe_false_when_bwrap_absent():
    with patch("gitbulk.sandbox.shutil.which", return_value=None):
        assert bwrap_available() is False


def test_probe_true_when_bwrap_runs():
    with patch("gitbulk.sandbox.shutil.which", return_value="/usr/bin/bwrap"), patch(
        "gitbulk.sandbox.subprocess.run", return_value=_Completed(0)
    ):
        assert bwrap_available() is True


def test_probe_false_when_userns_disabled():
    """bwrap present but the trivial run fails (e.g. userns disabled)."""
    with patch("gitbulk.sandbox.shutil.which", return_value="/usr/bin/bwrap"), patch(
        "gitbulk.sandbox.subprocess.run", return_value=_Completed(1)
    ):
        assert bwrap_available() is False


def test_probe_false_when_run_raises():
    with patch("gitbulk.sandbox.shutil.which", return_value="/usr/bin/bwrap"), patch(
        "gitbulk.sandbox.subprocess.run", side_effect=OSError("boom")
    ):
        assert bwrap_available() is False


# ─── wrap_argv ──────────────────────────────────────────────────────────────


def test_wrap_none_returns_argv_unchanged():
    argv = ["/canonical/tool", "-p", "x"]
    assert wrap_argv(argv, worktree=Path("/wt"), policy=SANDBOX_NONE) is argv


def _wrapped(policy, worktree="/tmp/gitbulk/wt"):
    with patch("gitbulk.sandbox.shutil.which", return_value="/usr/bin/bwrap"), patch(
        "gitbulk.sandbox.Path.exists", return_value=True
    ):
        return wrap_argv(
            ["/canonical/agent", "-p", "PROMPT"],
            worktree=Path(worktree),
            policy=policy,
        )


def test_wrap_fs_only_shape():
    wt = "/tmp/gitbulk/wt"
    argv = _wrapped(SANDBOX_FS_ONLY, wt)
    assert argv[0] == "/usr/bin/bwrap"
    assert "--die-with-parent" in argv
    assert "--unshare-user" in argv
    # fs-only keeps the network.
    assert "--unshare-net" not in argv
    # The worktree is the one writable real path, and cwd is set there.
    assert "--bind" in argv
    assert wt in argv
    assert argv[argv.index("--chdir") + 1] == wt
    # The agent argv follows the `--` separator, intact.
    sep = argv.index("--")
    assert argv[sep + 1:] == ["/canonical/agent", "-p", "PROMPT"]


def test_wrap_fs_no_net_unshares_network():
    argv = _wrapped(SANDBOX_FS_NO_NET)
    assert "--unshare-net" in argv


def test_wrap_does_not_bind_credentials_or_home():
    """The agent must not be able to read ~/.ssh / ~/.aws / ~/.config/gh:
    $HOME is tmpfs-shadowed and never bound (threat-model T1)."""
    argv = _wrapped(SANDBOX_FS_NO_NET)
    home = str(Path.home())
    # $HOME is shadowed by a tmpfs (the token right before it is --tmpfs),
    # never --bind'd.
    assert home in argv
    assert argv[argv.index(home) - 1] == "--tmpfs"
    for secret in (f"{home}/.ssh", f"{home}/.aws", f"{home}/.config/gh"):
        assert secret not in argv


def test_wrap_only_binds_existing_system_dirs():
    """Non-existent system dirs are skipped (no bind for a missing /opt)."""
    def fake_exists(self):
        return str(self) != "/opt"

    with patch("gitbulk.sandbox.shutil.which", return_value="/usr/bin/bwrap"), patch(
        "gitbulk.sandbox.Path.exists", fake_exists
    ):
        argv = wrap_argv(
            ["/canonical/agent"], worktree=Path("/wt"), policy=SANDBOX_FS_ONLY
        )
    assert "/opt" not in argv
    assert "/usr" in argv
