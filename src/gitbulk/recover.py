"""Recovery core for ``gitbulk recover-branch`` (tick 6lui).

Restores a branch that ``prune-branches --apply`` deleted, using the
durable audit trail prune-branches writes: each deleted branch leaves a
row in the run's ``state.yaml`` (``repos[slug].branches[]``) carrying its
tip ``sha`` and ``disposition: deleted``. Recovery re-creates the ref via
the GitHub git-ref API.

This is robust because prune-branches' data-loss guard only ever deletes a
branch whose tip is either the merged PR head (pinned forever by
``refs/pull/N/head``) or contained in the default branch (reachable from
history) — so the recorded SHA is never garbage-collected and can always be
re-pointed (verified live 2026-06-06).

The module is deliberately pure plus a single injected ``gh`` boundary so it
is fully testable with a hand-built ``repos`` map and a fake gh client — no
network and no real pruned branch. The command wrapper lives in
:mod:`gitbulk.commands.recover_branch`.
"""

from __future__ import annotations

from dataclasses import dataclass

from gitbulk.gh import GHError


@dataclass(frozen=True)
class DeletedBranch:
    """One branch a prior prune-branches run deleted, as recovered from its
    ``state.yaml`` row. ``sha`` is the tip recorded immediately before
    deletion — the ref to re-create."""

    slug: str
    branch: str
    sha: str
    pr_number: int | None = None
    reason: str = ""


@dataclass(frozen=True)
class RecoverOutcome:
    """The result of attempting to restore one :class:`DeletedBranch`.

    ``status`` is one of:
      ``"recovered"``        — the ref was absent and we re-created it.
      ``"already-present"``  — the ref already exists (idempotent re-run, or
                               restored by other means); left untouched.
      ``"failed"``           — the create call raised; ``detail`` carries why.
    """

    slug: str
    branch: str
    sha: str
    status: str
    detail: str = ""


def collect_deleted(
    repos: dict,
    *,
    slug: str | None = None,
    branch: str | None = None,
) -> list[DeletedBranch]:
    """Return every ``disposition: deleted`` branch in a prune-branches
    ``state.yaml`` ``repos`` map, sorted by slug for stable output.

    ``slug`` / ``branch`` narrow the selection. Rows missing a ``branch`` or
    ``sha`` (nothing to restore from) and non-dict repo entries are skipped
    defensively — a malformed or partial audit file yields fewer recoveries,
    never a crash.
    """
    out: list[DeletedBranch] = []
    for repo_slug, entry in sorted(repos.items()):
        if slug is not None and repo_slug != slug:
            continue
        if not isinstance(entry, dict):
            continue
        for row in entry.get("branches", []) or []:
            if not isinstance(row, dict):
                continue
            if row.get("disposition") != "deleted":
                continue
            name = row.get("branch")
            sha = row.get("sha")
            if not name or not sha:
                continue
            if branch is not None and name != branch:
                continue
            out.append(
                DeletedBranch(
                    slug=repo_slug,
                    branch=name,
                    sha=sha,
                    pr_number=row.get("pr_number"),
                    reason=row.get("reason", ""),
                )
            )
    return out


def recover_one(gh, db: DeletedBranch) -> RecoverOutcome:
    """Restore one deleted branch, idempotently.

    Pre-checks the ref with :meth:`gh.branch_ref_sha`: an already-present
    branch is reported (never overwritten — even at a different SHA, which is
    surfaced so the operator can investigate rather than clobber newer work).
    Only an absent ref is (re-)created. A failed create is captured as a
    ``failed`` outcome rather than raised, so a batch recovery continues past
    one bad repo.
    """
    existing = gh.branch_ref_sha(db.slug, db.branch)
    if existing is not None:
        if existing == db.sha:
            detail = "branch already exists at the recorded SHA"
        else:
            detail = (
                f"branch already exists at a different SHA ({existing[:12]}); "
                f"left untouched"
            )
        return RecoverOutcome(db.slug, db.branch, db.sha, "already-present", detail)
    try:
        gh.create_branch_ref(db.slug, db.branch, db.sha)
    except GHError as e:
        return RecoverOutcome(db.slug, db.branch, db.sha, "failed", str(e))
    return RecoverOutcome(db.slug, db.branch, db.sha, "recovered", "")
