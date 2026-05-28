"""Tests for sentinel.py (this.i nodes snk7p4qm + schv4nrm)."""

from __future__ import annotations

import json

import pytest

from gitbulk import paths, sentinel
from gitbulk.sentinel import SCHEMA_VERSION


@pytest.fixture
def isolated_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    paths.ensure_directories()
    return tmp_path


# ─── set_attention writes JSON ─────────────────────────────────────────────


def test_set_attention_writes_json_with_schema_version(isolated_cache):
    sentinel.set_attention(2, "report", "20260527T194501Z", "4 PRs need attention")
    text = paths.attention_sentinel().read_text()
    # File must end with newline (cron `cat` ergonomics)
    assert text.endswith("\n")
    payload = json.loads(text)
    assert payload == {
        "v": SCHEMA_VERSION,
        "exit_code": 2,
        "subcommand": "report",
        "runid": "20260527T194501Z",
        "summary": "4 PRs need attention",
    }


def test_set_attention_overwrites_existing(isolated_cache):
    sentinel.set_attention(2, "report", "20260527T120000Z", "first")
    sentinel.set_attention(3, "merge", "20260527T130000Z", "second")
    payload = json.loads(paths.attention_sentinel().read_text())
    assert payload["subcommand"] == "merge"
    assert payload["exit_code"] == 3
    assert payload["summary"] == "second"


# ─── clear / has / read ────────────────────────────────────────────────────


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


def test_read_attention_returns_raw_text(isolated_cache):
    sentinel.set_attention(3, "merge", "RID", "9 repos skipped")
    text = sentinel.read_attention()
    assert text is not None
    # Raw text is JSON
    assert json.loads(text)["summary"] == "9 repos skipped"


def test_read_attention_returns_none_when_absent(isolated_cache):
    assert sentinel.read_attention() is None


# ─── parse_attention ───────────────────────────────────────────────────────


def test_parse_attention_returns_dict_when_present(isolated_cache):
    sentinel.set_attention(2, "report", "20260527T194501Z", "summary text")
    parsed = sentinel.parse_attention()
    assert parsed == {
        "v": SCHEMA_VERSION,
        "exit_code": 2,
        "subcommand": "report",
        "runid": "20260527T194501Z",
        "summary": "summary text",
    }


def test_parse_attention_returns_none_when_absent(isolated_cache):
    assert sentinel.parse_attention() is None


def test_parse_attention_returns_none_for_legacy_whitespace_format(isolated_cache):
    """A pre-Phase-1D-format sentinel left over on disk parses as None
    rather than raising. The new gitbulk treats it as 'no usable sentinel.'"""
    paths.attention_sentinel().write_text("2 report 20260527T194501Z 4 PRs need attention\n")
    assert sentinel.parse_attention() is None


def test_parse_attention_returns_none_for_non_dict_json(isolated_cache):
    paths.attention_sentinel().write_text("[1, 2, 3]\n")
    assert sentinel.parse_attention() is None
