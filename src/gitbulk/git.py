"""Central, pinned resolution of the ``git`` executable (SEC-F3).

gitbulk already pins the other executables it shells out to so a later
``PATH`` prepend cannot substitute them under it: ``gh`` is resolved via
:func:`shutil.which` in :class:`gitbulk.gh.ProductionGHClient`, and the coding
agent binary via :func:`gitbulk.agent._pin_binary`. ``git`` was the gap — it
was invoked as a bare ``"git"`` argv[0] at every call site, including the
credentialed ``force-push-with-lease`` (:mod:`gitbulk.rebase`) and the
operator-clone (:mod:`gitbulk.isolated_clone`) paths. A malicious ``git`` earlier
on ``PATH`` would therefore run with the operator's credentials.

The defense is the same one used for ``gh``/``claude``: resolve the bare name to
an absolute path ONCE via :func:`shutil.which` and use that absolute path as
argv[0] everywhere. This module owns that single resolution.

Semantics mirror :func:`gitbulk.agent._pin_binary`'s fallback exactly:

  - Resolves to the absolute path :func:`shutil.which` finds, so a subsequent
    ``PATH`` prepend cannot hijack it.
  - Falls back to the bare name ``"git"`` when ``which`` finds nothing — an
    absent binary cannot be PATH-hijacked, and the missing executable then
    surfaces as a normal per-operation launch failure (``FileNotFoundError`` /
    a git-step error) rather than aborting import.

Resolution happens at import time and is cached in :data:`GIT`. Use
:data:`GIT` as argv[0] for every ``git`` subprocess invocation; call
:func:`resolve_git` only when a fresh resolution is genuinely wanted (tests).
"""

from __future__ import annotations

import shutil


def resolve_git() -> str:
    """Resolve ``git`` to a trusted absolute path, or fall back to ``"git"``.

    Mirrors :func:`gitbulk.agent._pin_binary`'s bare-name branch: a
    :func:`shutil.which` hit pins the absolute path (PATH-hijack proof); a miss
    returns the bare name so an absent binary surfaces as a normal launch
    failure rather than an import-time abort.
    """
    found = shutil.which("git")
    return found if found is not None else "git"


#: The pinned ``git`` executable, resolved once at import. Every ``git``
#: subprocess invocation in gitbulk uses this as argv[0].
GIT: str = resolve_git()


__all__ = ["GIT", "resolve_git"]
