"""GitHub URL → ``owner/repo`` slug extraction.

Shared between :mod:`gitbulk.invariants.catalog` (which uses it to
verify a local clone's ``origin`` matches the configured slug) and
:mod:`gitbulk.config.repos` (which uses it to canonicalize user-friendly
shortcut forms in ``repos.txt``).

Accepts the three remote URL forms ``gh`` and ``git`` normally emit:

  - SSH shorthand: ``git@github.com:owner/repo[.git]``
  - HTTPS: ``https://github.com/owner/repo[.git]``
  - SSH URL: ``ssh://git@github.com/owner/repo[.git]``

Trailing ``/`` is tolerated on HTTPS. Returns None for anything that
doesn't match — including non-GitHub hosts, which the caller should
report as a friendly error.
"""

from __future__ import annotations

import re

#: URL patterns. Each named group ``slug`` captures ``owner/repo``.
_GITHUB_REMOTE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # ssh shorthand
    re.compile(r"^git@github\.com:(?P<slug>[^/]+/[^/]+?)(?:\.git)?$"),
    # https
    re.compile(r"^https?://github\.com/(?P<slug>[^/]+/[^/]+?)(?:\.git)?/?$"),
    # ssh url
    re.compile(r"^ssh://git@github\.com/(?P<slug>[^/]+/[^/]+?)(?:\.git)?$"),
)


def extract_slug_from_url(url: str) -> str | None:
    """Return ``owner/repo`` parsed from a GitHub remote URL, or None.

    None means "doesn't match any recognized GitHub URL form" (including
    non-GitHub hosts). The caller decides how to surface that.
    """
    for pattern in _GITHUB_REMOTE_PATTERNS:
        match = pattern.match(url)
        if match:
            return match.group("slug")
    return None


__all__ = ["extract_slug_from_url"]
