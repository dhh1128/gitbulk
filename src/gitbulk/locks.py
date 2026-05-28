"""POSIX advisory locks for gitbulk's concurrency model.

Two context managers: ``global_lock`` (shared or exclusive at
``cache_dir()/run.lock``) and ``repo_lock`` (always exclusive at
``locks_dir()/<slug>.lock``). See this.i nodes ``lj5pqn4kr`` (why
two locks) and ``hk5pq3nm`` (the API contract).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal

from gitbulk import paths

_log = logging.getLogger("gitbulk.locks")

_MODE_TO_OP = {
    "shared": fcntl.LOCK_SH,
    "exclusive": fcntl.LOCK_EX,
}

_POLL_INTERVAL = 0.1  # seconds; how often we retry while waiting under a timeout


class LockTimeoutError(TimeoutError):
    """Raised when a timeout-bounded lock acquisition fails to acquire in time.

    ``holder`` is the JSON metadata the previous holder wrote to the lock
    file (may be ``None`` if metadata is unreadable). When ``holder`` is
    a dict, it carries an ``alive`` key indicating whether the recorded
    pid still exists on the system; the error message reflects this so
    the 2 a.m. operator does not chase a pid that no longer runs.
    """

    def __init__(self, lock_path: Path, holder: dict | None) -> None:
        self.lock_path = lock_path
        self.holder = holder
        if holder:
            pid = holder.get("pid")
            alive = holder.get("alive")
            if alive is True:
                liveness = f"pid {pid} (running)"
            elif alive is False:
                liveness = f"pid {pid} (no longer running — stale lock metadata)"
            else:
                liveness = f"pid {pid}"
            msg = (
                f"timed out waiting for lock at {lock_path}; "
                f"held by {liveness} "
                f"since {holder.get('started_at')} "
                f"running {holder.get('subcommand') or '<unknown>'}"
            )
        else:
            msg = f"timed out waiting for lock at {lock_path} (no holder metadata)"
        super().__init__(msg)


def _is_pid_alive(pid: int) -> bool:
    """Return True if a process with this pid currently exists on the system.

    Uses signal 0 (the standard portable liveness probe). Treats
    PermissionError as "alive" because the process exists but is owned
    by another user; treats any other OSError as "not alive" defensively.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_holder_metadata(path: Path) -> dict | None:
    try:
        text = path.read_text()
    except OSError:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    pid = parsed.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool):
        parsed["alive"] = _is_pid_alive(pid)
    return parsed


def _acquire(fd: int, lock_op: int, lock_path: Path, timeout: float | None) -> None:
    if timeout is None:
        fcntl.flock(fd, lock_op)
        return
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, lock_op | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LockTimeoutError(lock_path, _read_holder_metadata(lock_path))
            time.sleep(min(_POLL_INTERVAL, remaining))


def _write_metadata(fd: int, subcommand: str | None) -> None:
    metadata = {
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "subcommand": subcommand,
    }
    payload = json.dumps(metadata).encode()
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, payload)


@contextmanager
def _file_lock(
    path: Path,
    mode: str,
    *,
    timeout: float | None,
    subcommand: str | None,
) -> Iterator[None]:
    if mode not in _MODE_TO_OP:
        raise ValueError(
            f"invalid mode {mode!r}; expected 'shared' or 'exclusive'"
        )
    lock_op = _MODE_TO_OP[mode]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        _log.debug("acquiring %s lock at %s (timeout=%s)", mode, path, timeout)
        _acquire(fd, lock_op, path, timeout)
        _write_metadata(fd, subcommand)
        _log.debug("acquired lock at %s", path)
        yield
    finally:
        os.close(fd)
        _log.debug("released lock at %s", path)


@contextmanager
def global_lock(
    mode: Literal["shared", "exclusive"],
    *,
    timeout: float | None = None,
    subcommand: str | None = None,
) -> Iterator[None]:
    """Hold the global run.lock for the duration of the ``with`` block."""
    with _file_lock(
        paths.global_lock_file(), mode, timeout=timeout, subcommand=subcommand
    ):
        yield


@contextmanager
def repo_lock(
    slug: str,
    *,
    timeout: float | None = None,
    subcommand: str | None = None,
) -> Iterator[None]:
    """Hold a per-repo exclusive lock for the duration of the ``with`` block."""
    with _file_lock(
        paths.repo_lock_file(slug),
        "exclusive",
        timeout=timeout,
        subcommand=subcommand,
    ):
        yield
