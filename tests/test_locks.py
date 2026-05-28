"""Tests for locks.py (this.i node hk5pq3nm)."""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gitbulk import locks, paths
from gitbulk.locks import LockTimeoutError


@pytest.fixture
def isolated_cache(monkeypatch, tmp_path):
    """Point gitbulk's cache at tmp_path and ensure dirs exist."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    paths.ensure_directories()
    return tmp_path


# ─── Mode argument handling ────────────────────────────────────────────────


def test_global_lock_shared_calls_flock_with_LOCK_SH(isolated_cache, monkeypatch):
    captured: list = []

    def fake_flock(fd, op):
        captured.append(op)

    monkeypatch.setattr(fcntl, "flock", fake_flock)
    with locks.global_lock("shared"):
        pass
    assert captured == [fcntl.LOCK_SH]


def test_global_lock_exclusive_calls_flock_with_LOCK_EX(isolated_cache, monkeypatch):
    captured: list = []

    def fake_flock(fd, op):
        captured.append(op)

    monkeypatch.setattr(fcntl, "flock", fake_flock)
    with locks.global_lock("exclusive"):
        pass
    assert captured == [fcntl.LOCK_EX]


def test_invalid_mode_raises_value_error(isolated_cache):
    with pytest.raises(ValueError, match="invalid mode"):
        with locks.global_lock("write-only"):  # type: ignore[arg-type]
            pass


def test_repo_lock_calls_flock_with_LOCK_EX(isolated_cache, monkeypatch):
    captured: list = []

    def fake_flock(fd, op):
        captured.append(op)

    monkeypatch.setattr(fcntl, "flock", fake_flock)
    with locks.repo_lock("owner/repo"):
        pass
    assert captured == [fcntl.LOCK_EX]


def test_repo_lock_malformed_slug_raises(isolated_cache):
    with pytest.raises(ValueError, match="malformed slug"):
        with locks.repo_lock("no-slash"):
            pass


# ─── Lock file path correctness ─────────────────────────────────────────────


def test_global_lock_writes_to_global_lock_file(isolated_cache):
    with locks.global_lock("exclusive", subcommand="report"):
        assert paths.global_lock_file().exists()
        metadata = json.loads(paths.global_lock_file().read_text())
        assert metadata["pid"] == os.getpid()
        assert metadata["subcommand"] == "report"


def test_repo_lock_writes_to_repo_lock_file(isolated_cache):
    with locks.repo_lock("dhh1128/gitbulk", subcommand="merge"):
        lock_path = paths.repo_lock_file("dhh1128/gitbulk")
        assert lock_path.exists()
        metadata = json.loads(lock_path.read_text())
        assert metadata["subcommand"] == "merge"


# ─── Metadata contents ─────────────────────────────────────────────────────


def test_metadata_includes_pid_and_started_at_utc(isolated_cache):
    with locks.global_lock("exclusive", subcommand="dispatch"):
        metadata = json.loads(paths.global_lock_file().read_text())
        assert metadata["pid"] == os.getpid()
        assert metadata["started_at"].endswith("+00:00") or metadata["started_at"].endswith("Z")
        assert metadata["subcommand"] == "dispatch"


def test_metadata_subcommand_null_when_omitted(isolated_cache):
    with locks.global_lock("shared"):
        metadata = json.loads(paths.global_lock_file().read_text())
        assert metadata["subcommand"] is None


# ─── Lock file persistence ─────────────────────────────────────────────────


def test_lock_file_persists_after_release(isolated_cache):
    with locks.global_lock("shared"):
        pass
    # The file is unlocked (we exited the context) but the file remains on disk
    assert paths.global_lock_file().exists()


# ─── Timeout / blocking behavior ───────────────────────────────────────────


def test_no_timeout_omits_LOCK_NB(isolated_cache, monkeypatch):
    """Default block-forever path: flock called WITHOUT LOCK_NB."""
    captured: list = []

    def fake_flock(fd, op):
        captured.append(op)

    monkeypatch.setattr(fcntl, "flock", fake_flock)
    with locks.global_lock("shared"):
        pass
    assert captured == [fcntl.LOCK_SH]  # no LOCK_NB bit set
    assert not (captured[0] & fcntl.LOCK_NB)


def test_timeout_uses_LOCK_NB(isolated_cache, monkeypatch):
    captured: list = []

    def fake_flock(fd, op):
        captured.append(op)

    monkeypatch.setattr(fcntl, "flock", fake_flock)
    with locks.global_lock("shared", timeout=1.0):
        pass
    assert len(captured) == 1
    assert captured[0] & fcntl.LOCK_NB


def test_timeout_succeeds_after_retry(isolated_cache, monkeypatch):
    """First flock call raises BlockingIOError; second succeeds. Exercises the retry loop."""
    mock_flock = MagicMock(side_effect=[BlockingIOError("contention"), None])
    monkeypatch.setattr(fcntl, "flock", mock_flock)
    with locks.global_lock("shared", timeout=2.0):
        pass
    assert mock_flock.call_count == 2


def test_timeout_expires_raises_LockTimeoutError(isolated_cache):
    """Hold the lock from a second FD in this process and try to re-acquire with timeout."""
    fd_holder = os.open(paths.global_lock_file(), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd_holder, fcntl.LOCK_EX)
    try:
        # Pre-write some metadata so the error path has a holder dict to find
        os.lseek(fd_holder, 0, os.SEEK_SET)
        os.ftruncate(fd_holder, 0)
        os.write(
            fd_holder,
            json.dumps(
                {
                    "pid": 99999,
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "subcommand": "report",
                }
            ).encode(),
        )
        with pytest.raises(LockTimeoutError) as exc_info:
            with locks.global_lock("exclusive", timeout=0.2):
                pass
        err = exc_info.value
        assert err.holder is not None
        assert err.holder["pid"] == 99999
        assert err.holder["subcommand"] == "report"
        assert "99999" in str(err)
        assert "report" in str(err)
        assert err.lock_path == paths.global_lock_file()
    finally:
        os.close(fd_holder)


def test_timeout_error_message_when_no_holder_metadata(isolated_cache):
    """Lock file exists but is empty (no JSON to read)."""
    paths.global_lock_file().write_text("")
    fd_holder = os.open(paths.global_lock_file(), os.O_RDWR)
    fcntl.flock(fd_holder, fcntl.LOCK_EX)
    try:
        with pytest.raises(LockTimeoutError) as exc_info:
            with locks.global_lock("exclusive", timeout=0.2):
                pass
        assert exc_info.value.holder is None
        assert "no holder metadata" in str(exc_info.value)
    finally:
        os.close(fd_holder)


# ─── _read_holder_metadata branches ────────────────────────────────────────


def test_read_holder_metadata_missing_file_returns_none(tmp_path):
    assert locks._read_holder_metadata(tmp_path / "nonexistent") is None


def test_read_holder_metadata_malformed_json_returns_none(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("this is not json {{{")
    assert locks._read_holder_metadata(path) is None


def test_read_holder_metadata_non_dict_returns_none(tmp_path):
    path = tmp_path / "arr.json"
    path.write_text('["arrays", "are", "not", "dicts"]')
    assert locks._read_holder_metadata(path) is None


def test_read_holder_metadata_happy_path(tmp_path):
    path = tmp_path / "good.json"
    # Use a pid past Linux's default PID_MAX (32768) so the liveness check
    # is host-independent. Low pids like 42 are real kthreads on GitHub-
    # hosted Ubuntu runners but absent on WSL2 dev boxes — using one would
    # make the test pass locally and fail in CI (and did, 2026-05-28).
    bogus_pid = 2**22
    payload = {
        "pid": bogus_pid,
        "started_at": "2026-01-01T00:00:00+00:00",
        "subcommand": "x",
    }
    path.write_text(json.dumps(payload))
    result = locks._read_holder_metadata(path)
    assert result is not None
    assert result["pid"] == bogus_pid
    assert result["started_at"] == payload["started_at"]
    assert result["subcommand"] == payload["subcommand"]
    assert result["alive"] is False


def test_read_holder_metadata_marks_current_pid_alive(tmp_path):
    path = tmp_path / "current.json"
    payload = {
        "pid": os.getpid(),
        "started_at": "2026-01-01T00:00:00+00:00",
        "subcommand": "x",
    }
    path.write_text(json.dumps(payload))
    result = locks._read_holder_metadata(path)
    assert result is not None
    assert result["alive"] is True


def test_read_holder_metadata_no_alive_field_when_pid_missing(tmp_path):
    path = tmp_path / "nopid.json"
    payload = {"started_at": "2026-01-01T00:00:00+00:00", "subcommand": "x"}
    path.write_text(json.dumps(payload))
    result = locks._read_holder_metadata(path)
    assert result is not None
    # No pid in the file → no alive field added.
    assert "alive" not in result


def test_read_holder_metadata_no_alive_field_when_pid_is_bool(tmp_path):
    """JSON's True/False parses as Python bool which is a subclass of int.
    We deliberately do not call _is_pid_alive on bools."""
    path = tmp_path / "boolpid.json"
    payload = {
        "pid": True,
        "started_at": "2026-01-01T00:00:00+00:00",
        "subcommand": "x",
    }
    path.write_text(json.dumps(payload))
    result = locks._read_holder_metadata(path)
    assert result is not None
    assert "alive" not in result


# ─── _is_pid_alive directly ────────────────────────────────────────────────


def test_is_pid_alive_for_current_process():
    assert locks._is_pid_alive(os.getpid()) is True


def test_is_pid_alive_for_nonexistent_pid():
    # PID 2**22 is well beyond any plausible system pid maximum on Linux.
    assert locks._is_pid_alive(2**22) is False


def test_is_pid_alive_permission_error_means_alive(monkeypatch):
    """A PermissionError on os.kill(pid, 0) means the process exists but
    is owned by another user — definitely alive from our point of view."""

    def fake_kill(pid, sig):
        raise PermissionError("not your process")

    monkeypatch.setattr(os, "kill", fake_kill)
    assert locks._is_pid_alive(1) is True


def test_is_pid_alive_generic_oserror_treated_as_dead(monkeypatch):
    """Defensive: an unexpected OSError from os.kill should not crash the
    lock-error path. Treat as 'not alive' rather than propagating."""

    def fake_kill(pid, sig):
        raise OSError("something unusual")

    monkeypatch.setattr(os, "kill", fake_kill)
    assert locks._is_pid_alive(1) is False


# ─── LockTimeoutError message reflects alive status ────────────────────────


def test_locktimeout_message_shows_alive_when_holder_present(isolated_cache):
    # Use this process's own pid so the holder shows as alive.
    fd_holder = os.open(paths.global_lock_file(), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd_holder, fcntl.LOCK_EX)
    try:
        # Pre-write metadata referencing our own pid.
        os.lseek(fd_holder, 0, os.SEEK_SET)
        os.ftruncate(fd_holder, 0)
        os.write(
            fd_holder,
            json.dumps(
                {
                    "pid": os.getpid(),
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "subcommand": "report",
                }
            ).encode(),
        )
        with pytest.raises(LockTimeoutError) as exc_info:
            with locks.global_lock("exclusive", timeout=0.2):
                pass
        err = exc_info.value
        assert err.holder is not None
        assert err.holder["alive"] is True
        assert "(running)" in str(err)
    finally:
        os.close(fd_holder)


def test_locktimeout_message_no_alive_field(tmp_path):
    """If holder metadata exists but carries no alive field (e.g., metadata
    without a pid), the message uses the plain `pid X` form rather than
    `(running)` or `(no longer running)`."""
    err = LockTimeoutError(
        tmp_path / "fake.lock",
        {
            "pid": 12345,
            "started_at": "2026-01-01T00:00:00+00:00",
            "subcommand": "report",
        },
    )
    text = str(err)
    assert "pid 12345" in text
    assert "(running)" not in text
    assert "(no longer running" not in text


def test_locktimeout_message_shows_stale_when_holder_dead(isolated_cache):
    """When the recorded pid is gone, the message must say so clearly so the
    operator does not waste time chasing a nonexistent process."""
    fd_holder = os.open(paths.global_lock_file(), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd_holder, fcntl.LOCK_EX)
    try:
        # Pre-write metadata referencing a definitely-dead pid.
        os.lseek(fd_holder, 0, os.SEEK_SET)
        os.ftruncate(fd_holder, 0)
        os.write(
            fd_holder,
            json.dumps(
                {
                    "pid": 2**22,
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "subcommand": "report",
                }
            ).encode(),
        )
        with pytest.raises(LockTimeoutError) as exc_info:
            with locks.global_lock("exclusive", timeout=0.2):
                pass
        err = exc_info.value
        assert err.holder is not None
        assert err.holder["alive"] is False
        assert "no longer running" in str(err)
        assert "stale lock metadata" in str(err)
    finally:
        os.close(fd_holder)


# ─── Actual contention tests using a second FD in this process ─────────────


def test_exclusive_blocks_concurrent_exclusive_attempt(isolated_cache):
    """Real fcntl behavior: when one FD holds EX, another EX with LOCK_NB fails."""
    fd_holder = os.open(paths.global_lock_file(), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd_holder, fcntl.LOCK_EX)
    try:
        with pytest.raises(LockTimeoutError):
            with locks.global_lock("exclusive", timeout=0.15):
                pass
    finally:
        os.close(fd_holder)


def test_shared_coexists_with_shared(isolated_cache):
    """Real fcntl behavior: two shared locks on the same file coexist."""
    fd_holder = os.open(paths.global_lock_file(), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd_holder, fcntl.LOCK_SH)
    try:
        # This must NOT block; timeout=0.5 is generous in case scheduling is slow
        with locks.global_lock("shared", timeout=0.5):
            pass
    finally:
        os.close(fd_holder)


def test_repo_locks_for_different_slugs_dont_contend(isolated_cache):
    """A per-repo lock on owner1/repo1 must not block one on owner2/repo2."""
    fd_holder = os.open(paths.repo_lock_file("owner1/repo1"), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd_holder, fcntl.LOCK_EX)
    try:
        with locks.repo_lock("owner2/repo2", timeout=0.5):
            pass
    finally:
        os.close(fd_holder)


# ─── Timeout-bound acquire that succeeds when the holder releases ──────────


def test_acquire_succeeds_when_holder_releases_mid_wait(isolated_cache):
    """A separate thread releases the lock partway through our wait."""
    fd_holder = os.open(paths.global_lock_file(), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd_holder, fcntl.LOCK_EX)
    released = threading.Event()

    def release_after_delay() -> None:
        time.sleep(0.15)
        fcntl.flock(fd_holder, fcntl.LOCK_UN)
        released.set()

    t = threading.Thread(target=release_after_delay)
    t.start()
    try:
        with locks.global_lock("exclusive", timeout=2.0):
            assert released.wait(timeout=1.0)
    finally:
        t.join(timeout=2.0)
        os.close(fd_holder)
