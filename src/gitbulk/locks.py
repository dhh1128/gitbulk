"""POSIX advisory locks for gitbulk's concurrency model.

Resource-scoped locking (node ``rsclk7nq``, superseding the two-lock model of
``lj5pqn4kr``): one keyed ``fcntl.flock`` per shared resource, each acquired
only around the section that touches it. Context managers:

  - ``run_state_lock(target, mode)`` — per-subcommand run-state (resource #1)
  - ``repo_lock(slug, mode)``        — per-repo clone + remote (resources #6-8)
  - ``org_lock(org)``                — org-members cache (resource #2)
  - ``default_branches_lock()``      — default-branches cache (resource #3)
  - ``sentinel_lock()``              — ATTENTION sentinel (resource #4)
  - ``dashboard_lock()``             — dashboard.md (resource #5)
  - ``watchdog_ack_lock()``          — watchdog-ack cache (resource #9)

The legacy single ``global_lock`` of the two-lock model (``lj5pqn4kr``) has been
retired — every subcommand now takes only the resource locks it needs.

To avoid deadlock, never hold two at once (the design is flat/non-nested); if
nesting is ever unavoidable, acquire in this order: org -> default_branches ->
repo(slug) -> run_state(sub) -> sentinel -> dashboard. See ``hk5pq3nm`` for the
underlying ``_file_lock`` contract (timeout, holder metadata, pid-liveness).
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
def repo_lock(
    slug: str,
    mode: Literal["shared", "exclusive"] = "exclusive",
    *,
    timeout: float | None = None,
    subcommand: str | None = None,
) -> Iterator[None]:
    """Hold a per-repo lock (clone + remote) for the ``with`` block.

    Resource #6/#7/#8 of node ``rsclk7nq``: serializes all gitbulk work on one
    repository. ``mode`` defaults to ``"exclusive"`` (mutating git or any
    remote mutation); pass ``"shared"`` for read-only git (clone preflights).
    The previously always-exclusive contract of ``hk5pq3nm.b`` is relaxed here.
    """
    with _file_lock(
        paths.repo_lock_file(slug),
        mode,
        timeout=timeout,
        subcommand=subcommand,
    ):
        yield


@contextmanager
def run_state_lock(
    target: str,
    mode: Literal["shared", "exclusive"],
    *,
    timeout: float | None = None,
    subcommand: str | None = None,
) -> Iterator[None]:
    """Hold the run-state lock for subcommand ``target`` (resource #1).

    Guards the ``latest-<target>`` symlink swap and ``gc.prune_runs(target)``
    against readers. Writers (a run of ``target`` finishing) take exclusive;
    ``show``/dashboard reads take shared. Keyed by ``target`` (the run-state
    subcommand), which is independent of ``subcommand`` (the command actually
    running — e.g. ``show`` reads run-state ``prune-worktrees``).
    """
    with _file_lock(
        paths.named_lock_file(f"runstate-{target}"),
        mode,
        timeout=timeout,
        subcommand=subcommand,
    ):
        yield


@contextmanager
def org_lock(
    org: str,
    *,
    timeout: float | None = None,
    subcommand: str | None = None,
) -> Iterator[None]:
    """Hold the org-members-cache lock for ``org`` (resource #2, exclusive)."""
    with _file_lock(
        paths.named_lock_file(f"org-{org}"),
        "exclusive",
        timeout=timeout,
        subcommand=subcommand,
    ):
        yield


@contextmanager
def default_branches_lock(
    *,
    timeout: float | None = None,
    subcommand: str | None = None,
) -> Iterator[None]:
    """Hold the default-branches-cache lock (resource #3, exclusive)."""
    with _file_lock(
        paths.named_lock_file("default-branches"),
        "exclusive",
        timeout=timeout,
        subcommand=subcommand,
    ):
        yield


@contextmanager
def sentinel_lock(
    *,
    timeout: float | None = None,
    subcommand: str | None = None,
) -> Iterator[None]:
    """Hold the ATTENTION-sentinel lock (resource #4, exclusive)."""
    with _file_lock(
        paths.named_lock_file("attention"),
        "exclusive",
        timeout=timeout,
        subcommand=subcommand,
    ):
        yield


@contextmanager
def dashboard_lock(
    *,
    timeout: float | None = None,
    subcommand: str | None = None,
) -> Iterator[None]:
    """Hold the dashboard.md lock (resource #5, exclusive)."""
    with _file_lock(
        paths.named_lock_file("dashboard"),
        "exclusive",
        timeout=timeout,
        subcommand=subcommand,
    ):
        yield


@contextmanager
def watchdog_ack_lock(
    *,
    timeout: float | None = None,
    subcommand: str | None = None,
) -> Iterator[None]:
    """Hold the watchdog-ack-cache lock (resource #9, exclusive).

    Guards the load->modify->save in ``watchdog_ack.record_ack`` against the
    cross-process lost-update window (the write itself is already atomic).
    """
    with _file_lock(
        paths.named_lock_file("watchdog-acked"),
        "exclusive",
        timeout=timeout,
        subcommand=subcommand,
    ):
        yield
