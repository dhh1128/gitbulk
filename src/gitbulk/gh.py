"""GitHub network boundary for gitbulk.

The :class:`GHClient` Protocol is the only legitimate surface between
gitbulk code and GitHub. Two implementations live here:

  - :class:`ProductionGHClient` subprocesses to ``gh`` for real.
  - :class:`FakeGHClient` returns canned data; used by every test in
    the project per AGENTS.md "no network in tests."

See this.i node ``ghclmp7n`` (gh Client Implementation) for the load-
bearing design choices: Protocol shape, per-method typed API,
coalescing inside the client, stateless, hardcoded retry, per-call
timeout kwarg.

Constraint ``hp4nck2v`` makes ``gh`` the exclusive channel for GitHub
network traffic; this module is where that constraint is enforced.

Every ``gh`` command actually invoked by ProductionGHClient is
verified against GitHub API deprecations at integration time. The
verification date is recorded in a comment at the call site (per
AGENTS.md "Verify gh invocations against GitHub API deprecations"
and the feedback memory ``feedback-gh-cli-deprecation-verification``).
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import (
    Any,
    Callable,
    Iterable,
    Literal,
    Mapping,
    Protocol,
    runtime_checkable,
)

from gitbulk.pr_info import (
    BranchRef,
    CheckRun,
    ClosedPRRef,
    PRComment,
    PRInfo,
    TimelineEvent,
)


@runtime_checkable
class GHClient(Protocol):
    """Read-only GitHub network surface.

    Implementations:
      - :class:`ProductionGHClient` (subprocess to ``gh``)
      - :class:`FakeGHClient` (in-memory canned responses; tests)

    All methods are read-only as of Phase 2. Mutating operations (merge,
    close, push, branch deletion) will land in Phase 5 with their own
    Protocol methods; they intentionally do NOT exist yet so that no
    Phase-2 invariant can accidentally mutate.
    """

    def authenticated_user(self, *, timeout: float | None = None) -> dict[str, Any]:
        """Return the JSON for the currently authenticated GitHub user.

        Used by the ``gh.authenticated`` preflight invariant. Raises
        :class:`GHError` if gh is unauthed or unreachable.
        """
        ...

    def org_members(
        self, org: str, *, timeout: float | None = None
    ) -> list[str]:
        """Return all member logins of ``org``. Paginated; result is
        the full list."""
        ...

    def default_branch(
        self, slug: str, *, timeout: float | None = None
    ) -> str:
        """Return the current default-branch name of ``slug`` on GitHub."""
        ...

    def prefetch_default_branches(
        self,
        slugs: Iterable[str],
        *,
        timeout: float | None = None,
        on_progress: "Callable[[int, int], None] | None" = None,
    ) -> None:
        """Batch-fetch default branches for ``slugs`` into an in-process
        cache so subsequent ``default_branch`` calls hit memory.

        Issues GraphQL queries with aliased ``repository()`` nodes
        (chunked, because GitHub 502s past ~150 nodes) instead of one
        REST call per slug — cuts a 200-repo fleet's per-repo phase
        from ~60s of sequential REST to a handful of batched round-
        trips. If a chunk fails (network, rate limit, unresolvable
        repo), its slugs are left uncached and ``default_branch`` falls
        back to the per-slug REST path for them.

        ``on_progress`` (if given) is called after each chunk with
        ``(slugs_completed, slugs_total)`` so callers can render a
        progress indicator — the fetch is multi-second for large
        fleets and otherwise looks like a hang.
        """
        ...

    def seed_default_branches(self, mapping: dict[str, str]) -> None:
        """Populate the in-process default-branch cache from an external
        source (the on-disk cache) WITHOUT any network call.

        Lets :mod:`gitbulk.default_branch_cache` hand gitbulk a warm
        cache so ``default_branch`` hits memory for slugs that were
        fetched on a prior run. Existing entries are overwritten by
        ``mapping``.
        """
        ...

    def cached_default_branches(self) -> dict[str, str]:
        """Return a copy of the current in-process default-branch cache.

        Used by :mod:`gitbulk.default_branch_cache` to read back what a
        prefetch resolved (so it can be persisted to disk) without
        triggering any per-slug network fallback.
        """
        ...

    def is_archived(self, slug: str, *, timeout: float | None = None) -> bool:
        """Return True iff ``slug`` is archived on GitHub.

        Backs the ``github.not_archived`` PER_REPO invariant. Archived
        status is populated as a side effect of
        :meth:`prefetch_default_branches` (the same coalesced GraphQL
        query selects ``isArchived``), so on a warm cache this is a
        memory hit with no network cost. A cache miss falls back to a
        per-slug REST lookup of the repo's ``archived`` field.
        """
        ...

    def seed_archived(self, mapping: dict[str, bool]) -> None:
        """Populate the in-process archived cache from an external source
        (the on-disk default-branch cache) WITHOUT any network call."""
        ...

    def cached_archived(self) -> dict[str, bool]:
        """Return a copy of the current in-process archived cache.

        Used by :mod:`gitbulk.default_branch_cache` to read back what a
        prefetch resolved so it can be persisted alongside the branch.
        """
        ...

    def my_open_prs(
        self,
        slugs: Iterable[str] | None = None,
        *,
        author: str | None = "@me",
        timeout: float | None = None,
    ) -> dict[str, list[PRInfo]]:
        """Return open PRs grouped by repo slug.

        ``author`` is the GitHub search author qualifier: ``"@me"``
        (default, the authenticated user — the historical behavior),
        a specific login, or ``None`` for "any author" (no author:
        qualifier — used when a filter widens scope beyond the user's
        own PRs, per node ``flt7arg2``).

        If ``slugs`` is given, only PRs against those repos are returned;
        repos with no matching PRs map to an empty list. If ``slugs`` is
        None, all repos with matching PRs are returned.

        Chunks/paginates internally (per ``ghclmp7n.c`` + the pagination
        fix) regardless of how many slugs are passed.
        """
        ...

    def merge_pr(
        self,
        slug: str,
        number: int,
        *,
        method: Literal["merge", "squash", "rebase"] = "merge",
        delete_branch: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Merge the PR via ``gh pr merge``.

        Phase 5 mutating method. The only legitimate caller is the
        ``merge`` subcommand handler, and only on its ``--apply`` path
        per node ``2vqp4nk6``. Raises :class:`GHError` on non-clean
        mergeable state, auth failure, or any other refusal from gh.

        ``method`` selects merge / squash / rebase. Default ``merge``
        (true merge commit) per this.i node ``gji4dyze`` — the merge
        handler typically passes the per-repo-overridden value from
        ``policy_for(slug).merge_method`` and only falls back to this
        default when called from contexts without a Policy.
        ``delete_branch`` toggles ``--delete-branch``; the merge handler
        passes True so the remote PR branch is cleaned up after a
        successful merge (GitHub server-side refuses if another open PR
        targets the same head).

        NOTE: the ``.agent-bin/gh`` shim BLOCKS ``gh pr merge`` for AI
        agents. Production code constructs the right argv anyway; the
        shim catches at process boundary. Tests use FakeGHClient so the
        shim is not involved.

        Returns the gh response payload as a dict (gh emits an empty
        body on success in some versions; we return ``{}`` in that case
        rather than raising).
        """
        ...

    def approve_pr(
        self,
        slug: str,
        number: int,
        *,
        body: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Post an APPROVING review on the PR via ``gh pr review --approve``.

        Phase-5+ mutating method (this.i node ``aprmn5kq``). The only
        legitimate caller is the ``merge`` subcommand's ``--apply
        --approve`` path: it supplies the maintainer's approval
        programmatically on an eligible bot PR so a green-but-unapproved
        PR can auto-merge. ``body`` (if given) is passed as ``--body``.

        Raises :class:`GHError` if gh refuses (e.g. GitHub's 422 on
        self-approval, or insufficient permission).
        """
        ...

    def viewer_repo_permission(
        self, slug: str, *, timeout: float | None = None
    ) -> str:
        """Return the authenticated viewer's permission level on ``slug``.

        One of ``"admin" | "maintain" | "write" | "triage" | "read" |
        "none"``. Read-only. Used by the ``merge --approve`` maintainer
        gate (this.i node ``aprmn5kq``): auto-approval only fires when
        the viewer holds write/maintain/admin.
        """
        ...

    def fetch_pr_comments(
        self,
        slug: str,
        number: int,
        *,
        timeout: float | None = None,
    ) -> list[PRComment]:
        """Return the PR's issue-comments (oldest first).

        Read-only. Used by ``close-stale`` to find prior gitbulk warning
        markers without paying the cost on every subcommand: the comment
        slice is NOT part of :func:`my_open_prs`. Walks the last 50
        comments via GraphQL.
        """
        ...

    def post_comment(
        self,
        slug: str,
        number: int,
        body: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Post an issue-comment on the PR via ``gh pr comment``.

        Mutating method used by ``close-stale --apply`` to drop the
        stale-warning comment. Body should include the gitbulk marker
        (HTML comment ``<!-- gitbulk: stale-warning v1 -->``) so future
        runs can find it via :meth:`fetch_pr_comments`.
        """
        ...

    def close_pr(
        self,
        slug: str,
        number: int,
        *,
        delete_branch: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Close the PR via ``gh pr close``.

        Mutating method used by ``close-stale --apply``. ``delete_branch``
        defaults to False (the stale-close path keeps the branch so the
        author can revisit and reopen — per the design decision recorded
        in this.i; contrast with merge_pr which defaults True).
        """
        ...

    def fetch_merge_commit_sha(
        self,
        slug: str,
        number: int,
        *,
        timeout: float | None = None,
    ) -> str | None:
        """Return the resulting merge commit SHA for ``slug``#``number``,
        or None if the PR isn't merged or no merge commit exists.

        Called by the merge handler after a successful ``gh pr merge``
        so the SHA can be recorded in state.yaml for the post-merge
        watchdog (report's "Recent merges" section).
        """
        ...

    def fetch_check_runs(
        self,
        slug: str,
        sha: str,
        *,
        timeout: float | None = None,
    ) -> list[CheckRun]:
        """Return all check-runs reported for ``sha`` on ``slug``.

        Read-only. Used by the post-merge watchdog to detect CI/CD
        failures that fire on the merge commit (e.g., a cd.yml deploy
        workflow that broke after the merge landed).
        """
        ...

    # ─── prune-branches / prune-worktrees surface (nodes prnbr4kq / prnwt5nq) ──

    def list_branches(
        self, slug: str, *, timeout: float | None = None
    ) -> list[BranchRef]:
        """Return every branch on ``slug`` with its tip SHA and the
        branch-protection flag. Read-only."""
        ...

    def closed_prs_for_head(
        self, slug: str, head_ref: str, *, timeout: float | None = None
    ) -> list[ClosedPRRef]:
        """Return the closed-or-merged PRs whose head branch on the
        UPSTREAM repo is ``head_ref`` (newest first). Read-only.

        Filters server-side by ``head=<owner>:<head_ref>`` so it returns
        only PRs that originated on the upstream, never fork PRs that
        happen to share a branch name."""
        ...

    def branch_ahead_by(
        self, slug: str, base: str, branch: str, *, timeout: float | None = None
    ) -> int:
        """Return how many commits ``branch`` has that ``base`` does not
        (``ahead_by`` from the compare API). ``0`` means ``branch`` is
        fully contained in ``base``. Read-only — the data-loss guard
        (prdls2nq) uses this."""
        ...

    def delete_branch_ref(
        self, slug: str, branch: str, *, timeout: float | None = None
    ) -> None:
        """Delete the remote branch ``branch`` on ``slug`` via the git-ref
        API (node prdel4rq). The one mutating method the prune surface
        adds."""
        ...


class GHError(RuntimeError):
    """Raised when a gh invocation fails in a way the caller is expected
    to handle (network error, auth missing, unexpected JSON shape, etc.).

    Per-call ``timeout`` exceedance raises :class:`GHTimeoutError`
    (a subclass). The retry policy inside ProductionGHClient catches
    transient failures before they reach the caller; what bubbles up is
    a real, persistent problem."""

    def __init__(self, message: str, *, command: tuple[str, ...] | None = None) -> None:
        super().__init__(message)
        self.command = command


class GHTimeoutError(GHError, TimeoutError):
    """Raised when a per-call timeout elapses with the retry policy still
    not having succeeded."""


# ─── FakeGHClient ───────────────────────────────────────────────────────────


class FakeGHClient:
    """In-memory GHClient for tests.

    Configure with canned responses at construction time; each Protocol
    method either returns the configured value or raises a configured
    error. Tests get a deterministic, network-free GHClient that satisfies
    the Protocol contract.

    Example::

        fake = FakeGHClient(
            user={"login": "dhh1128"},
            org_members={"provenant-dev": ["dhh1128", "alice"]},
            default_branches={"dhh1128/gitbulk": "main"},
            my_open_prs={"dhh1128/gitbulk": [PRInfo(...)]},
        )

    Any unset field raises :class:`GHError` when the corresponding method
    is called — so tests fail loudly when they exercise a code path
    they did not configure.
    """

    def __init__(
        self,
        *,
        user: dict[str, Any] | None = None,
        org_members: Mapping[str, list[str]] | None = None,
        default_branches: Mapping[str, str] | None = None,
        my_open_prs: Mapping[str, list[PRInfo]] | None = None,
        merge_responses: Mapping[
            tuple[str, int], "dict[str, Any] | Exception"
        ] | None = None,
        pr_comments: Mapping[tuple[str, int], list[PRComment]] | None = None,
        post_comment_responses: Mapping[
            tuple[str, int], "dict[str, Any] | Exception"
        ] | None = None,
        close_responses: Mapping[
            tuple[str, int], "dict[str, Any] | Exception"
        ] | None = None,
        merge_commit_shas: Mapping[tuple[str, int], "str | None"] | None = None,
        check_runs: Mapping[tuple[str, str], list[CheckRun]] | None = None,
        approve_responses: Mapping[
            tuple[str, int], "dict[str, Any] | Exception"
        ] | None = None,
        repo_permissions: Mapping[str, str] | None = None,
        archived: Mapping[str, "bool | Exception"] | None = None,
        branches: Mapping[str, "list[BranchRef] | Exception"] | None = None,
        closed_prs_for_head: Mapping[
            tuple[str, str], "list[ClosedPRRef] | Exception"
        ] | None = None,
        branch_ahead_by: Mapping[
            tuple[str, str, str], "int | Exception"
        ] | None = None,
        delete_branch_responses: Mapping[
            tuple[str, str], "None | Exception"
        ] | None = None,
    ) -> None:
        self._user = user
        self._org_members = dict(org_members) if org_members is not None else None
        self._default_branches = (
            dict(default_branches) if default_branches is not None else None
        )
        self._my_open_prs = (
            {k: list(v) for k, v in my_open_prs.items()}
            if my_open_prs is not None
            else None
        )
        self._merge_responses = (
            dict(merge_responses) if merge_responses is not None else None
        )
        self._pr_comments = (
            {k: list(v) for k, v in pr_comments.items()}
            if pr_comments is not None
            else None
        )
        self._post_comment_responses = (
            dict(post_comment_responses)
            if post_comment_responses is not None
            else None
        )
        self._close_responses = (
            dict(close_responses) if close_responses is not None else None
        )
        self._merge_commit_shas = (
            dict(merge_commit_shas) if merge_commit_shas is not None else None
        )
        self._check_runs = (
            {k: list(v) for k, v in check_runs.items()}
            if check_runs is not None
            else None
        )
        self._approve_responses = (
            dict(approve_responses) if approve_responses is not None else None
        )
        self._repo_permissions = (
            dict(repo_permissions) if repo_permissions is not None else None
        )
        # Archived map doubles as the in-process archived cache. Unlike
        # default_branches (None → raise), an unset/missing slug here
        # resolves to False, so every chain test that doesn't configure
        # archived still passes the github.not_archived gate. A slug whose
        # value is an Exception raises it (mirrors merge_responses) so the
        # GHError→Skip branch is testable.
        self._archived: dict[str, "bool | Exception"] = (
            dict(archived) if archived is not None else {}
        )
        self._branches = dict(branches) if branches is not None else None
        self._closed_prs_for_head = (
            dict(closed_prs_for_head)
            if closed_prs_for_head is not None
            else None
        )
        self._branch_ahead_by = (
            dict(branch_ahead_by) if branch_ahead_by is not None else None
        )
        self._delete_branch_responses = (
            dict(delete_branch_responses)
            if delete_branch_responses is not None
            else None
        )
        #: Records every delete_branch_ref invocation for assertions.
        self.delete_branch_calls: list[dict[str, Any]] = []
        # Per-call argument records so tests can assert merge_pr was
        # invoked with the right method / delete_branch flags.
        self.merge_calls: list[dict[str, Any]] = []
        self.post_comment_calls: list[dict[str, Any]] = []
        self.close_calls: list[dict[str, Any]] = []
        self.approve_calls: list[dict[str, Any]] = []
        self.viewer_repo_permission_calls: list[dict[str, Any]] = []
        # Track call counts so tests can assert on coalescing
        self.call_count: dict[str, int] = {
            "authenticated_user": 0,
            "org_members": 0,
            "default_branch": 0,
            "my_open_prs": 0,
            "merge_pr": 0,
            "fetch_pr_comments": 0,
            "post_comment": 0,
            "close_pr": 0,
            "fetch_merge_commit_sha": 0,
            "fetch_check_runs": 0,
            "prefetch_default_branches": 0,
            "approve_pr": 0,
            "viewer_repo_permission": 0,
            "is_archived": 0,
            "list_branches": 0,
            "closed_prs_for_head": 0,
            "branch_ahead_by": 0,
            "delete_branch_ref": 0,
        }

    def authenticated_user(self, *, timeout: float | None = None) -> dict[str, Any]:
        self.call_count["authenticated_user"] += 1
        if self._user is None:
            raise GHError("FakeGHClient: authenticated_user not configured")
        return dict(self._user)

    def org_members(
        self, org: str, *, timeout: float | None = None
    ) -> list[str]:
        self.call_count["org_members"] += 1
        if self._org_members is None or org not in self._org_members:
            raise GHError(f"FakeGHClient: org_members({org!r}) not configured")
        return list(self._org_members[org])

    def default_branch(
        self, slug: str, *, timeout: float | None = None
    ) -> str:
        self.call_count["default_branch"] += 1
        if self._default_branches is None or slug not in self._default_branches:
            raise GHError(f"FakeGHClient: default_branch({slug!r}) not configured")
        return self._default_branches[slug]

    def prefetch_default_branches(
        self,
        slugs: Iterable[str],
        *,
        timeout: float | None = None,
        on_progress: "Callable[[int, int], None] | None" = None,
    ) -> None:
        """No-op in the fake: the configured ``default_branches`` map IS
        the cache. We still count the call so tests can assert the
        handler invoked the prefetch path, and fire ``on_progress`` once
        at completion so the handler's progress wiring is exercised."""
        self.call_count["prefetch_default_branches"] += 1
        if on_progress is not None:
            n = len(list(slugs))
            on_progress(n, n)

    def seed_default_branches(self, mapping: dict[str, str]) -> None:
        """Merge ``mapping`` into the fake's default-branches map (which
        doubles as its cache). Creates the map if it was unset."""
        if self._default_branches is None:
            self._default_branches = {}
        self._default_branches.update(mapping)

    def cached_default_branches(self) -> dict[str, str]:
        """Return a copy of the fake's configured default-branches map."""
        return dict(self._default_branches or {})

    def is_archived(self, slug: str, *, timeout: float | None = None) -> bool:
        self.call_count["is_archived"] += 1
        v = self._archived.get(slug)
        if isinstance(v, Exception):
            raise v
        return bool(v)

    def seed_archived(self, mapping: dict[str, bool]) -> None:
        self._archived.update(mapping)

    def cached_archived(self) -> dict[str, bool]:
        """Return a copy of the archived map, booleans only (Exception
        entries — used to exercise the error path — are excluded so they
        never reach the on-disk cache)."""
        return {k: v for k, v in self._archived.items() if isinstance(v, bool)}

    def my_open_prs(
        self,
        slugs: Iterable[str] | None = None,
        *,
        author: str | None = "@me",
        timeout: float | None = None,
    ) -> dict[str, list[PRInfo]]:
        self.call_count["my_open_prs"] += 1
        self.last_my_open_prs_author = author  # for test assertions
        if self._my_open_prs is None:
            raise GHError("FakeGHClient: my_open_prs not configured")
        if slugs is None:
            return {k: list(v) for k, v in self._my_open_prs.items()}
        slugs_set = set(slugs)
        return {
            slug: list(self._my_open_prs.get(slug, []))
            for slug in slugs_set
        }

    def merge_pr(
        self,
        slug: str,
        number: int,
        *,
        method: Literal["merge", "squash", "rebase"] = "merge",
        delete_branch: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.call_count["merge_pr"] += 1
        self.merge_calls.append(
            {
                "slug": slug,
                "number": number,
                "method": method,
                "delete_branch": delete_branch,
                "timeout": timeout,
            }
        )
        if self._merge_responses is None:
            raise GHError(
                f"FakeGHClient: merge_pr({slug!r}, {number}) not configured"
            )
        key = (slug, number)
        if key not in self._merge_responses:
            raise GHError(
                f"FakeGHClient: merge_pr({slug!r}, {number}) not configured"
            )
        outcome = self._merge_responses[key]
        if isinstance(outcome, Exception):
            raise outcome
        return dict(outcome)

    def approve_pr(
        self,
        slug: str,
        number: int,
        *,
        body: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.call_count["approve_pr"] += 1
        self.approve_calls.append(
            {
                "slug": slug,
                "number": number,
                "body": body,
                "timeout": timeout,
            }
        )
        if self._approve_responses is None:
            raise GHError(
                f"FakeGHClient: approve_pr({slug!r}, {number}) not configured"
            )
        key = (slug, number)
        if key not in self._approve_responses:
            raise GHError(
                f"FakeGHClient: approve_pr({slug!r}, {number}) not configured"
            )
        outcome = self._approve_responses[key]
        if isinstance(outcome, Exception):
            raise outcome
        return dict(outcome)

    def viewer_repo_permission(
        self, slug: str, *, timeout: float | None = None
    ) -> str:
        self.call_count["viewer_repo_permission"] += 1
        self.viewer_repo_permission_calls.append(
            {"slug": slug, "timeout": timeout}
        )
        if self._repo_permissions is None or slug not in self._repo_permissions:
            raise GHError(
                f"FakeGHClient: viewer_repo_permission({slug!r}) not configured"
            )
        return self._repo_permissions[slug]

    def fetch_pr_comments(
        self,
        slug: str,
        number: int,
        *,
        timeout: float | None = None,
    ) -> list[PRComment]:
        self.call_count["fetch_pr_comments"] += 1
        if self._pr_comments is None:
            raise GHError(
                f"FakeGHClient: fetch_pr_comments({slug!r}, {number}) not configured"
            )
        # Missing keys default to empty list — distinct from "not configured"
        # at all (which is the bare-FakeGHClient case). Lets a test set
        # pr_comments={} to mean "no PR has any comments."
        return list(self._pr_comments.get((slug, number), []))

    def post_comment(
        self,
        slug: str,
        number: int,
        body: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.call_count["post_comment"] += 1
        self.post_comment_calls.append(
            {
                "slug": slug,
                "number": number,
                "body": body,
                "timeout": timeout,
            }
        )
        if self._post_comment_responses is None:
            raise GHError(
                f"FakeGHClient: post_comment({slug!r}, {number}) not configured"
            )
        key = (slug, number)
        if key not in self._post_comment_responses:
            raise GHError(
                f"FakeGHClient: post_comment({slug!r}, {number}) not configured"
            )
        outcome = self._post_comment_responses[key]
        if isinstance(outcome, Exception):
            raise outcome
        return dict(outcome)

    def close_pr(
        self,
        slug: str,
        number: int,
        *,
        delete_branch: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.call_count["close_pr"] += 1
        self.close_calls.append(
            {
                "slug": slug,
                "number": number,
                "delete_branch": delete_branch,
                "timeout": timeout,
            }
        )
        if self._close_responses is None:
            raise GHError(
                f"FakeGHClient: close_pr({slug!r}, {number}) not configured"
            )
        key = (slug, number)
        if key not in self._close_responses:
            raise GHError(
                f"FakeGHClient: close_pr({slug!r}, {number}) not configured"
            )
        outcome = self._close_responses[key]
        if isinstance(outcome, Exception):
            raise outcome
        return dict(outcome)

    def fetch_merge_commit_sha(
        self,
        slug: str,
        number: int,
        *,
        timeout: float | None = None,
    ) -> str | None:
        self.call_count["fetch_merge_commit_sha"] += 1
        if self._merge_commit_shas is None:
            raise GHError(
                f"FakeGHClient: fetch_merge_commit_sha({slug!r}, {number}) "
                "not configured"
            )
        # Missing keys default to None (a PR that didn't end up with a
        # merge commit, e.g. closed unmerged).
        return self._merge_commit_shas.get((slug, number))

    def fetch_check_runs(
        self,
        slug: str,
        sha: str,
        *,
        timeout: float | None = None,
    ) -> list[CheckRun]:
        self.call_count["fetch_check_runs"] += 1
        if self._check_runs is None:
            raise GHError(
                f"FakeGHClient: fetch_check_runs({slug!r}, {sha[:7]}) "
                "not configured"
            )
        return list(self._check_runs.get((slug, sha), []))

    def list_branches(
        self, slug: str, *, timeout: float | None = None
    ) -> list[BranchRef]:
        self.call_count["list_branches"] += 1
        if self._branches is None or slug not in self._branches:
            raise GHError(f"FakeGHClient: list_branches({slug!r}) not configured")
        value = self._branches[slug]
        if isinstance(value, Exception):
            raise value
        return list(value)

    def closed_prs_for_head(
        self, slug: str, head_ref: str, *, timeout: float | None = None
    ) -> list[ClosedPRRef]:
        self.call_count["closed_prs_for_head"] += 1
        if (
            self._closed_prs_for_head is None
            or (slug, head_ref) not in self._closed_prs_for_head
        ):
            raise GHError(
                f"FakeGHClient: closed_prs_for_head({slug!r}, {head_ref!r}) "
                "not configured"
            )
        value = self._closed_prs_for_head[(slug, head_ref)]
        if isinstance(value, Exception):
            raise value
        return list(value)

    def branch_ahead_by(
        self, slug: str, base: str, branch: str, *, timeout: float | None = None
    ) -> int:
        self.call_count["branch_ahead_by"] += 1
        if (
            self._branch_ahead_by is None
            or (slug, base, branch) not in self._branch_ahead_by
        ):
            raise GHError(
                f"FakeGHClient: branch_ahead_by({slug!r}, {base!r}, "
                f"{branch!r}) not configured"
            )
        value = self._branch_ahead_by[(slug, base, branch)]
        if isinstance(value, Exception):
            raise value
        return value

    def delete_branch_ref(
        self, slug: str, branch: str, *, timeout: float | None = None
    ) -> None:
        self.call_count["delete_branch_ref"] += 1
        self.delete_branch_calls.append({"slug": slug, "branch": branch})
        if (
            self._delete_branch_responses is not None
            and (slug, branch) in self._delete_branch_responses
        ):
            value = self._delete_branch_responses[(slug, branch)]
            if isinstance(value, Exception):
                raise value
            return None
        # Unconfigured delete defaults to success — tests that care about
        # failure configure an Exception explicitly (mirrors merge_responses
        # ergonomics inverted: deletes are the common happy path).
        return None


# ─── ProductionGHClient ─────────────────────────────────────────────────────


#: Substrings (case-insensitive) in gh stderr that mark a transient failure
#: worth retrying. See node ``ghclmp7n.d`` — the retry policy is hardcoded
#: conservative and not configurable at call sites.
_RETRYABLE_STDERR_MARKERS: tuple[str, ...] = (
    "rate limit",
    "5xx",
    # gh emits things like "HTTP 502" / "HTTP 503" on bad-gateway and
    # service-unavailable; the literal "5xx" marker above doesn't catch
    # those. Discovered 2026-05-29 while batching 200+ default-branch
    # lookups: ~25% of chunks were silently failing on HTTP 502 without
    # ever retrying because none of the other markers matched.
    "http 50",
    "timeout",
    "could not resolve",
    "eof",
)


def _is_retryable_stderr(stderr: str) -> bool:
    """True if ``stderr`` matches one of the retryable patterns."""
    low = stderr.lower()
    return any(marker in low for marker in _RETRYABLE_STDERR_MARKERS)


#: GraphQL document used by ``my_open_prs``. Lives at module scope so the
#: test suite can assert on the exact wire shape and so the verification
#: comment at the call site stays close to the call, not the literal.
#: Window size for the per-PR timelineItems walk. Picked at 50 because a
#: typical merge-ready PR has well under a dozen ready/draft/review events
#: in its lifetime; 50 leaves headroom while keeping per-PR GraphQL cost
#: roughly constant. ``pageInfo.hasPreviousPage`` signals truncation so the
#: ``ready`` module can fall back to ``last_pushed_at`` defensively.
_TIMELINE_WINDOW = 50

#: Window size for reviewThreads. The PR-level invariant only consults the
#: count of currently-unresolved threads; 100 is GraphQL's default cap and
#: more than enough for any normal PR.
_REVIEW_THREADS_WINDOW = 100

#: Repos per coalesced search query. GitHub's search silently caps how
#: many ``repo:`` qualifiers it honors in one query (and may truncate
#: rather than error), so we chunk to stay well clear of the limit.
#: 50 matches the default-branch prefetch chunk size and is comfortably
#: under any observed ceiling.
_OPEN_PRS_REPO_CHUNK = 50

_MY_OPEN_PRS_GRAPHQL = """\
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number
        title
        url
        author { login }
        isDraft
        state
        baseRefName
        headRefName
        headRefOid
        createdAt
        updatedAt
        mergeStateStatus
        reviewDecision
        labels(first: 20) { nodes { name } }
        commits(last: 1) {
          nodes {
            commit {
              committedDate
              statusCheckRollup { state }
            }
          }
        }
        reviewThreads(first: 100) {
          nodes { isResolved }
        }
        timelineItems(
          last: 50,
          itemTypes: [
            READY_FOR_REVIEW_EVENT,
            CONVERT_TO_DRAFT_EVENT,
            PULL_REQUEST_REVIEW
          ]
        ) {
          nodes {
            __typename
            ... on ReadyForReviewEvent { createdAt }
            ... on ConvertToDraftEvent { createdAt }
            ... on PullRequestReview { submittedAt state }
          }
          pageInfo { hasPreviousPage }
        }
        repository { nameWithOwner }
      }
    }
  }
}
"""


def _parse_iso8601(value: str) -> datetime:
    """Parse a GitHub ISO-8601 timestamp (always ends in ``Z``) into an
    aware UTC ``datetime``. Kept local to gh.py because the rest of
    gitbulk has no need for ISO parsing yet."""
    # GitHub always emits a trailing 'Z'; fromisoformat doesn't accept
    # 'Z' until 3.11, so normalize to '+00:00' for the 3.10 baseline.
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


class ProductionGHClient:
    """Real :class:`GHClient` implementation that subprocesses to ``gh``.

    Stateless per node ``ghclmp7n.e``: no per-client cache of auth,
    rate-limit headers, or org membership. Each call shells out fresh.

    Constructor knobs (all keyword-only):
      - ``gh_path``: path to the ``gh`` executable. Default ``"gh"`` is
        resolved through ``shutil.which`` at construction time to an
        absolute path; that absolute path is then used for every
        subsequent invocation. This closes the security-hawk F2 PATH-
        hijack risk: a later ``PATH``-prepend cannot substitute ``gh``
        once the client has been constructed. (A passed absolute path
        is used as-is, without ``which`` lookup.)
      - ``default_timeout``: per-call timeout in seconds when the caller
        passes ``timeout=None``. Default 30s, matching node ghclmp7n.d.
      - ``max_retries``: max attempts (including the initial try) for
        transient failures. Default 3.

    Raises :class:`GHError` immediately if ``gh_path`` does not resolve
    to an executable.
    """

    def __init__(
        self,
        *,
        gh_path: str = "gh",
        default_timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        import shutil

        if Path(gh_path).is_absolute():
            resolved = gh_path
        else:
            resolved_path = shutil.which(gh_path)
            if resolved_path is None:
                raise GHError(
                    f"could not find {gh_path!r} on PATH; "
                    "set gh_path to an absolute path or install gh"
                )
            resolved = resolved_path
        self._gh_path = resolved
        self._default_timeout = default_timeout
        self._max_retries = max_retries
        #: Populated by :meth:`prefetch_default_branches`. Subsequent
        #: ``default_branch`` calls consult this first; on miss they
        #: fall back to a per-slug REST call. Per-process only — not
        #: persisted to disk in this stage.
        self._default_branch_cache: dict[str, str] = {}
        #: Archived status, populated by the same coalesced prefetch
        #: (``isArchived`` is selected alongside ``defaultBranchRef``).
        #: ``is_archived`` consults this first and falls back to REST on
        #: a miss. Seeded from disk by ``default_branch_cache``.
        self._archived_cache: dict[str, bool] = {}

    # ─── private helpers ───────────────────────────────────────────────

    def _run(
        self,
        args: tuple[str, ...],
        *,
        timeout: float | None,
    ) -> str:
        """Run ``gh <args>`` with retry + timeout discipline.

        Returns captured stdout on success. Raises:
          - :class:`GHTimeoutError` if the final attempt timed out.
          - :class:`GHError` for non-zero exits (immediate raise on
            non-retryable stderr; raise after exhaustion on retryable).
        """
        effective_timeout = (
            timeout if timeout is not None else self._default_timeout
        )
        command: tuple[str, ...] = (self._gh_path,) + args
        last_stderr = ""
        last_was_timeout = False

        for attempt in range(self._max_retries):
            try:
                completed = subprocess.run(
                    list(command),
                    capture_output=True,
                    text=True,
                    timeout=effective_timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                last_stderr = (
                    f"timeout after {effective_timeout}s: {exc}"
                )
                last_was_timeout = True
            else:
                if completed.returncode == 0:
                    return completed.stdout
                last_stderr = completed.stderr or ""
                last_was_timeout = False
                if not _is_retryable_stderr(last_stderr):
                    raise GHError(
                        f"gh failed: {last_stderr.strip()}",
                        command=command,
                    )

            # Retryable path: sleep with exponential backoff before next
            # attempt, but not after the final attempt.
            if attempt < self._max_retries - 1:
                time.sleep(2 ** attempt)

        # Exhausted all attempts on retryable / timeout failures.
        message = (
            f"gh exhausted {self._max_retries} attempts: "
            f"{last_stderr.strip()}"
        )
        if last_was_timeout:
            raise GHTimeoutError(message, command=command)
        raise GHError(message, command=command)

    # ─── Protocol methods ──────────────────────────────────────────────

    def authenticated_user(
        self, *, timeout: float | None = None
    ) -> dict[str, Any]:
        # verified non-deprecated against gh CLI 2026-05-28
        stdout = self._run(("api", "user"), timeout=timeout)
        return json.loads(stdout)

    def org_members(
        self, org: str, *, timeout: float | None = None
    ) -> list[str]:
        # verified non-deprecated against gh CLI 2026-05-28
        stdout = self._run(
            (
                "api",
                f"orgs/{org}/members",
                "--paginate",
                "--jq",
                ".[].login",
            ),
            timeout=timeout,
        )
        return [line.strip() for line in stdout.splitlines() if line.strip()]

    def default_branch(
        self, slug: str, *, timeout: float | None = None
    ) -> str:
        # In-process cache populated by prefetch_default_branches.
        # Cache miss falls through to the per-slug REST call below.
        cached = self._default_branch_cache.get(slug)
        if cached is not None:
            return cached
        # verified non-deprecated against gh CLI 2026-05-28
        stdout = self._run(
            ("api", f"repos/{slug}", "--jq", ".default_branch"),
            timeout=timeout,
        )
        return stdout.strip()

    def seed_default_branches(self, mapping: dict[str, str]) -> None:
        self._default_branch_cache.update(mapping)

    def cached_default_branches(self) -> dict[str, str]:
        return dict(self._default_branch_cache)

    def is_archived(self, slug: str, *, timeout: float | None = None) -> bool:
        # In-process cache populated by prefetch_default_branches.
        # Cache miss falls through to the per-slug REST call below.
        cached = self._archived_cache.get(slug)
        if cached is not None:
            return cached
        # verified non-deprecated against gh CLI 2026-06-02
        stdout = self._run(
            ("api", f"repos/{slug}", "--jq", ".archived"),
            timeout=timeout,
        )
        return stdout.strip().lower() == "true"

    def seed_archived(self, mapping: dict[str, bool]) -> None:
        self._archived_cache.update(mapping)

    def cached_archived(self) -> dict[str, bool]:
        return dict(self._archived_cache)

    def prefetch_default_branches(
        self,
        slugs: Iterable[str],
        *,
        timeout: float | None = None,
        on_progress: "Callable[[int, int], None] | None" = None,
    ) -> None:
        # verified non-deprecated against gh CLI 2026-05-28
        # GraphQL with aliased repository() nodes lets us look up N
        # default branches per round-trip. We chunk at _CHUNK because
        # GitHub returns HTTP 502 once the per-request query cost gets too
        # high. Each node now selects ``defaultBranchRef { name }`` AND
        # ``isArchived``; that extra field raised per-node cost enough that
        # 100-node chunks began 502-ing (verified 2026-06-02 against the
        # 204-repo fleet: 100 nodes → HTTP 502, 50 nodes → clean ~5s). So
        # the chunk size is 50, not 100. See node ghclmp7n.c.
        slug_list = [s for s in slugs if "/" in s]
        total = len(slug_list)
        if total == 0:
            return
        _CHUNK = 50
        done = 0
        for start in range(0, total, _CHUNK):
            chunk = slug_list[start : start + _CHUNK]
            self._prefetch_default_branches_chunk(chunk, timeout=timeout)
            done += len(chunk)
            if on_progress is not None:
                on_progress(done, total)

    def _prefetch_default_branches_chunk(
        self,
        chunk: list[str],
        *,
        timeout: float | None,
    ) -> None:
        """One GraphQL round-trip for up to ``_CHUNK`` slugs.

        Bypasses :meth:`_run` because GraphQL's partial-success semantics
        don't fit it: a chunk with one unknown repo (deleted, renamed,
        transferred) returns ``data`` with the other aliases populated
        AND a non-zero exit code AND an ``errors`` field. ``_run`` would
        raise on the non-zero exit, discarding the good data. We parse
        whatever stdout gives us. Retries are inlined because we still
        want to handle transient 5xx, but they're capped at 3 attempts
        with exponential backoff matching ``_run``'s convention.
        """
        aliases: dict[str, str] = {}
        body_lines: list[str] = []
        for i, slug in enumerate(chunk):
            owner, name = slug.split("/", 1)
            alias = f"r{i}"
            aliases[alias] = slug
            # Owner and name are validated by load_repos's slug regex,
            # so they're safe to interpolate. (Defense in depth: the
            # regex rejects quotes and backslashes.)
            body_lines.append(
                f'  {alias}: repository(owner: "{owner}", name: "{name}") '
                "{ defaultBranchRef { name } isArchived }"
            )
        query = "query {\n" + "\n".join(body_lines) + "\n}\n"
        argv = (self._gh_path, "api", "graphql", "-f", f"query={query}")
        effective_timeout = (
            timeout if timeout is not None else self._default_timeout
        )
        stdout = ""
        for attempt in range(self._max_retries):
            try:
                completed = subprocess.run(
                    list(argv),
                    capture_output=True,
                    text=True,
                    timeout=effective_timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                if attempt < self._max_retries - 1:
                    time.sleep(2 ** attempt)
                continue
            # Partial-success: GraphQL may set rc=1 because some aliases
            # couldn't be resolved (deleted repo) BUT still return valid
            # data for the rest. Accept stdout if it parses as JSON
            # regardless of returncode.
            if completed.stdout:
                stdout = completed.stdout
                break
            # No stdout at all → check stderr for transient and retry,
            # else give up on this chunk.
            if (
                attempt < self._max_retries - 1
                and _is_retryable_stderr(completed.stderr or "")
            ):
                time.sleep(2 ** attempt)
                continue
            return
        if not stdout:
            return
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return
        data = payload.get("data") or {}
        for alias, slug in aliases.items():
            node = data.get(alias)
            if not isinstance(node, dict):
                continue
            # Archived status is recorded independently of the branch name:
            # an archived (or empty) repo may have a null defaultBranchRef
            # yet still report isArchived.
            archived = node.get("isArchived")
            if isinstance(archived, bool):
                self._archived_cache[slug] = archived
            ref = node.get("defaultBranchRef")
            if not isinstance(ref, dict):
                continue
            name = ref.get("name")
            if isinstance(name, str) and name:
                self._default_branch_cache[slug] = name

    def _search_all_pages(
        self,
        search_terms: list[str],
        *,
        timeout: float | None,
    ) -> list[dict[str, Any]]:
        """Run a search query, following ``pageInfo`` until exhausted.

        Returns the flat list of result nodes across all pages. Each
        page is ``first: 100``; without this loop a fleet with >100
        matching PRs would silently lose the overflow (the original
        single-page bug). A missing ``pageInfo`` (older fixtures, or a
        backend that omits it) is treated as a single page.
        """
        search_string = " ".join(search_terms)
        nodes: list[dict[str, Any]] = []
        cursor: str | None = None
        # Hard page cap as a runaway guard: 100 pages × 100 = 10k PRs,
        # far beyond any real fleet. Prevents an infinite loop if the
        # backend ever returns hasNextPage=true with a stuck cursor.
        for _ in range(100):
            argv = [
                "api",
                "graphql",
                "-F",
                f"q={search_string}",
            ]
            if cursor is not None:
                # Raw string (-f): cursors are opaque base64 and must
                # not be type-coerced by -F.
                argv.extend(["-f", f"after={cursor}"])
            argv.extend(["-f", f"query={_MY_OPEN_PRS_GRAPHQL}"])
            stdout = self._run(tuple(argv), timeout=timeout)
            search = json.loads(stdout).get("data", {}).get("search", {}) or {}
            nodes.extend(search.get("nodes", []) or [])
            page_info = search.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                # hasNextPage true but no cursor — backend inconsistency;
                # stop rather than loop forever.
                break
        return nodes

    def my_open_prs(
        self,
        slugs: Iterable[str] | None = None,
        *,
        author: str | None = "@me",
        timeout: float | None = None,
    ) -> dict[str, list[PRInfo]]:
        # verified non-deprecated against gh CLI 2026-05-28
        # author=None → no author: qualifier (any author); otherwise add
        # the qualifier. The qualifier goes first so the query reads
        # naturally and the existing argv-assertion tests still match
        # when author is the default "@me".
        base_terms = ["is:open", "is:pr"]
        if author is not None:
            base_terms.insert(0, f"author:{author}")
        grouped: dict[str, list[PRInfo]] = {}

        if slugs is None:
            # No repo filter: one paginated search across all my PRs.
            node_batches = [self._search_all_pages(base_terms, timeout=timeout)]
        else:
            slug_list = list(slugs)
            # Pre-seed every requested slug so repos with no PRs still
            # appear (matches FakeGHClient.my_open_prs semantics).
            for slug in slug_list:
                grouped.setdefault(slug, [])
            # Chunk the repo: qualifiers. GitHub silently caps how many
            # it honors in one query, so coalescing all 200+ into one
            # search would drop repos without any error. Each chunk is
            # independently paginated.
            node_batches = []
            for start in range(0, len(slug_list), _OPEN_PRS_REPO_CHUNK):
                chunk = slug_list[start : start + _OPEN_PRS_REPO_CHUNK]
                terms = base_terms + [f"repo:{s}" for s in chunk]
                node_batches.append(
                    self._search_all_pages(terms, timeout=timeout)
                )

        for nodes in node_batches:
            for node in nodes:
                if not node:
                    # GraphQL returns nulls for non-PullRequest items
                    # (defensive — is:pr filters them, but the field is
                    # still nullable).
                    continue
                pr = _pr_info_from_graphql_node(node)
                grouped.setdefault(pr.slug, []).append(pr)

        return grouped


    def merge_pr(
        self,
        slug: str,
        number: int,
        *,
        method: Literal["merge", "squash", "rebase"] = "merge",
        delete_branch: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        # verified non-deprecated against gh CLI 2026-05-28
        # (`gh pr merge --help` shows -s/-m/-r and -d/--delete-branch,
        # no deprecation warnings; the .agent-bin shim blocks this call
        # when run from an AI-agent shell — production code constructs
        # the right argv regardless.)
        method_flag = {
            "merge": "--merge",
            "squash": "--squash",
            "rebase": "--rebase",
        }[method]
        args: list[str] = [
            "pr",
            "merge",
            str(number),
            "--repo",
            slug,
            method_flag,
        ]
        if delete_branch:
            args.append("--delete-branch")
        stdout = self._run(tuple(args), timeout=timeout)
        # gh pr merge prints a human line on success and emits no JSON.
        # Tolerate empty stdout (most common) by returning {}; if a future
        # gh version emits JSON, parse it.
        stripped = stdout.strip()
        if not stripped:
            return {}
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return {"stdout": stripped}

    def approve_pr(
        self,
        slug: str,
        number: int,
        *,
        body: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        # verified non-deprecated against gh CLI 2026-05-30
        # (`gh pr review --help` shows -a/--approve and -b/--body with no
        # deprecation warnings; `gh pr review --help 2>&1 1>/dev/null |
        # grep -iE 'warning|deprecat'` is empty. The .agent-bin shim does
        # NOT block `gh pr review`; only `gh pr merge`/`gh repo delete`.)
        args: list[str] = [
            "pr",
            "review",
            str(number),
            "--repo",
            slug,
            "--approve",
        ]
        if body is not None:
            args.extend(["--body", body])
        stdout = self._run(tuple(args), timeout=timeout)
        stripped = stdout.strip()
        if not stripped:
            return {}
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return {"stdout": stripped}

    def viewer_repo_permission(
        self, slug: str, *, timeout: float | None = None
    ) -> str:
        # verified non-deprecated against gh CLI 2026-05-30
        # (`gh api repos/<slug> --jq .permissions` returns the current
        # {admin,maintain,push,triage,pull} boolean shape; verified live
        # against provenant-dev/origin-sip-policy-lib with no deprecation
        # warning on stderr.)
        stdout = self._run(
            ("api", f"repos/{slug}", "--jq", ".permissions"),
            timeout=timeout,
        )
        try:
            perms = json.loads(stdout)
        except json.JSONDecodeError:
            return "none"
        if not isinstance(perms, dict):
            return "none"
        # Highest-to-lowest: GitHub returns all lower booleans true too,
        # so we check from the most privileged down and return the first.
        if perms.get("admin"):
            return "admin"
        if perms.get("maintain"):
            return "maintain"
        if perms.get("push"):
            return "write"
        if perms.get("triage"):
            return "triage"
        if perms.get("pull"):
            return "read"
        return "none"

    def fetch_pr_comments(
        self,
        slug: str,
        number: int,
        *,
        timeout: float | None = None,
    ) -> list[PRComment]:
        # verified non-deprecated against gh CLI 2026-05-28
        owner, name = slug.split("/", 1)
        stdout = self._run(
            (
                "api",
                "graphql",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
                "-F",
                f"number={number}",
                "-f",
                f"query={_PR_COMMENTS_GRAPHQL}",
            ),
            timeout=timeout,
        )
        payload = json.loads(stdout)
        repo_obj = (payload.get("data") or {}).get("repository") or {}
        pr_obj = repo_obj.get("pullRequest") or {}
        comments_obj = pr_obj.get("comments") or {}
        nodes = comments_obj.get("nodes") or []
        out: list[PRComment] = []
        for n in nodes:
            if not n:
                continue
            author = (n.get("author") or {}).get("login") or ""
            body = n.get("body") or ""
            created = n.get("createdAt")
            if not created:
                continue
            out.append(
                PRComment(author_login=author, body=body, at=_parse_iso8601(created))
            )
        return out

    def post_comment(
        self,
        slug: str,
        number: int,
        body: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        # verified non-deprecated against gh CLI 2026-05-28
        # (`gh pr comment --help` shows -b/--body, no deprecation warnings)
        stdout = self._run(
            (
                "pr",
                "comment",
                str(number),
                "--repo",
                slug,
                "--body",
                body,
            ),
            timeout=timeout,
        )
        stripped = stdout.strip()
        if not stripped:
            return {}
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return {"stdout": stripped}

    def close_pr(
        self,
        slug: str,
        number: int,
        *,
        delete_branch: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        # verified non-deprecated against gh CLI 2026-05-28
        # (`gh pr close --help` shows --delete-branch, no deprecation warnings)
        args: list[str] = [
            "pr",
            "close",
            str(number),
            "--repo",
            slug,
        ]
        if delete_branch:
            args.append("--delete-branch")
        stdout = self._run(tuple(args), timeout=timeout)
        stripped = stdout.strip()
        if not stripped:
            return {}
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return {"stdout": stripped}

    def fetch_merge_commit_sha(
        self,
        slug: str,
        number: int,
        *,
        timeout: float | None = None,
    ) -> str | None:
        # verified non-deprecated against gh CLI 2026-05-28
        # (`gh pr view --json mergeCommit` works against current gh)
        stdout = self._run(
            (
                "pr",
                "view",
                str(number),
                "--repo",
                slug,
                "--json",
                "mergeCommit",
            ),
            timeout=timeout,
        )
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return None
        mc = payload.get("mergeCommit")
        if not mc:
            return None
        return mc.get("oid")

    def fetch_check_runs(
        self,
        slug: str,
        sha: str,
        *,
        timeout: float | None = None,
    ) -> list[CheckRun]:
        # verified non-deprecated against gh CLI 2026-05-28
        # (REST endpoint /repos/<slug>/commits/<sha>/check-runs)
        stdout = self._run(
            (
                "api",
                f"repos/{slug}/commits/{sha}/check-runs",
                "--jq",
                ".check_runs[] | {name, status, conclusion, details_url, completed_at}",
            ),
            timeout=timeout,
        )
        out: list[CheckRun] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            completed_at_raw = row.get("completed_at")
            completed_at = _parse_iso8601(completed_at_raw) if completed_at_raw else None
            out.append(
                CheckRun(
                    name=row.get("name") or "",
                    status=row.get("status") or "",
                    conclusion=row.get("conclusion"),
                    details_url=row.get("details_url") or "",
                    completed_at=completed_at,
                )
            )
        return out

    # ─── prune surface (nodes prnbr4kq / prnwt5nq) ─────────────────────────

    def list_branches(
        self, slug: str, *, timeout: float | None = None
    ) -> list[BranchRef]:
        # verified non-deprecated against gh CLI 2026-06-03
        # (REST /repos/<slug>/branches — current, returns name/commit.sha/
        # protected; --paginate merges Link-header pages)
        stdout = self._run(
            (
                "api",
                f"repos/{slug}/branches",
                "--paginate",
                "--jq",
                ".[] | {name, sha: .commit.sha, protected}",
            ),
            timeout=timeout,
        )
        out: list[BranchRef] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            out.append(
                BranchRef(
                    name=row.get("name") or "",
                    sha=row.get("sha") or "",
                    protected=bool(row.get("protected")),
                )
            )
        return out

    def closed_prs_for_head(
        self, slug: str, head_ref: str, *, timeout: float | None = None
    ) -> list[ClosedPRRef]:
        # verified non-deprecated against gh CLI 2026-06-03
        # (REST /repos/<slug>/pulls?state=closed&head=<owner>:<ref> —
        # head filter scopes to the upstream owner so fork PRs sharing a
        # branch name are excluded. jq tolerates a null head.repo (deleted
        # fork) by returning null for .head.repo.full_name.)
        owner = slug.split("/", 1)[0]
        stdout = self._run(
            (
                "api",
                f"repos/{slug}/pulls?state=closed&head={owner}:{head_ref}"
                "&per_page=100",
                "--paginate",
                "--jq",
                ".[] | {number, title, url: .html_url, merged_at, "
                "closed_at, base_ref: .base.ref, head_ref: .head.ref, "
                "head_sha: .head.sha, head_repo: .head.repo.full_name}",
            ),
            timeout=timeout,
        )
        out: list[ClosedPRRef] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            merged_at = row.get("merged_at")
            closed_at = row.get("closed_at")
            merged = merged_at is not None
            # closed_at is always set for a closed PR; fall back to
            # merged_at defensively if GitHub ever omits it.
            stamp = merged_at if merged else closed_at
            if stamp is None:
                # No usable timestamp → cannot grace-check → skip the row
                # (the prune handler treats an absent ref as "no closed PR").
                continue
            out.append(
                ClosedPRRef(
                    number=row.get("number") or 0,
                    title=row.get("title") or "",
                    url=row.get("url") or "",
                    merged=merged,
                    base_ref=row.get("base_ref") or "",
                    head_ref=row.get("head_ref") or "",
                    head_sha=row.get("head_sha") or "",
                    head_repo_slug=row.get("head_repo"),
                    closed_at=_parse_iso8601(stamp),
                )
            )
        return out

    def branch_ahead_by(
        self, slug: str, base: str, branch: str, *, timeout: float | None = None
    ) -> int:
        # verified non-deprecated against gh CLI 2026-06-03
        # (REST /repos/<slug>/compare/<base>...<head> — .ahead_by is the
        # count of commits in <head> not reachable from <base>)
        stdout = self._run(
            (
                "api",
                f"repos/{slug}/compare/{base}...{branch}",
                "--jq",
                ".ahead_by",
            ),
            timeout=timeout,
        )
        stripped = stdout.strip()
        try:
            return int(stripped)
        except ValueError as exc:
            raise GHError(
                f"branch_ahead_by({slug!r}, {base!r}, {branch!r}): "
                f"unexpected ahead_by value {stripped!r}"
            ) from exc

    def delete_branch_ref(
        self, slug: str, branch: str, *, timeout: float | None = None
    ) -> None:
        # verified non-deprecated against gh CLI 2026-06-03
        # (REST DELETE /repos/<slug>/git/refs/heads/<branch> — the
        # documented programmatic branch-deletion path, node prdel4rq.
        # The .agent-bin shim blocks `git push --delete`, not this.)
        self._run(
            (
                "api",
                "-X",
                "DELETE",
                f"repos/{slug}/git/refs/heads/{branch}",
            ),
            timeout=timeout,
        )


#: GraphQL document used by :meth:`ProductionGHClient.fetch_pr_comments`.
#: Returns the last 50 issue-comments on a PR (oldest first within the slice).
_PR_COMMENTS_GRAPHQL = """\
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      comments(last: 50) {
        nodes {
          author { login }
          body
          createdAt
        }
      }
    }
  }
}
"""


def _pr_info_from_graphql_node(node: dict[str, Any]) -> PRInfo:
    """Translate one GraphQL ``PullRequest`` node into a :class:`PRInfo`.

    Pulled out of :class:`ProductionGHClient` so it can be unit-tested in
    isolation against fixture JSON. Tolerates missing optional fields
    (``statusCheckRollup`` is null when no checks have run; ``author`` is
    null for deleted users) by mapping them to ``None``.
    """
    slug = node["repository"]["nameWithOwner"]
    author_obj = node.get("author") or {}
    author = author_obj.get("login", "")

    labels_obj = node.get("labels") or {}
    label_nodes = labels_obj.get("nodes") or []
    labels = tuple(n["name"] for n in label_nodes if n and n.get("name"))

    last_pushed_at: datetime | None = None
    checks_status: str | None = None
    commits_obj = node.get("commits") or {}
    commit_nodes = commits_obj.get("nodes") or []
    if commit_nodes:
        commit = commit_nodes[0].get("commit") or {}
        committed_date = commit.get("committedDate")
        if committed_date:
            last_pushed_at = _parse_iso8601(committed_date)
        rollup = commit.get("statusCheckRollup")
        if rollup:
            checks_status = rollup.get("state")

    review_threads_obj = node.get("reviewThreads") or {}
    thread_nodes = review_threads_obj.get("nodes") or []
    unresolved_thread_count = sum(
        1
        for t in thread_nodes
        if t is not None and not t.get("isResolved", False)
    )

    timeline_obj = node.get("timelineItems") or {}
    timeline_nodes = timeline_obj.get("nodes") or []
    page_info = timeline_obj.get("pageInfo") or {}
    timeline_capped = bool(page_info.get("hasPreviousPage", False))
    timeline_events: list[TimelineEvent] = []
    for item in timeline_nodes:
        if not item:
            continue
        typename = item.get("__typename")
        if typename == "ReadyForReviewEvent":
            at = item.get("createdAt")
            if at:
                timeline_events.append(
                    TimelineEvent(kind="ready", at=_parse_iso8601(at))
                )
        elif typename == "ConvertToDraftEvent":
            at = item.get("createdAt")
            if at:
                timeline_events.append(
                    TimelineEvent(kind="draft", at=_parse_iso8601(at))
                )
        elif typename == "PullRequestReview":
            state = item.get("state")
            at = item.get("submittedAt")
            if not at:
                # Pending reviews have no submittedAt yet; not a timeline
                # signal until submitted.
                continue
            if state == "APPROVED":
                timeline_events.append(
                    TimelineEvent(kind="approved", at=_parse_iso8601(at))
                )
            elif state == "CHANGES_REQUESTED":
                timeline_events.append(
                    TimelineEvent(
                        kind="changes_requested", at=_parse_iso8601(at)
                    )
                )
            # COMMENTED / DISMISSED / PENDING reviews are not gates.

    return PRInfo(
        slug=slug,
        number=node["number"],
        title=node["title"],
        url=node["url"],
        author=author,
        base_ref=node["baseRefName"],
        head_ref=node["headRefName"],
        head_sha=node["headRefOid"],
        state=node["state"],
        is_draft=node["isDraft"],
        mergeable_state=node.get("mergeStateStatus"),
        created_at=_parse_iso8601(node["createdAt"]),
        updated_at=_parse_iso8601(node["updatedAt"]),
        last_pushed_at=last_pushed_at,
        labels=labels,
        review_decision=node.get("reviewDecision"),
        checks_status=checks_status,
        unresolved_thread_count=unresolved_thread_count,
        timeline_events=tuple(timeline_events),
        timeline_capped=timeline_capped,
    )
