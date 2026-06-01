"""Org-members cache (this.i nodes ``hbcls4pq`` + ``schv4nrm``).

Read/write of ``~/.cache/gitbulk/org-members/<org>.yaml``. The file is
the cache consulted by step 3 of :func:`gitbulk.classifier.classify_login`
and validated by the ``org.members.fresh`` preflight invariant.

Schema (per ``schv4nrm``):

.. code-block:: yaml

    schema_version: 1
    fetched_at: 2026-05-28T06:41:00+00:00
    members:
      - dhh1128
      - alice

Strictness: :func:`load_cache` returns ``None`` (never raises) for
missing files, malformed YAML, wrong schema_version, missing/extra
fields, or malformed timestamps. The caller treats ``None`` as
"must refresh"; corrupt cache must not crash a long-running gitbulk
process. Writers go through :func:`save_cache`, which uses
tmp-file-plus-``os.replace`` for atomicity.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import yaml

from gitbulk import paths
from gitbulk.gh import GHClient, GHError

if TYPE_CHECKING:
    from gitbulk.config.policy import Policy


class OrgMembersRefreshError(Exception):
    """Raised when :func:`ensure_org_members_fresh` cannot refresh the cache.

    The message is pre-formatted with the trigger (``--refresh-org-members``
    forced vs ``org-members auto-refresh`` automatic) so callers record and
    surface it verbatim without re-deriving which path fired.
    """

#: Cache file schema version. See ``schv4nrm``. A reader that finds a
#: different value treats the file as unreadable and returns None;
#: clean-break semantics are intentional per ``schv4nrm`` so old
#: gitbulk fails loudly when handed a newer cache.
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CachedMembers:
    """In-memory snapshot of one org-members cache file.

    ``fetched_at`` is always a tz-aware UTC ``datetime``.
    ``members`` is a frozenset to make membership checks O(1) and to
    discourage accidental mutation between cache read and classifier use.
    """

    org: str
    fetched_at: datetime
    members: frozenset[str]


# ─── load ───────────────────────────────────────────────────────────────────


def _coerce_loaded(raw: Any, org: str) -> CachedMembers | None:
    """Validate a parsed YAML payload and return a CachedMembers or None.

    Pulled out of :func:`load_cache` so each failure mode can be
    exercised by passing a constructed dict in tests if needed. In
    practice all callers go through :func:`load_cache`.
    """
    if not isinstance(raw, dict):
        return None
    if raw.get("schema_version") != SCHEMA_VERSION:
        return None
    fetched_at_raw = raw.get("fetched_at")
    members_raw = raw.get("members")
    if fetched_at_raw is None or members_raw is None:
        return None
    # PyYAML decodes ISO-8601 timestamps to datetime automatically when
    # they parse cleanly; if the user (or a corrupted writer) stored a
    # string, accept that too. Anything else is malformed.
    if isinstance(fetched_at_raw, datetime):
        fetched_at = fetched_at_raw
    elif isinstance(fetched_at_raw, str):
        try:
            fetched_at = datetime.fromisoformat(fetched_at_raw)
        except ValueError:
            return None
    else:
        return None
    if fetched_at.tzinfo is None:
        # Naive timestamps are ambiguous; refuse them (mirrors the
        # naive-datetime stance in paths.new_runid).
        return None
    fetched_at = fetched_at.astimezone(timezone.utc)
    if not isinstance(members_raw, list):
        return None
    for m in members_raw:
        if not isinstance(m, str):
            return None
    return CachedMembers(
        org=org,
        fetched_at=fetched_at,
        members=frozenset(members_raw),
    )


def load_cache(org: str) -> CachedMembers | None:
    """Read the cache file for ``org``.

    Returns ``None`` when the file is missing, has malformed YAML, has
    the wrong ``schema_version``, is missing a required field, or has
    a malformed timestamp. Never raises on a corrupt cache — the caller
    treats ``None`` as "must refresh."
    """
    path = paths.org_members_cache_file(org)
    if not path.exists():
        return None
    try:
        with path.open() as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError:
        return None
    return _coerce_loaded(raw, org)


# ─── save ───────────────────────────────────────────────────────────────────


def save_cache(cached: CachedMembers) -> None:
    """Atomically write ``cached`` to its on-disk YAML file.

    Uses tmp-file-plus-``os.replace`` so a partially-written file is
    never observable by a concurrent reader. The enclosing directory is
    created if absent (defensive — ``paths.ensure_directories`` should
    have already done so).
    """
    path = paths.org_members_cache_file(cached.org)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Normalize fetched_at to UTC ISO-8601 with explicit "+00:00".
    fetched_at_utc = cached.fetched_at.astimezone(timezone.utc)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "fetched_at": fetched_at_utc.isoformat(),
        "members": sorted(cached.members),
    }
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w") as f:
        yaml.safe_dump(payload, f, sort_keys=False)
    os.replace(tmp_path, path)


# ─── freshness ──────────────────────────────────────────────────────────────


def is_fresh(
    cached: CachedMembers,
    ttl_hours: int,
    *,
    now: datetime | None = None,
) -> bool:
    """Return True iff ``(now - cached.fetched_at) < ttl_hours``.

    Strict less-than: a cache exactly at the TTL boundary is treated as
    stale so that the ``org.members.fresh`` invariant errs on the side
    of refresh. ``now`` defaults to the current UTC wall clock; tests
    pass an explicit value to pin the comparison.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        raise ValueError("is_fresh requires tz-aware datetime; got naive (no tzinfo)")
    age = now - cached.fetched_at
    return age < timedelta(hours=ttl_hours)


# ─── refresh ────────────────────────────────────────────────────────────────


def refresh_cache(gh: GHClient, org: str) -> CachedMembers:
    """Fetch ``org`` members via ``gh.org_members``, save, and return.

    The fetched_at timestamp is the wall clock at the moment the GH call
    returns; this is the value used by :func:`is_fresh` against the
    configured TTL.
    """
    members = gh.org_members(org)
    cached = CachedMembers(
        org=org,
        fetched_at=datetime.now(timezone.utc),
        members=frozenset(members),
    )
    save_cache(cached)
    return cached


def ensure_org_members_fresh(
    gh: GHClient,
    policy: "Policy",
    *,
    force: bool = False,
) -> CachedMembers | None:
    """Refresh the org-members cache when it is missing, stale, or ``force``d.

    This is the self-healing entry point every subcommand calls before
    its universal preflight (node ormrf7kq). It mirrors the default-
    branch cache's ``prime_default_branches``: a missing or stale cache
    is refetched rather than hard-failing, because org-members staleness
    only degrades classification toward the conservative BOT default
    (node rj7p4kqn / pj5kn2zw) — it is never destructive, so refreshing
    is always the right move and never a decision a human needs to make.
    ``force`` (the CLI ``--refresh-org-members`` flag) refetches even a
    cache that is still within its TTL.

    Returns the freshly-fetched :class:`CachedMembers` when a refresh
    happened, or ``None`` when no refresh was needed — either because the
    on-disk cache was already fresh, or because ``policy.humans.org`` is
    unset (no org ⇒ the classifier falls through to the safe BOT default
    with no lookup, so no cache is required).

    Raises :class:`OrgMembersRefreshError` when the refresh fetch itself
    fails (GitHub unreachable or unauthenticated). That is the one
    legitimate hard-stop: a command — most acutely a mutating one — must
    not classify PR authors on a guess. The exception message names the
    trigger so the caller surfaces it verbatim. Callers invoke this
    inside the global lock (security-hawk F4, shawk7nq) and convert the
    error into their own EXIT_STRUCTURAL_FAILURE finish.
    """
    org = policy.humans.org
    if org is None:
        return None
    cached = load_cache(org)
    if (
        not force
        and cached is not None
        and is_fresh(cached, policy.humans.cache_ttl_hours)
    ):
        return None
    how = "--refresh-org-members" if force else "org-members auto-refresh"
    try:
        return refresh_cache(gh, org)
    except GHError as e:
        raise OrgMembersRefreshError(f"{how} failed: {e}") from e
