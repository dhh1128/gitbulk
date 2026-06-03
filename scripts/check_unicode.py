#!/usr/bin/env python3
"""Reject invisible / Trojan-Source Unicode in source files.

This is the CI gate recommended by the gitbulk threat model
(``docs/threat-model.md`` finding **T2**) and by the GitHub supply-chain
standards' "invisible code defense" (GlassWorm / Trojan Source class):
some malicious changes *cannot* be caught by eye or by ordinary linting,
because the payload is encoded in characters that render as nothing — or
that reorder how a security check reads versus how it executes.

What it rejects (by Unicode code point), and why:

* **Bidi controls** ``U+202A–202E`` and ``U+2066–2069`` — the
  "Trojan Source" class: they can make a comment or string visually
  swallow real code, so a reviewer reads a benign line that the compiler
  executes differently.
* **Directional marks** ``U+200E`` / ``U+200F`` / ``U+061C`` — zero-width
  bidi influencers.
* **Zero-width / invisible** ``U+200B–200D``, ``U+2060`` (word joiner),
  ``U+FEFF`` (BOM / ZWNBSP), ``U+00AD`` (soft hyphen) — render as nothing
  and can hide tokens or split identifiers.
* **Variation selectors** ``U+FE00–FE0F`` and ``U+E0100–E01EF`` — the
  GlassWorm-style invisible payload ranges.
* **Tags block** ``U+E0000–E007F`` — invisible ASCII "smuggling" used to
  hide instructions in otherwise-plain text (a real prompt-injection
  vector for files an agent reads).
* **Private Use Areas** ``U+E000–F8FF``, ``U+F0000–FFFFD``,
  ``U+100000–10FFFD`` — no legitimate meaning in source; GlassWorm hid a
  decoder in PUA.

What it deliberately does NOT reject: ordinary printable non-ASCII that
this codebase uses on purpose — status glyphs (``✓ ⚠ ✗``), box-drawing
comment rules (``─``), em-dashes, arrows, etc. A naive "ASCII-only" gate
would flag all of those; this gate targets only the dangerous, invisible,
or reordering categories named above.

Usage::

    python scripts/check_unicode.py [PATH ...]

With no arguments it scans the default roots (see ``DEFAULT_ROOTS``).
Exits non-zero and prints ``path:line:col`` for every finding; exits 0
when clean. Stdlib only, so CI needs no install step.
"""

from __future__ import annotations

import sys
import unicodedata
from pathlib import Path

#: Roots scanned when no paths are given on the command line. The shipped
#: code, the prompts an agent reads, the cron wrapper, this scanner's own
#: home, and the workflow files. ``tests/`` is intentionally omitted: a
#: test may legitimately embed a control character to exercise handling of
#: one. Pass paths explicitly to scan anything else.
DEFAULT_ROOTS: tuple[str, ...] = ("src", "prompts", "bin", "scripts", ".github")

#: Directories never descended into.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "__pycache__",
        ".venv",
        "node_modules",
        ".worktrees",
        "dist",
        "build",
        ".mypy_cache",
        ".pytest_cache",
    }
)

#: Files larger than this are assumed to be data/binary and skipped.
_MAX_BYTES = 2 * 1024 * 1024

# Explicit single code points (ranges handled in :func:`category`).
_BIDI_CONTROLS = frozenset(
    {0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069}
)
_DIRECTIONAL_MARKS = frozenset({0x200E, 0x200F, 0x061C})
_ZERO_WIDTH = frozenset({0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD})


def category(cp: int) -> str | None:
    """Return the disallowed-category name for code point ``cp``, or
    ``None`` if it is permitted."""
    if cp in _BIDI_CONTROLS:
        return "bidi-control"
    if cp in _DIRECTIONAL_MARKS:
        return "directional-mark"
    if cp in _ZERO_WIDTH:
        return "zero-width"
    if 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF:
        return "variation-selector"
    if 0xE0000 <= cp <= 0xE007F:
        return "tag-char"
    if 0xE000 <= cp <= 0xF8FF or 0xF0000 <= cp <= 0xFFFFD or 0x100000 <= cp <= 0x10FFFD:
        return "private-use"
    return None


def find_disallowed(text: str) -> list[tuple[int, int, int, str]]:
    """Scan ``text`` for disallowed code points.

    Returns ``(line, col, code_point, category)`` tuples, both line and
    col 1-based, in document order.
    """
    findings: list[tuple[int, int, int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for col, ch in enumerate(line, start=1):
            cat = category(ord(ch))
            if cat is not None:
                findings.append((lineno, col, ord(ch), cat))
    return findings


def iter_files(roots: list[Path]):
    """Yield decodable text files under ``roots``, skipping excluded dirs,
    oversized files, and anything that is not valid UTF-8."""
    for root in roots:
        if root.is_file():
            candidates = [root]
        else:
            candidates = sorted(root.rglob("*"))
        for path in candidates:
            if not path.is_file():
                continue
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            try:
                if path.stat().st_size > _MAX_BYTES:
                    continue
                path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                # Unreadable or non-UTF-8 (binary) — not our concern.
                continue
            yield path


def scan(roots: list[Path]) -> list[str]:
    """Return a sorted list of human-readable finding lines for ``roots``."""
    out: list[str] = []
    for path in iter_files(roots):
        text = path.read_text(encoding="utf-8")
        for lineno, col, cp, cat in find_disallowed(text):
            name = unicodedata.name(chr(cp), "<unnamed>")
            out.append(f"{path}:{lineno}:{col}: U+{cp:04X} {name} ({cat})")
    return out


def main(argv: list[str]) -> int:
    raw_roots = argv[1:] or list(DEFAULT_ROOTS)
    roots = [Path(r) for r in raw_roots]
    missing = [r for r in roots if not r.exists()]
    roots = [r for r in roots if r.exists()]
    findings = scan(roots)
    for line in findings:
        print(line)
    if missing:
        # Don't fail solely because an optional default root is absent in
        # some checkout; just note it on stderr.
        print(
            "check_unicode: skipped (not present): "
            + ", ".join(str(m) for m in missing),
            file=sys.stderr,
        )
    if findings:
        print(
            f"\ncheck_unicode: {len(findings)} disallowed character(s) found. "
            "These render as nothing or reorder how code reads vs. executes "
            "(Trojan Source / GlassWorm class). See docs/threat-model.md (T2).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
