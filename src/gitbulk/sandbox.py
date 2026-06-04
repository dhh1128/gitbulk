"""OS sandboxing for dispatched coding agents via bubblewrap (this.i ``agsbx3k``).

Defense-in-depth on top of least-privilege (``agpriv8n``) and env scoping
(``agenv6q``) — NOT the primary control. A non-claude backend can be run inside
an unprivileged ``bwrap`` user namespace so that, even if it is malicious or
prompt-injected, it cannot read the operator's credentials or other repos, and
(for the ``fs+no-net`` policy) cannot reach the network at all.

Policies (the profile ``sandbox:`` field):

  - ``none``      — no sandbox (default; today's behavior).
  - ``fs-only``   — ``$HOME`` shadowed, only the worktree bound read-write and a
    read-only system toolchain; network still available.
  - ``fs+no-net`` — ``fs-only`` plus ``--unshare-net`` (no network). Viable for
    tasks that need neither network nor credentials — which, thanks to
    ``agpriv8n``, includes resolve-conflicts.

Because the bind set is allow-list (only the worktree + a fixed set of system
dirs that exist), credential locations like ``~/.ssh`` / ``~/.aws`` /
``~/.config/gh`` and the other ~149 clones are simply never mounted.

Availability is capability-probed: ``bwrap`` must be installed AND unprivileged
user namespaces must work. If a profile requests a sandbox the host cannot
provide, the default is to **refuse to run** (``sandbox_fallback: refuse``)
rather than silently downgrade to unsandboxed — see ``backend_for``.
"""

from __future__ import annotations

import functools
import shutil
import subprocess
from pathlib import Path

#: Read-only system directories bound into the sandbox when they exist. These
#: carry the toolchain (git, the agent binary, shared libs) and CA certs — but
#: deliberately NOT ``$HOME`` or any credential location.
_RO_SYSTEM_DIRS: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/etc",
    "/opt",
)

SANDBOX_NONE = "none"
SANDBOX_FS_ONLY = "fs-only"
SANDBOX_FS_NO_NET = "fs+no-net"


@functools.lru_cache(maxsize=1)
def bwrap_available() -> bool:
    """Return True iff ``bwrap`` is installed and unprivileged user namespaces
    work on this host.

    Probes by actually running a trivial ``bwrap`` (the only reliable check —
    ``bwrap`` is present on many hosts where unprivileged userns is disabled,
    e.g. some hardened distros). Result is cached for the process. Tests
    monkeypatch this symbol (or clear the cache) rather than spawning bwrap.
    """
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        return False
    try:
        completed = subprocess.run(
            [bwrap, "--ro-bind", "/", "/", "--", "true"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def wrap_argv(
    argv: list[str],
    *,
    worktree: Path,
    policy: str,
) -> list[str]:
    """Wrap ``argv`` so it runs inside a bwrap sandbox per ``policy``.

    The agent sees only: a read-only system toolchain, fresh ``/proc`` /
    ``/dev`` / ``/tmp`` / ``/run``, a shadowed (tmpfs) ``$HOME``, and the
    worktree bound read-write at its real path (with cwd set there). It cannot
    see ``~/.ssh``, ``~/.aws``, ``~/.config/gh``, or any other clone. The
    namespaces are unshared (user/pid/ipc/uts/cgroup); ``fs+no-net`` also
    unshares the network. ``--die-with-parent`` ensures the child dies if
    gitbulk's per-target supervisor kills it (preserving the SIGTERM→SIGKILL
    timeout semantics of execk7nm).

    ``policy == "none"`` returns ``argv`` unchanged.
    """
    if policy == SANDBOX_NONE:
        return argv
    bwrap = shutil.which("bwrap") or "bwrap"
    wt = str(worktree)
    cmd: list[str] = [bwrap, "--die-with-parent"]
    cmd += [
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup",
    ]
    if policy == SANDBOX_FS_NO_NET:
        cmd += ["--unshare-net"]
    # Read-only system toolchain (only dirs that exist on this host).
    for d in _RO_SYSTEM_DIRS:
        if Path(d).exists():
            cmd += ["--ro-bind", d, d]
    # Fresh pseudo-filesystems and a shadowed home; the worktree is the one
    # writable real path.
    cmd += [
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--tmpfs", "/run",
        "--tmpfs", str(Path.home()),
        "--bind", wt, wt,
        "--chdir", wt,
    ]
    cmd += ["--", *argv]
    return cmd


__all__ = [
    "SANDBOX_FS_NO_NET",
    "SANDBOX_FS_ONLY",
    "SANDBOX_NONE",
    "bwrap_available",
    "wrap_argv",
]
