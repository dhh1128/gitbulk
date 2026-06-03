"""On-disk default-branch cache (Stage 2 of the prefetch optimization).

The per-repo invariant chain calls ``gh.default_branch(slug)``. Stage 1
batched those into a chunked GraphQL prefetch (~21s cold for a 205-repo
fleet). But default branches change closer to *never* than to daily, so
re-fetching every run is wasteful. This module persists the resolved
branches to ``~/.cache/gitbulk/default-branches.yaml`` and seeds the gh
client's in-process cache from it, so a warm run skips the network
entirely for slugs fetched within the TTL.

Schema::

    schema_version: 1
    branches:
      provenant-dev/origin-platform:
        branch: main
        fetched_at: 2026-05-29T14:02:00+00:00
      dhh1128/gitbulk:
        branch: main
        fetched_at: 2026-05-29T14:02:00+00:00

Per-entry ``fetched_at`` (not a single file-level timestamp) so adding
one repo to repos.txt only fetches that repo, and entries expire
independently. TTL defaults to 7 days (node ``dbcttl7d``): default
branches rarely change, and every staleness failure mode is "operate
too conservatively" (a PR's base looks non-default → Skip), never
destructive.

Strictness mirrors ``org_members_cache``: :func:`load_cache` returns an
empty dict (never raises) for missing files, malformed YAML, wrong
schema_version, or malformed entries — a corrupt cache must not crash a
long-running gitbulk process; the caller just re-fetches.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import yaml

from gitbulk import paths
from gitbulk.gh import GHClient
from gitbulk.util import atomicio

SCHEMA_VERSION = 1

#: Default time-to-live for a cached default-branch entry. See node
#: ``dbcttl7d``: a week balances cache savings against the (low) chance
#: a default branch was renamed since the last fetch.
DEFAULT_TTL_DAYS = 7


@dataclass(frozen=True)
class CachedBranch:
    """One cache entry: the default branch, when it was fetched, and the
    repo's archived status.

    ``archived`` piggybacks on this cache because it is resolved by the
    same coalesced GraphQL prefetch (``isArchived`` is selected alongside
    ``defaultBranchRef``). It defaults to False so bare two-arg
    construction keeps working and a legacy entry with no ``archived`` key
    loads as not-archived — the safe direction, since it never causes a
    live repo to be wrongly skipped.
    """

    branch: str
    fetched_at: datetime
    archived: bool = False


# ─── load ───────────────────────────────────────────────────────────────────


def _coerce_entry(raw: Any) -> CachedBranch | None:
    """Validate one parsed entry dict → CachedBranch or None."""
    if not isinstance(raw, dict):
        return None
    branch = raw.get("branch")
    fetched_at_raw = raw.get("fetched_at")
    if not isinstance(branch, str) or not branch:
        return None
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
        return None
    return CachedBranch(
        branch=branch,
        fetched_at=fetched_at.astimezone(timezone.utc),
        archived=bool(raw.get("archived", False)),
    )


def load_cache() -> dict[str, CachedBranch]:
    """Read the default-branch cache file → ``{slug: CachedBranch}``.

    Returns an empty dict on any whole-file problem (missing, malformed
    YAML, wrong schema_version). Individual malformed entries are
    skipped without discarding the rest of the file.
    """
    path = paths.default_branch_cache_file()
    if not path.exists():
        return {}
    try:
        with path.open() as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError:
        return {}
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        return {}
    branches_raw = raw.get("branches")
    if not isinstance(branches_raw, dict):
        return {}
    out: dict[str, CachedBranch] = {}
    for slug, entry in branches_raw.items():
        if not isinstance(slug, str):
            continue
        coerced = _coerce_entry(entry)
        if coerced is not None:
            out[slug] = coerced
    return out


# ─── save ───────────────────────────────────────────────────────────────────


def save_cache(branches: dict[str, CachedBranch]) -> None:
    """Atomically write the full ``{slug: CachedBranch}`` map.

    tmp-file-plus-``os.replace`` so a concurrent reader never sees a
    partial file (mirrors ``org_members_cache.save_cache``).
    """
    path = paths.default_branch_cache_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "branches": {
            slug: {
                "branch": cb.branch,
                "fetched_at": cb.fetched_at.astimezone(timezone.utc).isoformat(),
                "archived": cb.archived,
            }
            for slug, cb in sorted(branches.items())
        },
    }
    atomicio.atomic_write_text(path, yaml.safe_dump(payload, sort_keys=False))


# ─── freshness ────────────────────────────────────────────────────────────────


def is_fresh(cb: CachedBranch, ttl_days: int, *, now: datetime) -> bool:
    """True iff ``(now - cb.fetched_at) < ttl_days``. Strict less-than so
    an entry exactly at the boundary is treated as stale (re-fetch)."""
    return (now - cb.fetched_at) < timedelta(days=ttl_days)


# ─── orchestration ────────────────────────────────────────────────────────────


def prime_default_branches(
    gh: GHClient,
    slugs: list[str],
    *,
    ttl_days: int = DEFAULT_TTL_DAYS,
    on_progress: "Callable[[int, int], None] | None" = None,
    now: datetime | None = None,
) -> None:
    """Seed gh's in-process default-branch cache, fetching only misses.

    The full warm/cold flow:

      1. Load the on-disk cache.
      2. Seed gh's in-process cache with the entries that are both
         requested AND still fresh — those need no network.
      3. Prefetch (chunked GraphQL) only the stale/missing slugs.
      4. Persist the merged result: fresh entries keep their old
         timestamp, freshly-fetched entries get ``now``, untouched
         entries (slugs from other repos.txt subsets) are preserved.

    ``on_progress`` is forwarded to ``gh.prefetch_default_branches`` so
    the cold portion still shows progress. On an all-warm run there is
    no network call and no progress (nothing to fetch).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    file_cache = load_cache()

    fresh: dict[str, CachedBranch] = {}
    missing: list[str] = []
    for slug in slugs:
        cb = file_cache.get(slug)
        if cb is not None and is_fresh(cb, ttl_days, now=now):
            fresh[slug] = cb
        else:
            missing.append(slug)

    if fresh:
        gh.seed_default_branches({slug: cb.branch for slug, cb in fresh.items()})
        # Seed archived status too so the github.not_archived gate works on
        # an all-warm run (no network).
        gh.seed_archived({slug: cb.archived for slug, cb in fresh.items()})

    if missing:
        gh.prefetch_default_branches(missing, on_progress=on_progress)

    # Persist. Start from the existing file so slugs not in this run's
    # ``slugs`` list survive (a different cron entry may use a different
    # repos.txt subset).
    merged: dict[str, CachedBranch] = dict(file_cache)
    # Fresh entries: keep as-is (already in merged from file_cache).
    # Freshly-fetched entries: read back from gh's in-process cache and
    # stamp with `now`. A slug in `missing` that gh couldn't resolve
    # (deleted repo) won't be in the in-process cache — drop any stale
    # file entry for it so we don't keep serving a dead branch forever.
    resolved = gh.cached_default_branches()
    resolved_archived = gh.cached_archived()
    for slug in missing:
        branch = resolved.get(slug)
        if branch is not None:
            merged[slug] = CachedBranch(
                branch=branch,
                fetched_at=now,
                archived=bool(resolved_archived.get(slug, False)),
            )
        else:
            merged.pop(slug, None)
    save_cache(merged)


__all__ = [
    "CachedBranch",
    "DEFAULT_TTL_DAYS",
    "SCHEMA_VERSION",
    "is_fresh",
    "load_cache",
    "prime_default_branches",
    "save_cache",
]
