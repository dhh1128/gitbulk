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

from datetime import datetime
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

from gitbulk.pr_info import PRInfo


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

    def my_open_prs(
        self,
        slugs: Iterable[str] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, list[PRInfo]]:
        """Return open PRs authored by the authenticated user, grouped by repo slug.

        If ``slugs`` is given, only PRs against those repos are returned;
        repos with no matching PRs map to an empty list. If ``slugs`` is
        None, all repos the user has open PRs against are returned.

        Coalesces into a single GraphQL search call per ``ghclmp7n.c``
        regardless of how many slugs are passed.
        """
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
        # Track call counts so tests can assert on coalescing
        self.call_count: dict[str, int] = {
            "authenticated_user": 0,
            "org_members": 0,
            "default_branch": 0,
            "my_open_prs": 0,
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

    def my_open_prs(
        self,
        slugs: Iterable[str] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, list[PRInfo]]:
        self.call_count["my_open_prs"] += 1
        if self._my_open_prs is None:
            raise GHError("FakeGHClient: my_open_prs not configured")
        if slugs is None:
            return {k: list(v) for k, v in self._my_open_prs.items()}
        slugs_set = set(slugs)
        return {
            slug: list(self._my_open_prs.get(slug, []))
            for slug in slugs_set
        }
