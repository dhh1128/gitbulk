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
from typing import Any, Iterable, Literal, Mapping, Protocol, runtime_checkable

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

    def merge_pr(
        self,
        slug: str,
        number: int,
        *,
        method: Literal["merge", "squash", "rebase"] = "squash",
        delete_branch: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Merge the PR via ``gh pr merge``.

        Phase 5 mutating method. The only legitimate caller is the
        ``merge`` subcommand handler, and only on its ``--apply`` path
        per node ``2vqp4nk6``. Raises :class:`GHError` on non-clean
        mergeable state, auth failure, or any other refusal from gh.

        ``method`` selects squash / merge / rebase; ``delete_branch``
        toggles ``--delete-branch``. Both are pinned at the call site
        in the merge handler (squash + delete-branch as Phase-5 default
        per the task spec; per-repo override deferred).

        NOTE: the ``.agent-bin/gh`` shim BLOCKS ``gh pr merge`` for AI
        agents. Production code constructs the right argv anyway; the
        shim catches at process boundary. Tests use FakeGHClient so the
        shim is not involved.

        Returns the gh response payload as a dict (gh emits an empty
        body on success in some versions; we return ``{}`` in that case
        rather than raising).
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
        merge_responses: Mapping[
            tuple[str, int], "dict[str, Any] | Exception"
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
        # Per-call argument records so tests can assert merge_pr was
        # invoked with the right method / delete_branch flags.
        self.merge_calls: list[dict[str, Any]] = []
        # Track call counts so tests can assert on coalescing
        self.call_count: dict[str, int] = {
            "authenticated_user": 0,
            "org_members": 0,
            "default_branch": 0,
            "my_open_prs": 0,
            "merge_pr": 0,
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

    def merge_pr(
        self,
        slug: str,
        number: int,
        *,
        method: Literal["merge", "squash", "rebase"] = "squash",
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


# ─── ProductionGHClient ─────────────────────────────────────────────────────


#: Substrings (case-insensitive) in gh stderr that mark a transient failure
#: worth retrying. See node ``ghclmp7n.d`` — the retry policy is hardcoded
#: conservative and not configurable at call sites.
_RETRYABLE_STDERR_MARKERS: tuple[str, ...] = (
    "rate limit",
    "5xx",
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
_MY_OPEN_PRS_GRAPHQL = """\
query($q: String!) {
  search(query: $q, type: ISSUE, first: 100) {
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
        # verified non-deprecated against gh CLI 2026-05-28
        stdout = self._run(
            ("api", f"repos/{slug}", "--jq", ".default_branch"),
            timeout=timeout,
        )
        return stdout.strip()

    def my_open_prs(
        self,
        slugs: Iterable[str] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, list[PRInfo]]:
        # verified non-deprecated against gh CLI 2026-05-28
        # Build the search string per node ghclmp7n.c (coalescing): one
        # GraphQL call regardless of how many slugs are requested.
        slug_list: list[str] | None
        if slugs is None:
            slug_list = None
            search_terms = ["author:@me", "is:open", "is:pr"]
        else:
            slug_list = list(slugs)
            search_terms = ["author:@me", "is:open", "is:pr"]
            search_terms.extend(f"repo:{s}" for s in slug_list)
        search_string = " ".join(search_terms)

        stdout = self._run(
            (
                "api",
                "graphql",
                "-F",
                f"q={search_string}",
                "-f",
                f"query={_MY_OPEN_PRS_GRAPHQL}",
            ),
            timeout=timeout,
        )
        payload = json.loads(stdout)
        nodes = payload.get("data", {}).get("search", {}).get("nodes", [])

        grouped: dict[str, list[PRInfo]] = {}
        if slug_list is not None:
            # Ensure every requested slug appears, even when no PRs exist,
            # matching FakeGHClient.my_open_prs semantics.
            for slug in slug_list:
                grouped.setdefault(slug, [])

        for node in nodes:
            if not node:
                # GraphQL returns nulls for non-PullRequest items in the
                # search results (defensive — our query filters with is:pr,
                # but the field is still nullable).
                continue
            pr = _pr_info_from_graphql_node(node)
            grouped.setdefault(pr.slug, []).append(pr)

        return grouped


    def merge_pr(
        self,
        slug: str,
        number: int,
        *,
        method: Literal["merge", "squash", "rebase"] = "squash",
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
    )
