"""XDG-aware paths used by gitbulk.

Single source of truth for every file and directory the tool reads or
writes. See this.i node 3pw7qkn2 for the load-bearing conventions
(XDG-only resolution, compact ISO 8601 UTC run-ids, slug normalization,
no memoization).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from os import environ
from pathlib import Path

# Slug shape per security-hawk F1 (2026-05-28) — defense-in-depth against
# malicious config files. Owner: GitHub-style 1-39 chars, alphanumeric +
# hyphen, no leading hyphen. Repo: 1-100 chars, [A-Za-z0-9._-]. The
# `_FORBIDDEN_SEGMENTS` check after the regex match is the path-traversal
# defense: `..` and `.` as full segments are rejected even though the
# character class would otherwise permit them.
_SLUG_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$"
)
_FORBIDDEN_SEGMENTS: frozenset[str] = frozenset({".", ".."})
_RUNID_FORMAT = "%Y%m%dT%H%M%SZ"


def _xdg_or_default(env_var: str, fallback: Path) -> Path:
    value = environ.get(env_var)
    if value:
        return Path(value) / "gitbulk"
    return fallback


# CLI --config-root override (per cli._apply_config_root). Held in this module
# rather than mutated into XDG_CONFIG_HOME so that subprocess children — gh,
# claude — DO NOT inherit it. (Otherwise gh and claude, both of which respect
# XDG_CONFIG_HOME for their own credential / config lookups, would lose their
# auth when gitbulk relocates its own config dir.) Set via ``set_config_dir_override``;
# consulted in ``config_dir()`` before the XDG env-var check.
_CONFIG_DIR_OVERRIDE: Path | None = None


def set_config_dir_override(path: Path | None) -> None:
    """Pin :func:`config_dir` to ``path``, bypassing ``XDG_CONFIG_HOME``.

    Used by the CLI's ``--config-root`` flag to redirect gitbulk's config
    lookup WITHOUT mutating ``os.environ`` (which would mis-redirect child
    processes like ``gh`` that also respect XDG). Passing ``None`` restores
    the standard XDG-then-default resolution.
    """
    global _CONFIG_DIR_OVERRIDE
    _CONFIG_DIR_OVERRIDE = path


def config_dir() -> Path:
    if _CONFIG_DIR_OVERRIDE is not None:
        return _CONFIG_DIR_OVERRIDE
    return _xdg_or_default("XDG_CONFIG_HOME", Path.home() / ".config" / "gitbulk")


def cache_dir() -> Path:
    return _xdg_or_default("XDG_CACHE_HOME", Path.home() / ".cache" / "gitbulk")


def repos_file() -> Path:
    return config_dir() / "repos.txt"


def policy_file() -> Path:
    return config_dir() / "gitbulk.yaml"


def runs_dir() -> Path:
    return cache_dir() / "runs"


def run_dir(timestamp: str, subcommand: str) -> Path:
    return runs_dir() / f"{timestamp}-{subcommand}"


def latest_run_symlink(subcommand: str) -> Path:
    return runs_dir() / f"latest-{subcommand}"


def locks_dir() -> Path:
    return cache_dir() / "locks"


def _normalize_slug(slug: str) -> str:
    if not _SLUG_PATTERN.match(slug):
        raise ValueError(f"malformed slug: {slug!r} (expected exactly 'owner/repo')")
    # Defense-in-depth: even though the regex disallows path metacharacters,
    # explicitly reject `.` and `..` as full segments so a future regex
    # relaxation does not silently re-open the security-hawk F1 traversal.
    if any(part in _FORBIDDEN_SEGMENTS for part in slug.split("/")):
        raise ValueError(
            f"malformed slug: {slug!r} (contains forbidden path segment)"
        )
    return slug.replace("/", "__")


def repo_lock_file(slug: str) -> Path:
    return locks_dir() / f"{_normalize_slug(slug)}.lock"


def global_lock_file() -> Path:
    return cache_dir() / "run.lock"


def default_worktree_root() -> Path:
    return cache_dir() / "worktrees"


def worktree_dir(runid: str, slug: str, root: Path | None = None) -> Path:
    base = root if root is not None else default_worktree_root()
    return base / runid / _normalize_slug(slug)


def findings_dir(slug: str) -> Path:
    return cache_dir() / "findings" / _normalize_slug(slug)


def attention_sentinel() -> Path:
    return cache_dir() / "ATTENTION"


def dashboard_file() -> Path:
    return cache_dir() / "dashboard.md"


def org_members_cache_dir() -> Path:
    return cache_dir() / "org-members"


def org_members_cache_file(org: str) -> Path:
    """Path to the org-members cache YAML for ``org``.

    The classifier and the ``org.members.fresh`` invariant both read this
    file; ``org_members_cache.save_cache`` writes it. See this.i node
    ``hbcls4pq`` for the contract and ``schv4nrm`` for the schema-version
    discipline applied to the file's contents.
    """
    return org_members_cache_dir() / f"{org}.yaml"


def ensure_directories() -> None:
    """Create every directory gitbulk writes to. Idempotent."""
    for d in (
        config_dir(),
        cache_dir(),
        runs_dir(),
        locks_dir(),
        default_worktree_root(),
        org_members_cache_dir(),
    ):
        d.mkdir(parents=True, exist_ok=True)


def new_runid(when: datetime | None = None) -> str:
    """Compact ISO 8601 UTC timestamp used in run-directory names.

    A tz-aware datetime is required when ``when`` is supplied; a naive
    datetime would silently get interpreted as local time, which is
    exactly the ambiguity convention (b) of node 3pw7qkn2 rules out.
    """
    if when is None:
        when = datetime.now(timezone.utc)
    elif when.tzinfo is None:
        raise ValueError("new_runid requires tz-aware datetime; got naive (no tzinfo)")
    else:
        when = when.astimezone(timezone.utc)
    return when.strftime(_RUNID_FORMAT)
