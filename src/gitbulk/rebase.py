"""Git rebase + force-push operations for ``gitbulk rebase-pr``.

Operates exclusively inside a disposable worktree created by
:func:`gitbulk.worktree.create_worktree` (detached HEAD at the PR's head
SHA). NEVER touches the user's main clone working tree / index / HEAD /
branch — the local-git safety contract (AGENTS.md). The only ref the
``fetch`` step updates is the shared ``refs/remotes/origin/*``, which the
contract permits.

Two operations:

  - :func:`rebase_onto_base` — fetch the latest base, rebase the
    worktree's detached HEAD onto it, and classify the outcome:
    CLEAN (ready to push), CONFLICT (worktree left mid-rebase for
    manual resolution, per node ``vp7n2krq``), or ERROR (a non-conflict
    git failure; the rebase is aborted so the worktree can be removed
    cleanly).
  - :func:`force_push_with_lease` — push the rebased HEAD back to the
    PR's head branch with ``--force-with-lease``, so an intervening
    push by anyone else aborts rather than clobbers (the force-push
    safety model chosen for rebase-pr).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from gitbulk.worktree import is_worktree_in_conflict, worktree_in_progress_op


class RebaseStatus(Enum):
    CLEAN = "clean"
    CONFLICT = "conflict"
    ERROR = "error"


class PushReadiness(Enum):
    """Whether a dispatch agent left a worktree safe for gitbulk to push.

    gitbulk — not the agent — performs every networked git op (this.i
    ``agpriv8n``). After the agent resolves a conflict locally and reports
    ``RESOLVED``, gitbulk independently re-checks the worktree before pushing;
    the agent's word is never trusted on its own (threat-model T1 / §5).
    """

    READY = "ready"  # clean, HEAD advanced → safe to force-push-with-lease
    NO_CHANGE = "no-change"  # clean but HEAD unchanged → nothing to push (benign)
    BLOCKED = "blocked"  # conflict markers / rebase still in progress → do NOT push


@dataclass(frozen=True)
class RebaseResult:
    """Outcome of :func:`rebase_onto_base`.

    ``detail`` is human-readable: the conflicted file list for CONFLICT,
    or the git stderr for ERROR, or a short note for CLEAN.
    """

    status: RebaseStatus
    detail: str


class RebaseError(RuntimeError):
    """Raised by :func:`force_push_with_lease` when the push fails
    (including a lease violation — the remote head moved)."""

    def __init__(self, message: str, *, stderr: str | None = None) -> None:
        super().__init__(message)
        self.stderr = stderr


def _git(
    worktree_path: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    """Run ``git -C worktree_path <args>``, never raising on non-zero.

    Single subprocess seam so tests patch one place. Callers inspect
    ``returncode``/``stderr`` themselves — git's exit codes carry
    meaning here (rebase returns non-zero on conflict, which is not an
    error we want to raise on).
    """
    return subprocess.run(
        ["git", "-C", str(worktree_path), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _conflicted_files(worktree_path: Path) -> list[str]:
    """Return the paths git reports as unmerged (diff-filter=U)."""
    completed = _git(
        worktree_path, "diff", "--name-only", "--diff-filter=U"
    )
    if completed.returncode != 0:
        return []
    return [ln.strip() for ln in completed.stdout.splitlines() if ln.strip()]


def rebase_onto_base(worktree_path: Path, base_ref: str) -> RebaseResult:
    """Fetch ``origin/<base_ref>`` and rebase the worktree HEAD onto it.

    The worktree is assumed to be at the PR's head SHA (detached). On
    return:
      - CLEAN: HEAD is the rebased tip, ready for force-push.
      - CONFLICT: the rebase stopped at a conflict; the worktree is left
        mid-rebase (NOT aborted) so a human can resolve and continue.
      - ERROR: a non-conflict failure (bad fetch, bad ref). The rebase
        is aborted so the worktree is clean and removable.
    """
    fetched = _git(worktree_path, "fetch", "origin", base_ref)
    if fetched.returncode != 0:
        return RebaseResult(
            RebaseStatus.ERROR,
            f"fetch origin {base_ref} failed: {fetched.stderr.strip()}",
        )

    rebased = _git(worktree_path, "rebase", f"origin/{base_ref}")
    if rebased.returncode == 0:
        return RebaseResult(RebaseStatus.CLEAN, f"rebased onto origin/{base_ref}")

    # Non-zero: distinguish a genuine merge conflict (preserve the
    # worktree for manual fix-up) from any other git failure (abort so
    # the worktree can be torn down cleanly).
    if is_worktree_in_conflict(worktree_path):
        files = _conflicted_files(worktree_path)
        detail = ", ".join(files) if files else "conflicted (files unknown)"
        return RebaseResult(RebaseStatus.CONFLICT, detail)

    # Not a conflict — undo the partial rebase so the worktree is clean.
    _git(worktree_path, "rebase", "--abort")
    return RebaseResult(
        RebaseStatus.ERROR,
        rebased.stderr.strip() or "rebase failed (no conflict markers)",
    )


def fetch_base(worktree_path: Path, base_ref: str) -> RebaseResult:
    """Fetch ``origin/<base_ref>`` into the worktree (the networked step).

    Called by gitbulk *before* launching a dispatch agent so the agent can
    rebase against the already-fetched ``origin/<base_ref>`` with **no network
    and no credentials of its own** (this.i ``agpriv8n``; what makes the
    ``fs+no-net`` sandbox viable, ``agsbx3k``). Returns CLEAN on success or
    ERROR with the git stderr; never raises (the caller decides whether a
    fetch failure skips the target).
    """
    fetched = _git(worktree_path, "fetch", "origin", base_ref)
    if fetched.returncode != 0:
        return RebaseResult(
            RebaseStatus.ERROR,
            f"fetch origin {base_ref} failed: {fetched.stderr.strip()}",
        )
    return RebaseResult(RebaseStatus.CLEAN, f"fetched origin/{base_ref}")


def verify_resolved_for_push(
    worktree_path: Path, original_head_sha: str
) -> tuple[PushReadiness, str]:
    """Independently verify a worktree is safe to push after an agent ran.

    gitbulk does NOT trust the agent's ``RESOLVED`` verdict as proof that work
    happened (threat-model §5). Before pushing it confirms, from git state
    alone:

      - no unresolved conflict markers (``is_worktree_in_conflict``),
      - no rebase/merge/cherry-pick still in progress
        (``worktree_in_progress_op``),
      - HEAD actually advanced past the SHA gitbulk observed when it created
        the worktree (otherwise there is nothing to push).

    Returns ``(PushReadiness, human-readable detail)``. ``BLOCKED`` means a
    spoofed/garbled ``RESOLVED`` (or a half-finished rebase) — gitbulk pushes
    nothing and the target is surfaced for attention.
    """
    if is_worktree_in_conflict(worktree_path):
        return (PushReadiness.BLOCKED, "unresolved conflict markers present")
    op = worktree_in_progress_op(worktree_path)
    if op is not None:
        return (PushReadiness.BLOCKED, f"{op} still in progress")
    head = _git(worktree_path, "rev-parse", "HEAD")
    if head.returncode != 0:
        return (PushReadiness.BLOCKED, f"cannot read HEAD: {head.stderr.strip()}")
    current = head.stdout.strip()
    if current == original_head_sha:
        return (PushReadiness.NO_CHANGE, "HEAD unchanged; nothing to push")
    return (PushReadiness.READY, f"HEAD advanced to {current[:7]}")


def force_push_with_lease(
    worktree_path: Path,
    head_ref: str,
    expected_sha: str,
) -> None:
    """Force-push the worktree HEAD to ``origin/<head_ref>`` with a lease.

    ``--force-with-lease=<head_ref>:<expected_sha>`` aborts the push if
    the remote ``head_ref`` no longer points at ``expected_sha`` (the
    PR's head SHA as gitbulk last observed it) — i.e. if anyone pushed
    in the meantime. The explicit ``:<expected_sha>`` form is used
    rather than the bare ``--force-with-lease`` because we did not fetch
    the head ref into a remote-tracking ref, so git has no recorded
    baseline to lease against; we supply the value we trust.

    Raises :class:`RebaseError` on any push failure (lease violation,
    no write access to a fork head, network, etc.).
    """
    pushed = _git(
        worktree_path,
        "push",
        f"--force-with-lease={head_ref}:{expected_sha}",
        "origin",
        f"HEAD:{head_ref}",
    )
    if pushed.returncode != 0:
        raise RebaseError(
            f"force-push of {head_ref} failed (exit {pushed.returncode})",
            stderr=pushed.stderr.strip(),
        )


__all__ = [
    "PushReadiness",
    "RebaseError",
    "RebaseResult",
    "RebaseStatus",
    "fetch_base",
    "force_push_with_lease",
    "rebase_onto_base",
    "verify_resolved_for_push",
]
