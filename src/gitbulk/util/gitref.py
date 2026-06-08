"""Validation of untrusted git refs and SHAs before they reach a subprocess
or a REST API path (this.i node ``gtargv7n``).

``base_ref`` / ``head_ref`` / ``head_sha`` arrive from gh's GraphQL/REST JSON
and are interpolated into ``git`` argv (rebase.py) and into REST paths
(``gh.fetch_check_runs``). A ref that begins with ``-`` is parsed by git as an
OPTION rather than a positional — e.g. ``--upload-pack=<cmd>`` on a fetch is
remote-code-execution under cron — and a sha containing ``/`` or ``?`` can
redirect an API path. These validators are the primary, fail-closed defense;
they intentionally reject far less than git's full ``check-ref-format`` (we
only need to neutralize option/path injection), so they never trip on a
legitimate GitHub branch name.
"""

from __future__ import annotations

import re

#: A git object name: 7–40 lowercase hex chars. gh always emits the full
#: 40-char oid for head SHAs; the 7-char floor tolerates an abbreviated value
#: without admitting anything path-significant.
_SHA_RE = re.compile(r"[0-9a-f]{7,40}")


class UnsafeGitValue(ValueError):
    """Raised by :func:`ensure_safe_ref` / :func:`ensure_valid_sha` when a
    value cannot be safely handed to ``git`` or interpolated into a REST
    path."""


def is_safe_ref(ref: object) -> bool:
    """True if ``ref`` is safe to pass to ``git`` as a positional argument.

    Safe means: a non-empty ``str`` that does not begin with ``-`` (which git
    would read as an option) and contains no ASCII whitespace or control
    characters (which a legitimate refname never does, and which keep the
    value from being split or smuggling control bytes).
    """
    if not isinstance(ref, str) or not ref:
        return False
    if ref[0] == "-":
        return False
    return all(not ch.isspace() and 0x20 < ord(ch) != 0x7F for ch in ref)


def is_valid_sha(sha: object) -> bool:
    """True if ``sha`` is a 7–40 char lowercase-hex git object name."""
    return isinstance(sha, str) and _SHA_RE.fullmatch(sha) is not None


def ensure_safe_ref(ref: str) -> str:
    """Return ``ref`` if :func:`is_safe_ref`, else raise :class:`UnsafeGitValue`."""
    if not is_safe_ref(ref):
        raise UnsafeGitValue(f"unsafe git ref {ref!r}")
    return ref


def ensure_valid_sha(sha: str) -> str:
    """Return ``sha`` if :func:`is_valid_sha`, else raise :class:`UnsafeGitValue`."""
    if not is_valid_sha(sha):
        raise UnsafeGitValue(f"invalid git sha {sha!r}")
    return sha


__all__ = [
    "UnsafeGitValue",
    "ensure_safe_ref",
    "ensure_valid_sha",
    "is_safe_ref",
    "is_valid_sha",
]
