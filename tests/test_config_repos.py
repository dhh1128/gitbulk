"""Tests for config/repos.py (this.i node rj4pwn7k)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from gitbulk.config.repos import ConfigError, RepoEntry, load_repos


def _write_repos(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "repos.txt"
    p.write_text(content)
    return p


# ─── Happy path ─────────────────────────────────────────────────────────────


def test_basic_load(tmp_path):
    path = _write_repos(tmp_path, "dhh1128/gitbulk\nprovenant-dev/origin-platform\n")
    code_root = tmp_path / "code"
    entries = load_repos(path, code_root=code_root)
    assert len(entries) == 2
    assert entries[0] == RepoEntry(
        slug="dhh1128/gitbulk",
        owner="dhh1128",
        name="gitbulk",
        local_path=code_root / "gitbulk",
        source_line=1,
    )
    assert entries[1].slug == "provenant-dev/origin-platform"
    assert entries[1].local_path == code_root / "origin-platform"


def test_comments_and_blank_lines_ignored(tmp_path):
    content = """
# leading comment

dhh1128/gitbulk
   # indented comment

provenant-dev/origin-platform   # trailing comment with whitespace
# trailing comment

"""
    path = _write_repos(tmp_path, content)
    entries = load_repos(path, code_root=tmp_path / "code")
    assert [e.slug for e in entries] == [
        "dhh1128/gitbulk",
        "provenant-dev/origin-platform",
    ]
    # Source lines reflect the original file lines, not the de-commented sequence
    assert entries[0].source_line == 4
    assert entries[1].source_line == 7


def test_inline_comment_stripped(tmp_path):
    path = _write_repos(tmp_path, "dhh1128/gitbulk # personal triage tool\n")
    entries = load_repos(path, code_root=tmp_path / "code")
    assert len(entries) == 1
    assert entries[0].slug == "dhh1128/gitbulk"


# ─── Defaults ──────────────────────────────────────────────────────────────


def test_path_default_uses_paths_module(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    repos_file = tmp_path / "cfg" / "gitbulk" / "repos.txt"
    repos_file.parent.mkdir(parents=True)
    repos_file.write_text("dhh1128/gitbulk\n")
    entries = load_repos(code_root=tmp_path / "code")
    assert len(entries) == 1


def test_code_root_default_is_home_code(tmp_path):
    path = _write_repos(tmp_path, "dhh1128/gitbulk\n")
    entries = load_repos(path)  # no code_root → defaults to Path.home() / "code"
    assert entries[0].local_path == Path.home() / "code" / "gitbulk"


# ─── Malformed slug → ConfigError ──────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_line",
    [
        "no-slash",
        "a/b/c",
        "/leading-slash",
        "trailing-slash/",
        "/",
        "has spaces/in-owner",
        "owner/has spaces",
    ],
)
def test_malformed_slug_raises_configerror(tmp_path, bad_line):
    path = _write_repos(tmp_path, f"dhh1128/gitbulk\n{bad_line}\n")
    with pytest.raises(ConfigError, match="malformed slug"):
        load_repos(path, code_root=tmp_path / "code")


def test_configerror_message_includes_file_and_line(tmp_path):
    path = _write_repos(tmp_path, "dhh1128/gitbulk\nbroken-line\n")
    with pytest.raises(ConfigError) as exc:
        load_repos(path, code_root=tmp_path / "code")
    msg = str(exc.value)
    assert str(path) in msg
    assert ":2:" in msg
    assert "broken-line" in msg


def test_double_dot_segment_in_repos_raises(tmp_path):
    """Security-hawk F1 (2026-05-28): a malicious repos.txt entry with a
    `..` segment is rejected with a clear ConfigError."""
    # `aa/..` passes the regex (`..` matches the repo character class) but
    # the forbidden-segments check rejects it.
    path = _write_repos(tmp_path, "aa/..\n")
    with pytest.raises(ConfigError, match="forbidden path segment"):
        load_repos(path, code_root=tmp_path / "code")


def test_security_hardened_slug_regex_rejects_in_repos(tmp_path):
    """Security-hawk F1: the tightened regex rejects shapes the original
    `[^/\\s]+/[^/\\s]+` accepted (leading hyphen owner, @ in owner)."""
    for bad in ("-leading/ok", "owner@bad/ok"):
        path = _write_repos(tmp_path, f"{bad}\n")
        with pytest.raises(ConfigError, match="malformed slug"):
            load_repos(path, code_root=tmp_path / "code")


# ─── Duplicate slugs ───────────────────────────────────────────────────────


def test_duplicate_slug_keeps_first_and_warns(tmp_path, caplog):
    content = "dhh1128/gitbulk\nprovenant-dev/origin-platform\ndhh1128/gitbulk\n"
    path = _write_repos(tmp_path, content)
    with caplog.at_level(logging.WARNING, logger="gitbulk.config"):
        entries = load_repos(path, code_root=tmp_path / "code")
    assert len(entries) == 2
    assert entries[0].slug == "dhh1128/gitbulk"
    assert entries[0].source_line == 1
    assert any("duplicate slug" in rec.message for rec in caplog.records)


# ─── Edge cases ────────────────────────────────────────────────────────────


def test_empty_file_returns_empty_list(tmp_path):
    path = _write_repos(tmp_path, "")
    assert load_repos(path, code_root=tmp_path / "code") == []


def test_only_comments_returns_empty_list(tmp_path):
    path = _write_repos(tmp_path, "# just a comment\n# another\n")
    assert load_repos(path, code_root=tmp_path / "code") == []


# ─── Missing repos.txt → friendly ConfigError ──────────────────────────────


def test_missing_repos_txt_raises_configerror(tmp_path):
    """User onboarding: the first time gitbulk runs, there's no repos.txt.
    Surface a friendly ConfigError pointing at the file location and the
    example, not a bare FileNotFoundError."""
    missing = tmp_path / "no-such-repos.txt"
    with pytest.raises(ConfigError, match="repos.txt not found"):
        load_repos(missing, code_root=tmp_path / "code")
