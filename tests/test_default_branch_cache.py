"""Tests for :mod:`gitbulk.default_branch_cache` (Stage 2 file cache)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import yaml

from gitbulk import paths
from gitbulk.default_branch_cache import (
    DEFAULT_TTL_DAYS,
    SCHEMA_VERSION,
    CachedBranch,
    is_fresh,
    load_cache,
    prime_default_branches,
    save_cache,
)
from gitbulk.gh import FakeGHClient


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    paths.ensure_directories()
    return tmp_path


def _now() -> datetime:
    return datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)


# ─── load / save round-trip ────────────────────────────────────────────────


def test_load_missing_file_returns_empty():
    assert load_cache() == {}


def test_save_then_load_round_trips():
    branches = {
        "a/b": CachedBranch("main", _now()),
        "c/d": CachedBranch("develop", _now() - timedelta(days=1)),
    }
    save_cache(branches)
    loaded = load_cache()
    assert set(loaded.keys()) == {"a/b", "c/d"}
    assert loaded["a/b"].branch == "main"
    assert loaded["c/d"].branch == "develop"
    # Timestamps survive as tz-aware UTC.
    assert loaded["a/b"].fetched_at == _now()


def test_save_leaves_no_tmp_file():
    save_cache({"a/b": CachedBranch("main", _now())})
    assert not paths.default_branch_cache_file().with_suffix(".yaml.tmp").exists()


# ─── load strictness ───────────────────────────────────────────────────────


def test_load_malformed_yaml_returns_empty():
    paths.default_branch_cache_file().write_text("[not: valid: yaml")
    assert load_cache() == {}


def test_load_wrong_schema_version_returns_empty():
    paths.default_branch_cache_file().write_text(
        yaml.safe_dump({"schema_version": 999, "branches": {"a/b": {}}})
    )
    assert load_cache() == {}


def test_load_top_level_not_dict_returns_empty():
    paths.default_branch_cache_file().write_text(yaml.safe_dump([1, 2, 3]))
    assert load_cache() == {}


def test_load_branches_not_dict_returns_empty():
    paths.default_branch_cache_file().write_text(
        yaml.safe_dump({"schema_version": SCHEMA_VERSION, "branches": "nope"})
    )
    assert load_cache() == {}


def test_load_skips_malformed_entries_keeps_good_ones():
    paths.default_branch_cache_file().write_text(
        yaml.safe_dump(
            {
                "schema_version": SCHEMA_VERSION,
                "branches": {
                    "good/repo": {
                        "branch": "main",
                        "fetched_at": _now().isoformat(),
                    },
                    "no/branch": {"fetched_at": _now().isoformat()},
                    "no/timestamp": {"branch": "main"},
                    "empty/branch": {
                        "branch": "",
                        "fetched_at": _now().isoformat(),
                    },
                    "bad/timestamp": {
                        "branch": "main",
                        "fetched_at": "not-a-date",
                    },
                    "naive/ts": {
                        "branch": "main",
                        "fetched_at": "2026-05-29T12:00:00",  # no tzinfo
                    },
                    "nonstr/slug-not-dict": "scalar",
                },
            }
        )
    )
    loaded = load_cache()
    assert set(loaded.keys()) == {"good/repo"}


def test_load_accepts_native_datetime_from_yaml():
    """PyYAML decodes bare ISO timestamps to datetime; we accept that."""
    paths.default_branch_cache_file().write_text(
        "schema_version: 1\n"
        "branches:\n"
        "  a/b:\n"
        "    branch: main\n"
        "    fetched_at: 2026-05-29 12:00:00+00:00\n"
    )
    loaded = load_cache()
    assert loaded["a/b"].branch == "main"
    assert loaded["a/b"].fetched_at.tzinfo is not None


def test_load_rejects_native_naive_datetime():
    """A naive (no-tz) native datetime is ambiguous → entry dropped."""
    paths.default_branch_cache_file().write_text(
        "schema_version: 1\n"
        "branches:\n"
        "  a/b:\n"
        "    branch: main\n"
        "    fetched_at: 2026-05-29 12:00:00\n"  # naive
    )
    assert load_cache() == {}


def test_load_skips_non_string_slug_key():
    paths.default_branch_cache_file().write_text(
        yaml.safe_dump(
            {
                "schema_version": SCHEMA_VERSION,
                "branches": {
                    123: {"branch": "main", "fetched_at": _now().isoformat()},
                    "ok/repo": {"branch": "main", "fetched_at": _now().isoformat()},
                },
            }
        )
    )
    loaded = load_cache()
    assert set(loaded.keys()) == {"ok/repo"}


# ─── is_fresh ──────────────────────────────────────────────────────────────


def test_is_fresh_within_ttl():
    cb = CachedBranch("main", _now() - timedelta(days=3))
    assert is_fresh(cb, 7, now=_now()) is True


def test_is_fresh_at_boundary_is_stale():
    """Exactly TTL old → stale (strict less-than)."""
    cb = CachedBranch("main", _now() - timedelta(days=7))
    assert is_fresh(cb, 7, now=_now()) is False


def test_is_fresh_past_ttl_is_stale():
    cb = CachedBranch("main", _now() - timedelta(days=10))
    assert is_fresh(cb, 7, now=_now()) is False


# ─── prime_default_branches orchestration ──────────────────────────────────


def test_prime_all_cold_fetches_everything():
    """No file cache → every slug is a miss → prefetch called for all,
    results persisted with `now` timestamps."""
    gh = FakeGHClient(default_branches={})
    prime_default_branches(
        gh, ["a/b", "c/d"], now=_now()
    )
    # Fake prefetch is a no-op (doesn't populate), so nothing resolved →
    # nothing persisted. Use the real-ish path below instead; here just
    # assert prefetch was attempted.
    assert gh.call_count["prefetch_default_branches"] == 1


def test_prime_seeds_fresh_entries_and_skips_prefetch_when_all_warm():
    """All slugs fresh in file cache → seed gh, NO prefetch call."""
    save_cache(
        {
            "a/b": CachedBranch("main", _now() - timedelta(days=1)),
            "c/d": CachedBranch("develop", _now() - timedelta(days=2)),
        }
    )
    gh = FakeGHClient(default_branches={})
    prime_default_branches(gh, ["a/b", "c/d"], now=_now())
    # Everything was warm → no prefetch.
    assert gh.call_count["prefetch_default_branches"] == 0
    # gh's cache was seeded so default_branch hits memory.
    assert gh.default_branch("a/b") == "main"
    assert gh.default_branch("c/d") == "develop"


def test_prime_stale_entries_trigger_prefetch():
    """An entry past TTL is treated as missing → prefetch called."""
    save_cache({"a/b": CachedBranch("main", _now() - timedelta(days=30))})
    gh = FakeGHClient(default_branches={})
    prime_default_branches(gh, ["a/b"], now=_now())
    assert gh.call_count["prefetch_default_branches"] == 1


def test_prime_mixed_warm_and_cold():
    """Warm slug is seeded (no fetch); cold slug is prefetched."""
    save_cache({"warm/repo": CachedBranch("main", _now() - timedelta(days=1))})
    # Fake resolves cold/repo via its default_branches map after the
    # no-op prefetch — seed it directly to simulate a successful fetch.
    gh = FakeGHClient(default_branches={"cold/repo": "trunk"})
    prime_default_branches(gh, ["warm/repo", "cold/repo"], now=_now())
    # Prefetch was called (cold/repo was missing from the file cache).
    assert gh.call_count["prefetch_default_branches"] == 1
    # warm/repo seeded from file:
    assert gh.default_branch("warm/repo") == "main"


def test_prime_persists_freshly_fetched_with_now_timestamp():
    """After a cold fetch, the resolved branch is written to the file
    cache stamped with `now`."""
    # Fake's default_branches map acts as the 'resolved' set that
    # cached_default_branches() reads back.
    gh = FakeGHClient(default_branches={"a/b": "main"})
    prime_default_branches(gh, ["a/b"], now=_now())
    persisted = load_cache()
    assert persisted["a/b"].branch == "main"
    assert persisted["a/b"].fetched_at == _now()


def test_prime_keeps_fresh_timestamp_unchanged_on_persist():
    """A warm entry's fetched_at is preserved (not bumped to now)."""
    original_ts = _now() - timedelta(days=2)
    save_cache({"a/b": CachedBranch("main", original_ts)})
    gh = FakeGHClient(default_branches={})
    prime_default_branches(gh, ["a/b"], now=_now())
    persisted = load_cache()
    assert persisted["a/b"].fetched_at == original_ts


def test_prime_drops_unresolvable_slug_from_cache():
    """A stale slug that the fetch can't resolve (deleted repo) is
    removed from the file cache, not kept serving a dead branch."""
    save_cache({"deleted/repo": CachedBranch("main", _now() - timedelta(days=30))})
    # Fake resolves nothing (empty map) → cached_default_branches() empty.
    gh = FakeGHClient(default_branches={})
    prime_default_branches(gh, ["deleted/repo"], now=_now())
    assert "deleted/repo" not in load_cache()


def test_prime_preserves_entries_for_slugs_not_in_this_run():
    """A different cron entry may use a different repos.txt subset.
    Slugs not in this run's list survive in the file."""
    save_cache(
        {
            "other/repo": CachedBranch("main", _now() - timedelta(days=1)),
            "this/repo": CachedBranch("main", _now() - timedelta(days=1)),
        }
    )
    gh = FakeGHClient(default_branches={})
    prime_default_branches(gh, ["this/repo"], now=_now())
    persisted = load_cache()
    # other/repo wasn't in slugs but is still there.
    assert "other/repo" in persisted
    assert "this/repo" in persisted


def test_prime_forwards_on_progress_to_prefetch():
    """The on_progress callback reaches gh.prefetch_default_branches."""
    gh = FakeGHClient(default_branches={"a/b": "main"})
    calls: list[tuple[int, int]] = []
    prime_default_branches(
        gh,
        ["a/b"],
        now=_now(),
        on_progress=lambda done, total: calls.append((done, total)),
    )
    # Fake fires on_progress(n, n) once for the 1 missing slug.
    assert calls == [(1, 1)]


def test_prime_defaults_now_to_wall_clock():
    """now=None path: uses datetime.now; just verify no crash + persist."""
    gh = FakeGHClient(default_branches={"a/b": "main"})
    prime_default_branches(gh, ["a/b"])  # no now=
    assert "a/b" in load_cache()


def test_default_ttl_is_seven_days():
    assert DEFAULT_TTL_DAYS == 7


# ─── archived status (piggybacks on the default-branch cache) ───────────────


def test_archived_round_trips_through_save_load():
    save_cache(
        {
            "a/archived": CachedBranch("main", _now(), archived=True),
            "a/live": CachedBranch("main", _now(), archived=False),
        }
    )
    loaded = load_cache()
    assert loaded["a/archived"].archived is True
    assert loaded["a/live"].archived is False


def test_load_defaults_archived_false_for_legacy_entry():
    """A pre-archived cache file (no `archived` key) loads as not-archived —
    backward compatible, and the safe direction (won't wrongly skip)."""
    paths.default_branch_cache_file().write_text(
        yaml.safe_dump(
            {
                "schema_version": SCHEMA_VERSION,
                "branches": {
                    "a/b": {"branch": "main", "fetched_at": _now().isoformat()},
                },
            }
        )
    )
    loaded = load_cache()
    assert loaded["a/b"].archived is False


def test_cachedbranch_archived_defaults_false():
    """The dataclass default keeps existing two-arg construction working."""
    assert CachedBranch("main", _now()).archived is False


def test_prime_seeds_archived_from_fresh_entries():
    """Warm entries seed gh's archived cache (no network), so the
    github.not_archived gate works on an all-warm run."""
    save_cache(
        {
            "warm/archived": CachedBranch(
                "main", _now() - timedelta(days=1), archived=True
            ),
        }
    )
    gh = FakeGHClient(default_branches={}, archived={})
    prime_default_branches(gh, ["warm/archived"], now=_now())
    assert gh.call_count["prefetch_default_branches"] == 0
    assert gh.is_archived("warm/archived") is True


def test_prime_persists_archived_from_fetch():
    """After a cold fetch, the resolved archived flag is written to the
    file cache (read back from gh.cached_archived())."""
    gh = FakeGHClient(
        default_branches={"a/b": "main"}, archived={"a/b": True}
    )
    prime_default_branches(gh, ["a/b"], now=_now())
    persisted = load_cache()
    assert persisted["a/b"].archived is True


def test_prime_persists_archived_false_when_not_archived():
    gh = FakeGHClient(
        default_branches={"a/b": "main"}, archived={"a/b": False}
    )
    prime_default_branches(gh, ["a/b"], now=_now())
    assert load_cache()["a/b"].archived is False
