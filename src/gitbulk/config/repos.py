"""Loader for ~/.config/gitbulk/repos.txt.

See this.i node ``rj4pwn7k`` for the API contract.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from gitbulk import paths

_log = logging.getLogger("gitbulk.config")

# A slug must match exactly owner/repo: two non-empty, non-slash segments.
_SLUG_PATTERN = re.compile(r"^[^/\s]+/[^/\s]+$")


class ConfigError(ValueError):
    """Raised when a config file cannot be parsed as expected."""


@dataclass(frozen=True)
class RepoEntry:
    slug: str
    owner: str
    name: str
    local_path: Path
    source_line: int


def _default_code_root() -> Path:
    return Path.home() / "code"


def load_repos(
    path: Path | None = None,
    code_root: Path | None = None,
) -> list[RepoEntry]:
    """Parse repos.txt and return a list of RepoEntry records.

    Comments (``#``) and blank lines are ignored. A malformed slug raises
    ``ConfigError``; duplicates keep the first occurrence and emit a
    WARNING via ``logging.getLogger('gitbulk.config')``.
    """
    if path is None:
        path = paths.repos_file()
    if code_root is None:
        code_root = _default_code_root()

    text = path.read_text()
    seen_slugs: dict[str, int] = {}
    entries: list[RepoEntry] = []

    for lineno, raw in enumerate(text.splitlines(), start=1):
        # Strip inline comments first, then surrounding whitespace.
        stripped = raw.split("#", 1)[0].strip()
        if not stripped:
            continue
        if not _SLUG_PATTERN.match(stripped):
            raise ConfigError(
                f"{path}:{lineno}: malformed slug {stripped!r} "
                f"(expected exactly 'owner/repo')"
            )
        if stripped in seen_slugs:
            _log.warning(
                "%s:%d: duplicate slug %r (first seen at line %d); ignoring",
                path,
                lineno,
                stripped,
                seen_slugs[stripped],
            )
            continue
        seen_slugs[stripped] = lineno
        owner, name = stripped.split("/", 1)
        entries.append(
            RepoEntry(
                slug=stripped,
                owner=owner,
                name=name,
                local_path=code_root / name,
                source_line=lineno,
            )
        )
    return entries
