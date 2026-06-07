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
from dataclasses import dataclass
from pathlib import Path

from gitbulk import paths
from gitbulk.git import GIT

#: Two-character ``git status --porcelain`` codes that mark a merge
#: conflict (per git-status(1)). Shared by :func:`is_worktree_in_conflict`
#: and :func:`worktree_change_summary`.
_CONFLICT_CODES: frozenset[str] = frozenset(
    {"UU", "AA", "DD", "AU", "UA", "DU", "UD"}
)


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
    argv = (GIT, "-C", str(repo_path), *args)
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
        [GIT, "-C", str(worktree_path), "status", "--porcelain"],
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
    for line in completed.stdout.splitlines():
        if len(line) >= 2 and line[:2] in _CONFLICT_CODES:
            return True
    return False


# ─── prune-worktrees surface (node prnwt5nq) ───────────────────────────────


@dataclass(frozen=True)
class WorktreeEntry:
    """One row from ``git worktree list --porcelain``.

    ``is_main`` is True for the clone's primary working tree (always the
    first entry git lists). prune-worktrees must NEVER remove a main
    entry — that is the tree the user edits (local-git safety contract).
    ``branch`` is ``None`` for a detached-HEAD worktree.
    """

    path: Path
    head_sha: str
    branch: str | None
    is_main: bool
    is_detached: bool
    is_locked: bool
    is_bare: bool


def list_worktrees(repo_path: Path) -> list[WorktreeEntry]:
    """Parse ``git -C repo_path worktree list --porcelain``.

    Returns one :class:`WorktreeEntry` per worktree, the main checkout
    first. Raises :class:`WorktreeError` if git fails — the caller treats
    a repo whose worktrees can't be enumerated as "skip with reason"
    rather than guessing.
    """
    completed = _git_run(repo_path, "worktree", "list", "--porcelain")
    entries: list[WorktreeEntry] = []
    # Porcelain emits blank-line-separated blocks; each starts with a
    # ``worktree <path>`` line. Accumulate fields until the block ends.
    cur: dict = {}

    def _flush() -> None:
        if not cur:
            return
        entries.append(
            WorktreeEntry(
                path=Path(cur["worktree"]),
                head_sha=cur.get("HEAD", ""),
                branch=cur.get("branch"),
                is_main=(len(entries) == 0),
                is_detached=cur.get("detached", False),
                is_locked=cur.get("locked", False),
                is_bare=cur.get("bare", False),
            )
        )
        cur.clear()

    for line in completed.stdout.splitlines():
        if not line.strip():
            _flush()
            continue
        key, _, rest = line.partition(" ")
        if key == "worktree":
            cur["worktree"] = rest
        elif key == "HEAD":
            cur["HEAD"] = rest
        elif key == "branch":
            # rest is like ``refs/heads/feature-x`` → store short name.
            cur["branch"] = rest[len("refs/heads/"):] if rest.startswith(
                "refs/heads/"
            ) else rest
        elif key == "detached":
            cur["detached"] = True
        elif key == "bare":
            cur["bare"] = True
        elif key == "locked":
            cur["locked"] = True
        # Other keys (prunable, etc.) are ignored.
    _flush()
    return entries


def local_branch_upstreams(repo_path: Path) -> list[tuple[str, str | None]]:
    """Return ``(local_branch, upstream_remote_branch)`` for every local branch.

    The second element is the branch NAME on the remote that the local branch
    tracks, or ``None`` when the branch tracks nothing. We read
    ``%(upstream:remoteref)``, which is the ref name ON THE REMOTE —
    ``refs/heads/<branch>`` — deliberately NOT bare ``%(upstream)`` /
    ``:short``, which give the LOCAL tracking ref (``refs/remotes/<remote>/
    <branch>`` / ``<remote>/<branch>``). We strip the ``refs/heads/`` prefix to
    the bare branch name. As defence in depth on this safety-critical path we
    ALSO strip a ``refs/remotes/<remote>/`` prefix, so the parser still yields
    the bare branch name even if the format is ever changed to the tracking-ref
    form (a wrong result here could delete a protected/default-tracking branch).

    prune-worktrees (node prnwlb7q) decides protection by the REMOTE's notion of
    default/protected applied to this upstream — never by the local branch's
    name, which is unreliable (a branch named ``main`` need not track
    ``origin/main``, and an integration branch can be named anything).
    Read-only; honours the local-git safety contract. Raises
    :class:`WorktreeError` on git failure so the caller treats a clone whose
    branches can't be enumerated as "skip with reason".
    """
    completed = _git_run(
        repo_path,
        "for-each-ref",
        "--format=%(refname:short)%09%(upstream:remoteref)",
        "refs/heads",
    )
    out: list[tuple[str, str | None]] = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        name, _, remoteref = line.partition("\t")
        name = name.strip()
        if not name:
            continue
        remoteref = remoteref.strip()
        if remoteref.startswith("refs/heads/"):
            upstream: str | None = remoteref[len("refs/heads/"):]
        elif remoteref.startswith("refs/remotes/"):
            # Defensive: strip refs/remotes/<remote>/ to the bare branch name.
            _remote, _, branch = remoteref[len("refs/remotes/"):].partition("/")
            upstream = branch or None
        else:
            upstream = remoteref or None
        out.append((name, upstream))
    return out


def worktree_change_summary(worktree_path: Path) -> tuple[bool, bool, bool]:
    """Return ``(tracked_dirty, has_untracked, conflicted)`` for a worktree.

    ``tracked_dirty`` is True if any tracked file is modified/staged;
    ``has_untracked`` if any ``??`` entry exists; ``conflicted`` if any
    conflict code is present. On git failure all three are True so the
    caller refuses to remove a worktree whose state it cannot read.
    """
    completed = subprocess.run(
        [GIT, "-C", str(worktree_path), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return (True, True, True)
    tracked_dirty = False
    has_untracked = False
    conflicted = False
    for line in completed.stdout.splitlines():
        if len(line) < 2:
            continue
        code = line[:2]
        if code == "??":
            has_untracked = True
        elif code in _CONFLICT_CODES:
            conflicted = True
            tracked_dirty = True
        else:
            tracked_dirty = True
    return (tracked_dirty, has_untracked, conflicted)


#: ``git`` relative paths whose existence signals an interrupted operation.
#: Mapped to the operation name surfaced in the skip reason.
_IN_PROGRESS_PATHS: tuple[tuple[str, str], ...] = (
    ("rebase-merge", "rebase"),
    ("rebase-apply", "rebase"),
    ("MERGE_HEAD", "merge"),
    ("CHERRY_PICK_HEAD", "cherry-pick"),
    ("REVERT_HEAD", "revert"),
)


def worktree_in_progress_op(worktree_path: Path) -> str | None:
    """Return the name of an in-progress git operation, or ``None``.

    Detects a paused rebase/merge/cherry-pick/revert even when there are
    no conflict markers (a clean pause). Such a worktree holds state we
    must not destroy, so the prune handler skips it.
    """
    for rel, name in _IN_PROGRESS_PATHS:
        completed = subprocess.run(
            [GIT, "-C", str(worktree_path), "rev-parse", "--git-path", rel],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            # Can't resolve git-path → can't prove it's clean → treat as
            # in-progress (fail safe).
            return name
        candidate = completed.stdout.strip()
        if not candidate:
            continue
        # ``--git-path`` returns a path relative to git's cwd (the
        # worktree) unless already absolute.
        resolved = Path(candidate)
        if not resolved.is_absolute():
            resolved = worktree_path / candidate
        if resolved.exists():
            return name
    return None


def branch_unpushed_commit_count(repo_path: Path, branch: str) -> int:
    """Commits on ``branch`` that exist on NO remote-tracking branch.

    ``git rev-list --count <branch> --not --remotes``. ``0`` means every
    commit on the branch is already on some remote — deleting the local
    branch loses nothing (the local half of the data-loss guard,
    node prdls2nq). Raises :class:`WorktreeError` on git failure.
    """
    completed = _git_run(
        repo_path, "rev-list", "--count", branch, "--not", "--remotes"
    )
    try:
        return int(completed.stdout.strip())
    except ValueError as exc:
        raise WorktreeError(
            f"branch_unpushed_commit_count({branch!r}): unexpected output "
            f"{completed.stdout.strip()!r}"
        ) from exc


def remove_linked_worktree(repo_path: Path, worktree_path: Path) -> None:
    """Remove a LINKED worktree via ``git worktree remove`` (no --force).

    Path-verified: refuses if ``worktree_path`` resolves to ``repo_path``
    itself (the main worktree). Without ``--force``, git itself refuses a
    dirty or locked worktree — a second line of defense behind the
    handler's own guards. Follows up with ``git worktree prune`` to clear
    any now-stale admin entries. Per node wtrm6kpq this is the one blessed
    mutating local operation.
    """
    if worktree_path.resolve() == repo_path.resolve():
        raise WorktreeError(
            f"refusing to remove the main worktree: {worktree_path}"
        )
    _git_run(repo_path, "worktree", "remove", str(worktree_path))
    _git_run(repo_path, "worktree", "prune")


def delete_merged_local_branch(repo_path: Path, branch: str) -> bool:
    """Delete a local branch with ``git branch -d`` (merged-only).

    ``-d`` (lowercase) refuses to delete a branch not fully merged into
    its upstream or HEAD — a built-in data-loss guard. Returns True if
    the branch was deleted, False if git refused (unmerged) or the branch
    was absent. Never raises for the refusal case: a kept branch is a
    valid, safe outcome.
    """
    completed = _git_run(
        repo_path, "branch", "-d", branch, check=False
    )
    return completed.returncode == 0


__all__ = [
    "WorktreeEntry",
    "WorktreeError",
    "branch_unpushed_commit_count",
    "create_worktree",
    "delete_merged_local_branch",
    "is_worktree_in_conflict",
    "list_worktrees",
    "local_branch_upstreams",
    "remove_linked_worktree",
    "remove_worktree",
    "worktree_change_summary",
    "worktree_in_progress_op",
]
