"""Tests for sentinel.py (this.i node snk7p4qm)."""

from __future__ import annotations

import pytest

from gitbulk import paths, sentinel


@pytest.fixture
def isolated_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    paths.ensure_directories()
    return tmp_path


def test_set_attention_creates_file_with_expected_line(isolated_cache):
    sentinel.set_attention(2, "report", "20260527T194501Z", "4 PRs need attention")
    text = paths.attention_sentinel().read_text()
    assert text == "2 report 20260527T194501Z 4 PRs need attention\n"


def test_set_attention_overwrites_existing(isolated_cache):
    sentinel.set_attention(2, "report", "20260527T120000Z", "first")
    sentinel.set_attention(3, "merge", "20260527T130000Z", "second")
    text = paths.attention_sentinel().read_text()
    assert text == "3 merge 20260527T130000Z second\n"


def test_clear_attention_when_present_returns_true(isolated_cache):
    sentinel.set_attention(2, "report", "RID", "summary")
    assert sentinel.clear_attention() is True
    assert not paths.attention_sentinel().exists()


def test_clear_attention_when_absent_returns_false(isolated_cache):
    assert sentinel.clear_attention() is False


def test_has_attention_true_after_set(isolated_cache):
    sentinel.set_attention(2, "report", "RID", "summary")
    assert sentinel.has_attention() is True


def test_has_attention_false_when_absent(isolated_cache):
    assert sentinel.has_attention() is False


def test_read_attention_returns_content(isolated_cache):
    sentinel.set_attention(3, "merge", "RID", "9 repos skipped")
    assert sentinel.read_attention() == "3 merge RID 9 repos skipped\n"


def test_read_attention_returns_none_when_absent(isolated_cache):
    assert sentinel.read_attention() is None
