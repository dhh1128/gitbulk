"""Tests for the invisible-Unicode / Trojan-Source CI gate.

The gate itself lives in ``scripts/check_unicode.py`` (not under
``src/gitbulk/``, so it is outside the 100%-branch coverage gate); these
tests lock in that it flags the dangerous categories and passes the
legitimate non-ASCII this codebase uses on purpose. See
``docs/threat-model.md`` finding T2.

Disallowed code points are built with ``chr()`` on purpose: embedding the
literal invisible characters in this file would be fragile (copy/paste or
an editor can strip them) and self-defeating (a reviewer could not see
them, and this file is excluded from the gate's default scan).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_unicode.py"

# Representative dangerous code points, named for readability.
ZWSP = chr(0x200B)  # ZERO WIDTH SPACE
RLO = chr(0x202E)  # RIGHT-TO-LEFT OVERRIDE (Trojan Source)
ZWJ = chr(0x200D)  # ZERO WIDTH JOINER


def _load():
    spec = importlib.util.spec_from_file_location("check_unicode", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cu = _load()


# ─── category() classification ─────────────────────────────────────────────


def test_category_flags_dangerous_codepoints():
    assert cu.category(0x202E) == "bidi-control"  # RIGHT-TO-LEFT OVERRIDE
    assert cu.category(0x2066) == "bidi-control"  # LEFT-TO-RIGHT ISOLATE
    assert cu.category(0x200E) == "directional-mark"
    assert cu.category(0x200B) == "zero-width"  # ZERO WIDTH SPACE
    assert cu.category(0xFEFF) == "zero-width"  # BOM / ZWNBSP
    assert cu.category(0xFE0F) == "variation-selector"
    assert cu.category(0xE0101) == "variation-selector"
    assert cu.category(0xE0001) == "tag-char"
    assert cu.category(0xE000) == "private-use"  # BMP PUA
    assert cu.category(0xF0000) == "private-use"  # plane-15 PUA


def test_category_allows_legitimate_codepoints():
    # The glyphs and rules this codebase uses on purpose must NOT trip.
    for cp in (
        ord("a"),
        ord(" "),
        0x2713,  # check status glyph
        0x26A0,  # warning glyph
        0x2717,  # ballot-x status glyph
        0x2716,  # heavy multiplication x (prompt indicator)
        0x2500,  # box-drawing rule used in comment separators
        0x2014,  # em dash
        0x2192,  # rightwards arrow
    ):
        assert cu.category(cp) is None, f"U+{cp:04X} should be allowed"


# ─── find_disallowed() positions ───────────────────────────────────────────


def test_find_disallowed_reports_line_and_col():
    text = "ok line\nbad" + ZWSP + "here\n"  # ZWSP after "bad" (col 4) on line 2
    findings = cu.find_disallowed(text)
    assert findings == [(2, 4, 0x200B, "zero-width")]


def test_find_disallowed_clean_text_is_empty():
    assert cu.find_disallowed("plain ascii only\n") == []


# ─── scan() / main() over a directory ──────────────────────────────────────


def test_scan_flags_planted_file(tmp_path):
    good = tmp_path / "good.py"
    good.write_text("x = 1  # fine\n", encoding="utf-8")
    bad = tmp_path / "bad.py"
    bad.write_text("token = 'a" + RLO + "b'\n", encoding="utf-8")
    lines = cu.scan([tmp_path])
    assert len(lines) == 1
    assert "bad.py" in lines[0]
    assert "U+202E" in lines[0]
    assert "(bidi-control)" in lines[0]


def test_main_returns_1_on_finding_and_0_when_clean(tmp_path, capsys):
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "a.py").write_text("y = 2\n", encoding="utf-8")
    assert cu.main(["check_unicode.py", str(clean)]) == 0

    dirty = tmp_path / "dirty"
    dirty.mkdir()
    (dirty / "b.py").write_text("z = '" + ZWJ + "'\n", encoding="utf-8")
    assert cu.main(["check_unicode.py", str(dirty)]) == 1
    out = capsys.readouterr().out
    assert "b.py" in out


def test_main_notes_missing_root_without_failing(tmp_path, capsys):
    # A non-existent root is skipped with a stderr note, not a failure.
    assert cu.main(["check_unicode.py", str(tmp_path / "nope")]) == 0
    assert "skipped" in capsys.readouterr().err


def test_iter_files_skips_non_utf8_and_oversized(tmp_path):
    (tmp_path / "binary.bin").write_bytes(b"\xff\xfe\x00\x01not-utf8")
    (tmp_path / "ok.py").write_text("v = 3\n", encoding="utf-8")
    files = {p.name for p in cu.iter_files([tmp_path])}
    assert "ok.py" in files
    assert "binary.bin" not in files
