"""Loader for ~/.config/gitbulk/repos.txt.

See this.i node ``rj4pwn7k`` for the API contract.

Accepted line forms (canonical → shortcut):

  1. ``owner/repo`` — GitHub slug. Local clone assumed at
     ``<code-root>/<repo-name>``. (Canonical.)
  2. ``https://github.com/owner/repo[.git]`` — HTTPS URL. Slug parsed
     from URL; local clone assumed at ``<code-root>/<repo-name>``.
  3. ``git@github.com:owner/repo[.git]`` or
     ``ssh://git@github.com/owner/repo[.git]`` — SSH URLs. Same as #2.
  4. ``/absolute/path/to/clone`` or ``~/path/to/clone`` — local path.
     Slug parsed from ``git -C <path> remote get-url origin``; local
     clone is the explicit path, NOT ``<code-root>/<repo-name>``.
     Useful when clones aren't organized as ``<code-root>/<repo-name>``
     (nested categories, duplicate basenames, etc.).

The slug form is canonical because it's the smallest and the one a
discovery-mode export would emit. All other forms are user-friendly
shortcuts that the loader canonicalizes on its way through.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from gitbulk import paths
from gitbulk.util.github_url import extract_slug_from_url

_log = logging.getLogger("gitbulk.config")

# Slug shape per security-hawk F1 (2026-05-28) — must match paths._SLUG_PATTERN
# to prevent path-traversal via repos.txt. Owner: GitHub-style 1-39 chars,
# alphanumeric + hyphen, no leading hyphen. Repo: 1-100 chars,
# [A-Za-z0-9._-]. Path-segment safety enforced by `_FORBIDDEN_SEGMENTS`.
_SLUG_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$"
)
_FORBIDDEN_SEGMENTS: frozenset[str] = frozenset({".", ".."})

#: Quick form-detection. URL forms start with a known protocol marker;
#: path forms start with ``/`` or ``~``; everything else is treated as a
#: slug candidate and validated by ``_SLUG_PATTERN``.
_URL_PREFIXES = ("https://", "http://", "git@", "ssh://")
_PATH_PREFIXES = ("/", "~")


class ConfigError(ValueError):
    """Raised when the repos.txt FILE itself is unreadable or absent.

    Per-entry validation errors do NOT raise — they're returned as
    :class:`SkippedEntry` records so the rest of the file can still
    process. Reserve ConfigError for whole-file failures the user
    can't work around without fixing the file at all (missing file,
    permission denied).
    """


@dataclass(frozen=True)
class RepoEntry:
    slug: str
    owner: str
    name: str
    local_path: Path
    source_line: int


@dataclass(frozen=True)
class SkippedEntry:
    """One repos.txt line that couldn't be canonicalized.

    Returned alongside the valid :class:`RepoEntry` records by
    :func:`load_repos` so the rest of the file still processes. Each
    skipped entry carries the raw line content, the 1-based line
    number, and a human-readable reason that the handler surfaces in
    summary.md so the user can act on it.
    """

    raw: str
    lineno: int
    reason: str


def _default_code_root() -> Path:
    return Path.home() / "code"


def _slug_to_entry(
    slug: str, *, lineno: int, code_root: Path, explicit_path: Path | None = None
) -> RepoEntry:
    """Build a :class:`RepoEntry` from a validated slug.

    ``explicit_path``, when given, overrides the default
    ``<code-root>/<repo-name>`` location. The slug itself is assumed
    pre-validated by the caller.
    """
    owner, name = slug.split("/", 1)
    local = explicit_path if explicit_path is not None else (code_root / name)
    return RepoEntry(
        slug=slug,
        owner=owner,
        name=name,
        local_path=local,
        source_line=lineno,
    )


def _validate_slug_reason(slug: str, *, source: str) -> str | None:
    """Return None if slug is valid, else a human-readable reason.

    ``source`` is a short tag describing where the slug came from
    (``"line"``, ``"URL"``, ``"path remote"``) — used in the reason
    so the user can tell whether they typed a malformed slug directly
    or supplied an upstream form that decoded to a malformed slug.
    """
    if not _SLUG_PATTERN.match(slug):
        return (
            f"{source} resolved to slug {slug!r} which does not match "
            f"the expected GitHub form 'owner/repo' (owner: 1-39 chars, "
            f"repo: 1-100 chars, no '..' segments)."
        )
    if any(part in _FORBIDDEN_SEGMENTS for part in slug.split("/")):
        return (
            f"{source} resolved to slug {slug!r} which contains a "
            f"forbidden path segment ('.' or '..')."
        )
    return None


def _resolve_path_to_slug(
    raw_path: str,
) -> tuple[str, Path] | str:
    """Given a user-provided local path, return either
    ``(slug, resolved_path)`` on success or a string reason on failure.

    Resolves ``~`` and relative path components, then asks git for the
    ``origin`` remote URL and extracts the slug from it. Failure modes:
    path doesn't exist, not a git repo, no origin, non-GitHub origin.
    """
    resolved = Path(raw_path).expanduser().resolve()
    if not resolved.exists():
        return f"local path {raw_path!r} does not exist."
    if not (resolved / ".git").exists():
        return f"{resolved} is not a git repository (no .git directory)."
    # ``git remote get-url origin`` is read-only and inexpensive.
    result = subprocess.run(
        ["git", "-C", str(resolved), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return (
            f"{resolved} has no 'origin' remote "
            f"(git stderr: {result.stderr.strip() or 'unknown'})."
        )
    url = result.stdout.strip()
    slug = extract_slug_from_url(url)
    if slug is None:
        return (
            f"{resolved}'s origin URL {url!r} is not a recognized "
            f"GitHub remote (gitbulk currently only operates on "
            f"github.com repos)."
        )
    return slug, resolved


def load_repos(
    path: Path | None = None,
    code_root: Path | None = None,
) -> tuple[list[RepoEntry], list[SkippedEntry]]:
    """Parse repos.txt and return ``(valid_entries, skipped_entries)``.

    See module docstring for the accepted line forms. Comments (``#``)
    and blank lines are ignored. A line that can't be canonicalized
    becomes a :class:`SkippedEntry` with a human-readable reason —
    the rest of the file still processes so one typo doesn't block a
    150-line repos.txt. Duplicate slugs are silently deduped (first
    wins; debug log records the dup).

    The only error condition that still RAISES is the repos.txt file
    itself being missing or unreadable: that's a whole-file failure
    the user can't work around without fixing it, so a clean
    :class:`ConfigError` at the CLI layer is the right surface.
    """
    if path is None:
        path = paths.repos_file()
    if code_root is None:
        code_root = _default_code_root()

    try:
        text = path.read_text()
    except FileNotFoundError:
        raise ConfigError(
            f"repos.txt not found at {path}. "
            f"Create it (one 'owner/repo' slug per line; see "
            f"config/repos.txt.example) or pass --config-root."
        ) from None

    seen_slugs: dict[str, int] = {}
    entries: list[RepoEntry] = []
    skipped: list[SkippedEntry] = []

    for lineno, raw in enumerate(text.splitlines(), start=1):
        # Strip inline comments first, then surrounding whitespace.
        stripped = raw.split("#", 1)[0].strip()
        if not stripped:
            continue
        # Detect form and canonicalize. Each branch sets either
        # (slug, explicit_path) on success OR appends to ``skipped``
        # and continues.
        slug: str | None = None
        explicit_path: Path | None = None
        if stripped.startswith(_URL_PREFIXES):
            extracted = extract_slug_from_url(stripped)
            if extracted is None:
                skipped.append(SkippedEntry(
                    raw=stripped,
                    lineno=lineno,
                    reason=(
                        f"URL {stripped!r} is not a recognized GitHub "
                        f"remote form (expected https://github.com/owner/repo, "
                        f"git@github.com:owner/repo, or "
                        f"ssh://git@github.com/owner/repo)."
                    ),
                ))
                continue
            reason = _validate_slug_reason(extracted, source="URL")
            if reason is not None:
                skipped.append(SkippedEntry(raw=stripped, lineno=lineno, reason=reason))
                continue
            slug = extracted
        elif stripped.startswith(_PATH_PREFIXES):
            outcome = _resolve_path_to_slug(stripped)
            if isinstance(outcome, str):
                skipped.append(SkippedEntry(raw=stripped, lineno=lineno, reason=outcome))
                continue
            slug, explicit_path = outcome
            reason = _validate_slug_reason(slug, source="path remote")
            if reason is not None:
                skipped.append(SkippedEntry(raw=stripped, lineno=lineno, reason=reason))
                continue
        else:
            # Bare slug form (canonical).
            reason = _validate_slug_reason(stripped, source="line")
            if reason is not None:
                skipped.append(SkippedEntry(raw=stripped, lineno=lineno, reason=reason))
                continue
            slug = stripped

        assert slug is not None  # narrowing for type checkers
        if slug in seen_slugs:
            # Silent dedup — first wins. Kept at DEBUG for anyone
            # debugging "why isn't gitbulk seeing my entry?"
            _log.debug(
                "%s:%d: duplicate slug %r (first seen at line %d); ignoring",
                path,
                lineno,
                slug,
                seen_slugs[slug],
            )
            continue
        seen_slugs[slug] = lineno
        entries.append(
            _slug_to_entry(
                slug,
                lineno=lineno,
                code_root=code_root,
                explicit_path=explicit_path,
            )
        )
    return entries, skipped
