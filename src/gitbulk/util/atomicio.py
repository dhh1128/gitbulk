"""Atomic filesystem writes with unique temp names.

See this.i node ``rsclk7nq`` (resource-scoped locking), Phase 0 hardening.

Every writer that previously used a fixed ``<name>.tmp`` sidecar shared that
temp path with any concurrent writer of the same target: writer A creates the
tmp, B overwrites it, A renames it onto the target, then B's ``os.replace``
finds the tmp gone and raises ``ENOENT``. The single global lock used to mask
this; resource-scoped locking removes that umbrella, so the temp name must be
unique per call. ``tempfile.mkstemp`` in the *target's own directory* gives a
collision-free name and keeps the rename on one filesystem (so ``os.replace``
is atomic on POSIX).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _unique_tmp(target: Path) -> Path:
    """Create and return a unique, empty temp file beside ``target``."""
    fd, name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(fd)
    return Path(name)


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` atomically (unique tmp + ``os.replace``).

    The parent directory must already exist (callers own directory creation,
    matching the pre-existing behaviour of the writers this replaces).
    """
    tmp = _unique_tmp(path)
    try:
        tmp.write_text(text, encoding=encoding)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def atomic_write_symlink(symlink_path: Path, target: Path) -> None:
    """Create/replace ``symlink_path`` -> ``target`` atomically.

    The link target is stored RELATIVE to the symlink's parent so the cache
    tree can be relocated without breaking symlinks (per kp7nw4mq.e).
    """
    relative_target = os.path.relpath(target, start=symlink_path.parent)
    tmp = _unique_tmp(symlink_path)
    # mkstemp made a regular file; drop it so the unique name is free for a
    # symlink. The name is unique, so no other process competes for it.
    tmp.unlink()
    try:
        os.symlink(relative_target, tmp)
        os.replace(tmp, symlink_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


__all__ = ["atomic_write_text", "atomic_write_symlink"]
