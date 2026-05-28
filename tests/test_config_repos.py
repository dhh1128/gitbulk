"""Tests for config/repos.py (this.i node rj4pwn7k)."""

from __future__ import annotations

import logging
import subprocess
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
    entries, _skipped = load_repos(path, code_root=code_root)
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
    entries, _skipped = load_repos(path, code_root=tmp_path / "code")
    assert [e.slug for e in entries] == [
        "dhh1128/gitbulk",
        "provenant-dev/origin-platform",
    ]
    # Source lines reflect the original file lines, not the de-commented sequence
    assert entries[0].source_line == 4
    assert entries[1].source_line == 7


def test_inline_comment_stripped(tmp_path):
    path = _write_repos(tmp_path, "dhh1128/gitbulk # personal triage tool\n")
    entries, _skipped = load_repos(path, code_root=tmp_path / "code")
    assert len(entries) == 1
    assert entries[0].slug == "dhh1128/gitbulk"


# ─── Defaults ──────────────────────────────────────────────────────────────


def test_path_default_uses_paths_module(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    repos_file = tmp_path / "cfg" / "gitbulk" / "repos.txt"
    repos_file.parent.mkdir(parents=True)
    repos_file.write_text("dhh1128/gitbulk\n")
    entries, _skipped = load_repos(code_root=tmp_path / "code")
    assert len(entries) == 1


def test_code_root_default_is_home_code(tmp_path):
    path = _write_repos(tmp_path, "dhh1128/gitbulk\n")
    entries, _skipped = load_repos(path)  # no code_root → defaults to Path.home() / "code"
    assert entries[0].local_path == Path.home() / "code" / "gitbulk"


# ─── Malformed slug → SkippedEntry (not raise) ─────────────────────────────


@pytest.mark.parametrize(
    "bad_line",
    [
        "no-slash",
        "a/b/c",
        "trailing-slash/",
        "has spaces/in-owner",
        "owner/has spaces",
    ],
)
def test_malformed_slug_becomes_skipped_entry(tmp_path, bad_line):
    """Bare slug form: anything that doesn't match the GitHub regex
    becomes a SkippedEntry rather than aborting the load (one typo
    shouldn't block a 175-line repos.txt)."""
    path = _write_repos(tmp_path, f"dhh1128/gitbulk\n{bad_line}\n")
    entries, skipped = load_repos(path, code_root=tmp_path / "code")
    # Good line still loaded
    assert len(entries) == 1
    assert entries[0].slug == "dhh1128/gitbulk"
    # Bad line surfaced as SkippedEntry
    assert len(skipped) == 1
    assert skipped[0].lineno == 2
    assert skipped[0].raw == bad_line
    assert "does not match the expected GitHub form" in skipped[0].reason


def test_skipped_entry_includes_lineno_and_raw(tmp_path):
    path = _write_repos(tmp_path, "dhh1128/gitbulk\nbroken-line\n")
    entries, skipped = load_repos(path, code_root=tmp_path / "code")
    assert len(entries) == 1
    assert len(skipped) == 1
    assert skipped[0].lineno == 2
    assert "broken-line" in skipped[0].raw
    assert "does not match" in skipped[0].reason


def test_double_dot_segment_becomes_skipped_entry(tmp_path):
    """Security-hawk F1: a `..` segment is filtered (still rejected,
    just non-fatally now)."""
    path = _write_repos(tmp_path, "aa/..\n")
    entries, skipped = load_repos(path, code_root=tmp_path / "code")
    assert entries == []
    assert len(skipped) == 1
    assert "forbidden path segment" in skipped[0].reason


def test_security_hardened_slug_regex_skips_in_repos(tmp_path):
    """Security-hawk F1: the tightened regex rejects shapes the original
    `[^/\\s]+/[^/\\s]+` accepted (leading hyphen owner, @ in owner)."""
    for bad in ("-leading/ok", "owner@bad/ok"):
        path = _write_repos(tmp_path, f"{bad}\n")
        entries, skipped = load_repos(path, code_root=tmp_path / "code")
        assert entries == []
        assert len(skipped) == 1
        assert "does not match the expected GitHub form" in skipped[0].reason


# ─── Duplicate slugs ───────────────────────────────────────────────────────


def test_duplicate_slug_keeps_first_silently(tmp_path, caplog):
    """First-wins dedup is silent (DEBUG-only) per the design that
    duplicates are harmless and don't warrant a WARNING."""
    content = "dhh1128/gitbulk\nprovenant-dev/origin-platform\ndhh1128/gitbulk\n"
    path = _write_repos(tmp_path, content)
    with caplog.at_level(logging.WARNING, logger="gitbulk.config"):
        entries, _skipped = load_repos(path, code_root=tmp_path / "code")
    assert len(entries) == 2
    assert entries[0].slug == "dhh1128/gitbulk"
    assert entries[0].source_line == 1
    # No WARNING surfaced — duplicates are silent.
    assert not any("duplicate slug" in rec.message for rec in caplog.records)


def test_duplicate_slug_records_debug_log(tmp_path, caplog):
    """Debug-level record is still available for anyone investigating
    'why isn't my entry being seen?'"""
    content = "dhh1128/gitbulk\ndhh1128/gitbulk\n"
    path = _write_repos(tmp_path, content)
    with caplog.at_level(logging.DEBUG, logger="gitbulk.config"):
        load_repos(path, code_root=tmp_path / "code")
    assert any("duplicate slug" in rec.message for rec in caplog.records)


# ─── Edge cases ────────────────────────────────────────────────────────────


def test_empty_file_returns_empty_list(tmp_path):
    path = _write_repos(tmp_path, "")
    assert load_repos(path, code_root=tmp_path / "code") == ([], [])


def test_only_comments_returns_empty_list(tmp_path):
    path = _write_repos(tmp_path, "# just a comment\n# another\n")
    assert load_repos(path, code_root=tmp_path / "code") == ([], [])


# ─── Missing repos.txt → friendly ConfigError ──────────────────────────────


def test_missing_repos_txt_raises_configerror(tmp_path):
    """User onboarding: the first time gitbulk runs, there's no repos.txt.
    Surface a friendly ConfigError pointing at the file location and the
    example, not a bare FileNotFoundError."""
    missing = tmp_path / "no-such-repos.txt"
    with pytest.raises(ConfigError, match="repos.txt not found"):
        load_repos(missing, code_root=tmp_path / "code")


# ─── URL form: canonicalize to slug ────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/dhh1128/gitbulk",
        "https://github.com/dhh1128/gitbulk.git",
        "https://github.com/dhh1128/gitbulk/",
        "http://github.com/dhh1128/gitbulk",
        "git@github.com:dhh1128/gitbulk",
        "git@github.com:dhh1128/gitbulk.git",
        "ssh://git@github.com/dhh1128/gitbulk",
        "ssh://git@github.com/dhh1128/gitbulk.git",
    ],
)
def test_url_form_canonicalizes_to_slug(tmp_path, url):
    """All accepted URL forms parse to the same canonical slug + the
    default <code-root>/<repo-name> local path."""
    path = _write_repos(tmp_path, f"{url}\n")
    code_root = tmp_path / "code"
    entries, _skipped = load_repos(path, code_root=code_root)
    assert len(entries) == 1
    assert entries[0].slug == "dhh1128/gitbulk"
    assert entries[0].owner == "dhh1128"
    assert entries[0].name == "gitbulk"
    assert entries[0].local_path == code_root / "gitbulk"


def test_url_form_non_github_becomes_skipped_entry(tmp_path):
    """A URL pointing at a non-GitHub host becomes a SkippedEntry —
    the rest of the file still loads."""
    path = _write_repos(tmp_path, "https://gitlab.com/foo/bar\n")
    entries, skipped = load_repos(path, code_root=tmp_path / "code")
    assert entries == []
    assert len(skipped) == 1
    assert "not a recognized GitHub remote form" in skipped[0].reason


def test_url_form_extracts_slug_with_security_validation(tmp_path):
    """A URL that decodes to a malformed slug (somehow) still trips
    the security validation — defense in depth, becomes SkippedEntry."""
    path = _write_repos(tmp_path, "https://github.com/-bad/repo\n")
    entries, skipped = load_repos(path, code_root=tmp_path / "code")
    assert entries == []
    assert len(skipped) == 1
    assert "does not match the expected GitHub form" in skipped[0].reason


# ─── Path form: resolve via git remote ─────────────────────────────────────


def _make_fake_git_repo(parent: Path, name: str, origin_url: str) -> Path:
    """Create a real git repo with a configured origin remote."""
    repo = parent / name
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", origin_url],
        check=True,
        capture_output=True,
    )
    return repo


def test_path_form_resolves_slug_from_origin_remote(tmp_path):
    """An absolute path with a configured GitHub origin yields the
    correct slug, AND uses the explicit path as local_path (not
    <code-root>/<basename>)."""
    repo = _make_fake_git_repo(
        tmp_path, "weirdname", "https://github.com/dhh1128/gitbulk.git"
    )
    path = _write_repos(tmp_path, f"{repo}\n")
    code_root = tmp_path / "code"
    entries, _skipped = load_repos(path, code_root=code_root)
    assert len(entries) == 1
    assert entries[0].slug == "dhh1128/gitbulk"
    # local_path is the EXPLICIT path, not code_root/<basename>:
    assert entries[0].local_path == repo.resolve()
    # name is still derived from the slug, not the directory basename:
    assert entries[0].name == "gitbulk"


def test_path_form_handles_tilde_expansion(tmp_path, monkeypatch):
    repo = _make_fake_git_repo(
        tmp_path, "myrepo", "git@github.com:dhh1128/gitbulk.git"
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    path = _write_repos(tmp_path, "~/myrepo\n")
    entries, _skipped = load_repos(path, code_root=tmp_path / "code")
    assert entries[0].slug == "dhh1128/gitbulk"
    assert entries[0].local_path == repo.resolve()


def test_path_form_nonexistent_becomes_skipped(tmp_path):
    path = _write_repos(tmp_path, "/nonexistent/path/to/repo\n")
    entries, skipped = load_repos(path, code_root=tmp_path / "code")
    assert entries == []
    assert len(skipped) == 1
    assert "does not exist" in skipped[0].reason


def test_path_form_not_a_git_repo_becomes_skipped(tmp_path):
    plain = tmp_path / "plain-dir"
    plain.mkdir()
    path = _write_repos(tmp_path, f"{plain}\n")
    entries, skipped = load_repos(path, code_root=tmp_path / "code")
    assert entries == []
    assert "not a git repository" in skipped[0].reason


def test_path_form_no_origin_remote_becomes_skipped(tmp_path):
    repo = tmp_path / "no-origin-repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    path = _write_repos(tmp_path, f"{repo}\n")
    entries, skipped = load_repos(path, code_root=tmp_path / "code")
    assert entries == []
    assert "no 'origin' remote" in skipped[0].reason


def test_path_form_non_github_origin_becomes_skipped(tmp_path):
    repo = _make_fake_git_repo(
        tmp_path, "gitlab-repo", "https://gitlab.com/foo/bar.git"
    )
    path = _write_repos(tmp_path, f"{repo}\n")
    entries, skipped = load_repos(path, code_root=tmp_path / "code")
    assert entries == []
    assert "not a recognized GitHub remote" in skipped[0].reason


def test_path_form_with_malformed_origin_slug_becomes_skipped(tmp_path):
    """Defense in depth: a path whose origin URL parses but resolves to
    a malformed slug (e.g. leading hyphen owner) still gets skipped."""
    repo = _make_fake_git_repo(
        tmp_path, "weirdorigin", "https://github.com/-bad/repo.git"
    )
    path = _write_repos(tmp_path, f"{repo}\n")
    entries, skipped = load_repos(path, code_root=tmp_path / "code")
    assert entries == []
    assert len(skipped) == 1
    assert "does not match the expected GitHub form" in skipped[0].reason


def test_one_bad_entry_does_not_block_good_entries(tmp_path):
    """Load-bearing test for the skip-with-reason design: a bad entry
    among 175 good ones doesn't abort the whole load."""
    content = (
        "dhh1128/good-one\n"
        "/nonexistent/bad-one\n"
        "provenant-dev/another-good\n"
    )
    path = _write_repos(tmp_path, content)
    entries, skipped = load_repos(path, code_root=tmp_path / "code")
    # Both good entries loaded; bad one captured separately.
    assert [e.slug for e in entries] == ["dhh1128/good-one", "provenant-dev/another-good"]
    assert len(skipped) == 1
    assert skipped[0].lineno == 2


def test_mixed_forms_in_one_repos_txt(tmp_path):
    """Slug + URL + path all in the same file work together."""
    repo = _make_fake_git_repo(
        tmp_path, "explicit-path", "https://github.com/dhh1128/intent.git"
    )
    content = (
        "dhh1128/gitbulk\n"  # canonical
        "https://github.com/provenant-dev/origin-platform\n"  # URL
        f"{repo}\n"  # path
    )
    path = _write_repos(tmp_path, content)
    code_root = tmp_path / "code"
    entries, _skipped = load_repos(path, code_root=code_root)
    assert [e.slug for e in entries] == [
        "dhh1128/gitbulk",
        "provenant-dev/origin-platform",
        "dhh1128/intent",
    ]
    # path-form entry uses the explicit path, the others use code_root.
    assert entries[0].local_path == code_root / "gitbulk"
    assert entries[1].local_path == code_root / "origin-platform"
    assert entries[2].local_path == repo.resolve()


def test_duplicate_slug_across_forms_keeps_first(tmp_path):
    """A slug entry followed by a URL pointing at the same slug is
    deduped to the slug entry (first wins, silent)."""
    content = (
        "dhh1128/gitbulk\n"
        "https://github.com/dhh1128/gitbulk\n"
    )
    path = _write_repos(tmp_path, content)
    entries, _skipped = load_repos(path, code_root=tmp_path / "code")
    assert len(entries) == 1
    assert entries[0].source_line == 1
