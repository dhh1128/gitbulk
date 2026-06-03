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


# ─── clear_if_matches (node aklr5pq3 trigger 1) ────────────────────────────


def test_clear_if_matches_clears_and_returns_payload_on_exact_match(isolated_cache):
    sentinel.set_attention(2, "report", "RID-1", "4 PRs need attention")
    cleared = sentinel.clear_if_matches("report", "RID-1")
    assert cleared is not None
    assert cleared["subcommand"] == "report"
    assert cleared["runid"] == "RID-1"
    assert not sentinel.has_attention()


def test_clear_if_matches_leaves_sentinel_on_runid_mismatch(isolated_cache):
    sentinel.set_attention(2, "report", "RID-1", "summary")
    assert sentinel.clear_if_matches("report", "RID-2") is None
    assert sentinel.has_attention()


def test_clear_if_matches_leaves_sentinel_on_subcommand_mismatch(isolated_cache):
    sentinel.set_attention(2, "dispatch", "RID-1", "agent failed")
    # Viewing a report run must not clear a dispatch-set sentinel.
    assert sentinel.clear_if_matches("report", "RID-1") is None
    assert sentinel.has_attention()


def test_clear_if_matches_never_matches_fallback_runid(isolated_cache):
    # The "?" placeholder written by _maybe_set_attention requires `ack`.
    sentinel.set_attention(2, "report", "?", "fallback")
    assert sentinel.clear_if_matches("report", "?") is None
    assert sentinel.has_attention()


def test_clear_if_matches_returns_none_when_absent(isolated_cache):
    assert sentinel.clear_if_matches("report", "RID-1") is None


# ─── clear_if_superseded (node aklr5pq3 trigger 3) ─────────────────────────


def test_clear_if_superseded_clears_same_subcommand(isolated_cache):
    sentinel.set_attention(2, "report", "OLD-RID", "stale")
    cleared = sentinel.clear_if_superseded("report")
    assert cleared is not None
    assert cleared["subcommand"] == "report"
    assert not sentinel.has_attention()


def test_clear_if_superseded_leaves_cross_subcommand(isolated_cache):
    sentinel.set_attention(2, "dispatch", "RID", "agent failed")
    # A clean report run must not clear a dispatch failure sentinel.
    assert sentinel.clear_if_superseded("report") is None
    assert sentinel.has_attention()


def test_clear_if_superseded_returns_none_when_absent(isolated_cache):
    assert sentinel.clear_if_superseded("report") is None


# ─── clear_and_describe (node aklr5pq3 trigger 2) ──────────────────────────


def test_clear_and_describe_clears_any_parseable_sentinel(isolated_cache):
    sentinel.set_attention(3, "merge", "RID", "1 repo skipped")
    cleared = sentinel.clear_and_describe()
    assert cleared is not None
    assert cleared["subcommand"] == "merge"
    assert not sentinel.has_attention()


def test_clear_and_describe_returns_none_when_absent(isolated_cache):
    assert sentinel.clear_and_describe() is None


def test_clear_and_describe_leaves_unparseable_sentinel(isolated_cache):
    # Legacy/corrupt format: parse_attention() returns None, so the bare
    # `show` dashboard path leaves it for `ack` to clear unconditionally.
    paths.attention_sentinel().write_text("2 report RID legacy\n")
    assert sentinel.clear_and_describe() is None
    assert sentinel.has_attention()
