"""Git worktree helpers for ``gitbulk dispatch``.

Per this.i nodes ``mw6kp2nq`` (Worktree Root Under XDG Cache) and
``vp7n2krq`` (Rebase Conflicts Persist The Worktree), every worktree
the dispatch subcommand creates lives under
:func:`gitbulk.paths.default_worktree_root` (overridable via the
policy) and is named ``<runid>/<owner>__<repo>__pr<N>``. Worktrees in
git-conflict state are NOT removed automatically — the caller writes a
``CONFLICT.md`` next to the run directory and leaves the worktree on
disk so the user can resolve at the next sitting.

The path-verification step in :func:`create_worktree` is the
load-bearing defense from AGENTS.md "Worktree path verification": if
``git worktree add`` silently failed and we proceeded to write inside
the main clone, that would breach the local-git safety contract. The
``is_relative_to`` check rejects that branch eagerly.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from gitbulk import paths


class WorktreeError(RuntimeError):
    """Raised when a worktree operation fails or violates an invariant.

    Carries the underlying ``git`` argv and stderr where available so
    a caller can surface a useful error to the run state without
    re-running the command.
    """

    def __init__(
        self,
        message: str,
        *,
        command: tuple[str, ...] | None = None,
        stderr: str | None = None,
    ) -> None:
        super().__init__(message)
        self.command = command
        self.stderr = stderr


def _git_run(
    repo_path: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Invoke ``git -C repo_path <args>`` capturing stdout/stderr.

    Wrapped here so tests can patch a single seam. ``check=True``
    raises :class:`WorktreeError` on non-zero exit; ``check=False``
    returns the CompletedProcess for the caller to inspect.
    """
    argv = ("git", "-C", str(repo_path), *args)
    completed = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args)} failed (exit {completed.returncode})",
            command=argv,
            stderr=completed.stderr,
        )
    return completed


def create_worktree(
    repo_path: Path,
    slug: str,
    pr_number: int,
    pr_head_ref: str,
    pr_head_sha: str,
    *,
    worktree_root: Path | None = None,
    runid: str | None = None,
) -> Path:
    """Create a detached-HEAD worktree at ``pr_head_sha``.

    The worktree path is
    ``<worktree_root>/<runid>/<owner>__<repo>__pr<N>``. The function:

    1. Computes the target path.
    2. **Verifies** the target resolves under ``worktree_root`` —
       defense in depth against a misconfigured root, so a buggy
       caller cannot land a worktree inside the main clone.
    3. Runs ``git -C repo_path worktree add --detach target
       pr_head_sha``. Detached HEAD is deliberate: we do NOT want to
       create or move any branch in the user's clone (local-git
       safety contract).
    4. Verifies the worktree actually appeared at the target.
    5. Returns the target path.

    Parameters:
        repo_path: the user's clone, e.g., ``~/code/<repo>``.
        slug: ``"<owner>/<repo>"`` — normalized to ``owner__repo``
            for the directory name (via :func:`gitbulk.paths`).
        pr_number: PR number; appended as ``__pr<N>``.
        pr_head_ref: the PR's head branch name. Kept in the function
            signature so future telemetry / dashboard rows can record
            it; not used in the argv (detached HEAD is by SHA).
        pr_head_sha: the SHA to check out (detached).
        worktree_root: override for testing or per-policy. ``None``
            uses :func:`gitbulk.paths.default_worktree_root`.
        runid: subdirectory under ``worktree_root`` to group worktrees
            from one dispatch run. ``None`` → ``"adhoc"``.

    Raises:
        WorktreeError: on git failure OR on path verification failure.
    """
    del pr_head_ref  # documented in the signature; not used in argv

    root = (
        worktree_root if worktree_root is not None else paths.default_worktree_root()
    )
    runid_segment = runid if runid is not None else "adhoc"
    target = paths.worktree_dir(runid_segment, slug, root=root) / f"pr{pr_number}"

    # Path verification: target must resolve under root, and must NOT
    # be the same as repo_path. ``resolve(strict=False)`` because the
    # target doesn't exist yet (we're about to create it); the parent
    # chain still resolves enough for is_relative_to to be meaningful.
    resolved_target = target.resolve()
    resolved_root = root.resolve()
    if not resolved_target.is_relative_to(resolved_root):
        raise WorktreeError(
            f"refusing to create worktree outside worktree_root: "
            f"target={resolved_target} root={resolved_root}"
        )
    if resolved_target == repo_path.resolve():
        raise WorktreeError(
            f"refusing to create worktree at main clone path: {repo_path}"
        )

    target.parent.mkdir(parents=True, exist_ok=True)

    _git_run(
        repo_path,
        "worktree",
        "add",
        "--detach",
        str(target),
        pr_head_sha,
    )

    # Post-condition: the worktree must actually exist at target. If
    # git "succeeded" but the directory is missing OR the directory
    # ended up somewhere else (extremely unlikely, but cheap to
    # check), refuse to return the path — the caller would then write
    # into a non-worktree directory.
    if not target.is_dir():
        raise WorktreeError(
            f"git worktree add reported success but target does not exist: {target}"
        )

    return target


def remove_worktree(repo_path: Path, worktree_path: Path) -> None:
    """Remove a worktree via ``git worktree remove --force``.

    The ``--force`` flag is necessary because dispatch may have left
    modified files in the worktree (the whole point of running claude
    is to mutate files) that haven't been committed. The user has
    already taken whatever they want via the per-target log; the
    worktree itself is disposable.

    Per node ``vp7n2krq``, callers MUST check
    :func:`is_worktree_in_conflict` first and choose to preserve
    in-conflict worktrees. This function does not implement that gate
    itself because the policy is the caller's (dispatch may want to
    remove unconditionally during a forced GC; a normal run preserves
    conflicts).
    """
    _git_run(repo_path, "worktree", "remove", "--force", str(worktree_path))


def is_worktree_in_conflict(worktree_path: Path) -> bool:
    """Return True if ``worktree_path`` has any conflicted entries.

    ``git status --porcelain`` reports merge conflicts with the
    two-character codes ``UU``, ``AA``, ``DD``, ``AU``, ``UA``,
    ``DU``, ``UD`` per ``git-status(1)``. We treat any of those as
    "in conflict"; an empty porcelain output (or one without conflict
    codes) is "clean".
    """
    completed = subprocess.run(
        ["git", "-C", str(worktree_path), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        # If git itself fails (e.g., the worktree is broken), treat as
        # "in conflict" so the caller errs on the side of preservation
        # rather than silently rm-ing a directory whose state we can't
        # determine.
        return True
    conflict_codes = {"UU", "AA", "DD", "AU", "UA", "DU", "UD"}
    for line in completed.stdout.splitlines():
        if len(line) >= 2 and line[:2] in conflict_codes:
            return True
    return False


__all__ = [
    "WorktreeError",
    "create_worktree",
    "is_worktree_in_conflict",
    "remove_worktree",
]
