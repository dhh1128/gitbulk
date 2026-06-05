"""Self-contained agent workspaces for sandboxed dispatch (this.i agecln4k).

SEC-F1: a linked ``git worktree`` is NOT usable inside a bubblewrap sandbox —
its ``.git`` is a pointer into the operator's clone (``<clone>/.git/worktrees/
<name>`` → commondir → the clone's objects/refs/config/hooks), none of which the
sandbox binds, and ``--tmpfs $HOME`` shadows the clone outright. Binding the
clone's ``.git`` to make git work would re-expose its hooks/config to the
auto-approve agent (hook-planting). So sandboxed agents instead get a
**self-contained clone**:

  - ``git clone --no-hardlinks --no-checkout`` of the operator's local clone —
    a standalone repo with its OWN ``.git`` (objects copied, not shared; no
    pointer back to the operator clone; default — neutralized — hooks).
  - ``origin`` reset to the real remote URL, so gitbulk's later push targets
    GitHub (not the local clone).
  - ``core.hooksPath`` pointed at an empty dir, so even the clone's own sample
    hooks (or anything an agent writes there) never execute.
  - the PR head fetched from the real remote and checked out detached.

All of this runs OUTSIDE the sandbox (gitbulk has the network + credentials).
The agent then runs bound to this directory ALONE: git works (the ``.git`` is
inside the bound dir), and there is no filesystem path from the agent to the
operator's real clone, other repos, or credentials. Teardown is a plain
``rmtree`` — there is no worktree admin entry in the operator clone to unregister.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from gitbulk import paths
from gitbulk.git import GIT
from gitbulk.worktree import WorktreeError

#: Directory name inside the clone's ``.git`` used as an empty hooks dir.
_EMPTY_HOOKS_DIRNAME = "gitbulk-no-hooks"


def _run(
    *args: str, cwd: Path | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` (optionally with ``cwd``), list-form, no shell."""
    completed = subprocess.run(
        [GIT, *args],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args)} failed (exit {completed.returncode})",
            command=("git", *args),
            stderr=completed.stderr,
        )
    return completed


def create_isolated_clone(
    repo_path: Path,
    slug: str,
    pr_number: int,
    pr_head_ref: str,
    pr_head_sha: str,
    *,
    worktree_root: Path | None = None,
    runid: str | None = None,
) -> Path:
    """Create a self-contained clone checked out at ``pr_head_sha`` (detached).

    Mirrors :func:`gitbulk.worktree.create_worktree`'s pathing and path
    verification, but produces a standalone repo (see module docstring). The
    networked steps (origin URL, head fetch) run here, outside any sandbox.

    Raises :class:`~gitbulk.worktree.WorktreeError` on git failure or path
    verification failure.
    """
    root = (
        worktree_root if worktree_root is not None else paths.default_worktree_root()
    )
    runid_segment = runid if runid is not None else "adhoc"
    target = (
        paths.worktree_dir(runid_segment, slug, root=root) / f"pr{pr_number}-clone"
    )

    # Path verification: under root, and never the main clone itself.
    resolved_target = target.resolve()
    resolved_root = root.resolve()
    if not resolved_target.is_relative_to(resolved_root):
        raise WorktreeError(
            f"refusing to create isolated clone outside worktree_root: "
            f"target={resolved_target} root={resolved_root}"
        )
    if resolved_target == repo_path.resolve():
        raise WorktreeError(
            f"refusing to create isolated clone at main clone path: {repo_path}"
        )

    target.parent.mkdir(parents=True, exist_ok=True)

    # 1. Standalone local clone — own .git, objects copied (--no-hardlinks),
    #    no working-tree checkout yet (--no-checkout). Local source ⇒ no network.
    _run(
        "clone",
        "--no-hardlinks",
        "--no-checkout",
        "--quiet",
        str(repo_path),
        str(target),
    )
    if not (target / ".git").is_dir():
        raise WorktreeError(
            f"git clone reported success but .git is missing at: {target}"
        )

    # 2. Point origin at the REAL remote so gitbulk's later push goes to GitHub,
    #    not the operator's local clone.
    url = _run("remote", "get-url", "origin", cwd=repo_path).stdout.strip()
    _run("remote", "set-url", "origin", url, cwd=target)

    # 3. Neutralize hooks: even a fresh clone has sample hooks, and the agent
    #    has write access to this repo — point core.hooksPath at an empty dir so
    #    nothing in hooks/ can ever run.
    empty_hooks = target / ".git" / _EMPTY_HOOKS_DIRNAME
    empty_hooks.mkdir(parents=True, exist_ok=True)
    _run("config", "core.hooksPath", str(empty_hooks), cwd=target)

    # 4. Fetch the PR head from the real remote (the local clone may not carry
    #    it as a copied ref) and check it out detached. Network here is fine —
    #    we are outside the sandbox. The base is fetched separately by the
    #    caller (rebase.fetch_base), uniformly with the worktree path.
    _run("fetch", "origin", pr_head_ref, cwd=target)
    _run("checkout", "--detach", "--quiet", pr_head_sha, cwd=target)
    return target


def remove_isolated_clone(clone_path: Path, *, worktree_root: Path) -> None:
    """Delete an isolated clone. It is standalone, so a plain recursive remove
    is correct (no worktree admin entry to unregister).

    Guards against deleting anything outside ``worktree_root`` (defense in depth
    against a bad path), mirroring the spirit of create's path check.
    """
    resolved = clone_path.resolve()
    if not resolved.is_relative_to(worktree_root.resolve()):
        raise WorktreeError(
            f"refusing to remove isolated clone outside worktree_root: "
            f"{resolved}"
        )
    try:
        shutil.rmtree(resolved)
    except OSError as e:
        raise WorktreeError(
            f"failed to remove isolated clone {resolved}: {e}"
        )


__all__ = ["create_isolated_clone", "remove_isolated_clone"]
