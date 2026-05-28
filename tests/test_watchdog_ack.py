"""Tests for the post-merge watchdog ack cache (this.i node ``yhwagcvw``)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import yaml

from gitbulk import paths
from gitbulk.watchdog_ack import load_acked, record_ack


@pytest.fixture(autouse=True)
def isolated_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    paths.ensure_directories()
    return tmp_path


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── load_acked ───────────────────────────────────────────────────────────


def test_load_acked_returns_empty_when_file_missing():
    assert load_acked() == set()


def test_load_acked_returns_empty_when_file_unparseable():
    (paths.cache_dir() / "watchdog-acked.yaml").write_text("[not a mapping")
    assert load_acked() == set()


def test_load_acked_returns_empty_on_schema_mismatch():
    """A future-version cache file is treated as opaque; we fall back
    to re-fetching rather than guessing."""
    (paths.cache_dir() / "watchdog-acked.yaml").write_text(
        yaml.safe_dump({"version": 999, "acked": []})
    )
    assert load_acked() == set()


def test_load_acked_returns_empty_when_top_not_a_mapping():
    """Defensive: top-level is a list, string, etc."""
    (paths.cache_dir() / "watchdog-acked.yaml").write_text(yaml.safe_dump([1, 2]))
    assert load_acked() == set()


def test_load_acked_returns_empty_when_acked_field_not_list():
    (paths.cache_dir() / "watchdog-acked.yaml").write_text(
        yaml.safe_dump({"version": 1, "acked": "not-a-list"})
    )
    assert load_acked() == set()


def test_load_acked_skips_non_dict_entries():
    (paths.cache_dir() / "watchdog-acked.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "acked": [
                    "not-a-dict",
                    {"slug": "a/b", "sha": "abc"},
                ],
            }
        )
    )
    assert load_acked() == {("a/b", "abc")}


def test_load_acked_skips_entries_with_non_string_slug_or_sha():
    (paths.cache_dir() / "watchdog-acked.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "acked": [
                    {"slug": 42, "sha": "abc"},
                    {"slug": "a/b", "sha": 42},
                    {"slug": "good/repo", "sha": "validsha"},
                ],
            }
        )
    )
    assert load_acked() == {("good/repo", "validsha")}


# ─── record_ack ───────────────────────────────────────────────────────────


def test_record_ack_creates_file_and_persists():
    now = _now()
    record_ack("a/b", "deadbeef" * 5, now)
    assert load_acked() == {("a/b", "deadbeef" * 5)}


def test_record_ack_is_idempotent():
    """Re-ack of the same pair doesn't duplicate."""
    now = _now()
    record_ack("a/b", "abc", now)
    record_ack("a/b", "abc", now + timedelta(minutes=5))
    assert load_acked() == {("a/b", "abc")}
    raw = yaml.safe_load((paths.cache_dir() / "watchdog-acked.yaml").read_text())
    assert len(raw["acked"]) == 1


def test_record_ack_prunes_entries_older_than_seven_days():
    """A pre-existing entry from 10 days ago disappears on the next write."""
    now = _now()
    old = now - timedelta(days=10)
    (paths.cache_dir() / "watchdog-acked.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "acked": [
                    {"slug": "stale/repo", "sha": "olddata", "acked_at": old.isoformat()},
                ],
            }
        )
    )
    record_ack("fresh/repo", "newsha", now)
    assert load_acked() == {("fresh/repo", "newsha")}


def test_record_ack_keeps_entries_within_seven_days():
    now = _now()
    recent = now - timedelta(days=3)
    (paths.cache_dir() / "watchdog-acked.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "acked": [
                    {"slug": "kept/repo", "sha": "stillok", "acked_at": recent.isoformat()},
                ],
            }
        )
    )
    record_ack("new/repo", "newsha", now)
    assert load_acked() == {("kept/repo", "stillok"), ("new/repo", "newsha")}


def test_record_ack_tolerates_unparseable_timestamp_by_dropping():
    """Defensive: a malformed acked_at on disk shouldn't crash the
    next record_ack — drop that entry conservatively."""
    now = _now()
    (paths.cache_dir() / "watchdog-acked.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "acked": [
                    {"slug": "bad/repo", "sha": "x", "acked_at": "not-a-date"},
                    {"slug": "ok/repo", "sha": "y", "acked_at": now.isoformat()},
                ],
            }
        )
    )
    record_ack("new/repo", "z", now)
    # bad/repo dropped; ok/repo kept; new/repo added
    assert load_acked() == {("ok/repo", "y"), ("new/repo", "z")}


def test_record_ack_tolerates_missing_acked_at():
    """An entry without acked_at is kept defensively (not pruned)."""
    now = _now()
    (paths.cache_dir() / "watchdog-acked.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "acked": [
                    {"slug": "no/timestamp", "sha": "x"},
                ],
            }
        )
    )
    record_ack("new/repo", "y", now)
    assert load_acked() == {("no/timestamp", "x"), ("new/repo", "y")}


def test_record_ack_tolerates_unparseable_yaml_existing_file():
    """If the existing file is corrupt, treat it as empty and rewrite."""
    (paths.cache_dir() / "watchdog-acked.yaml").write_text("[corrupt")
    now = _now()
    record_ack("fresh/repo", "x", now)
    assert load_acked() == {("fresh/repo", "x")}


def test_record_ack_skips_non_dict_entries_in_existing_file():
    (paths.cache_dir() / "watchdog-acked.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "acked": [
                    "not-a-dict",
                    {"slug": "kept/repo", "sha": "y", "acked_at": _now().isoformat()},
                ],
            }
        )
    )
    record_ack("new/repo", "z", _now())
    assert load_acked() == {("kept/repo", "y"), ("new/repo", "z")}


def test_record_ack_handles_wrong_schema_in_existing_file():
    """An on-disk file with version != 1 is treated as empty."""
    (paths.cache_dir() / "watchdog-acked.yaml").write_text(
        yaml.safe_dump({"version": 999, "acked": [{"slug": "x/y", "sha": "ignored"}]})
    )
    now = _now()
    record_ack("new/repo", "z", now)
    # Old entry dropped (schema mismatch), only new one remains.
    assert load_acked() == {("new/repo", "z")}


def test_record_ack_handles_acked_field_not_a_list():
    """Defensive: acked field is corrupt (a string instead of a list)."""
    (paths.cache_dir() / "watchdog-acked.yaml").write_text(
        yaml.safe_dump({"version": 1, "acked": "corrupt-not-a-list"})
    )
    now = _now()
    record_ack("new/repo", "z", now)
    assert load_acked() == {("new/repo", "z")}
