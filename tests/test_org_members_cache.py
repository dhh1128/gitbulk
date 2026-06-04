"""Tests for the org-members cache (this.i nodes ``hbcls4pq`` + ``schv4nrm``).

Cache file: ``~/.cache/gitbulk/org-members/<org>.yaml``. Tests redirect
the cache root to a tmp dir via the ``isolated_cache`` fixture so
nothing touches the user's real ``~/.cache``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gitbulk import org_members_cache as omc
from gitbulk import paths
from gitbulk.gh import FakeGHClient, GHError
from gitbulk.config.policy import HumansConfig, Policy
from gitbulk.org_members_cache import (
    SCHEMA_VERSION,
    CachedMembers,
    OrgMembersRefreshError,
    ensure_org_members_fresh,
    is_fresh,
    load_cache,
    refresh_cache,
    save_cache,
)


# ─── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_cache(monkeypatch, tmp_path):
    """Redirect XDG_CACHE_HOME (and XDG_CONFIG_HOME for symmetry) to tmp."""
    cache_root = tmp_path / "cache-root"
    config_root = tmp_path / "config-root"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    paths.ensure_directories()
    return cache_root


# ─── save → load roundtrip ──────────────────────────────────────────────────


def test_save_then_load_roundtrip(isolated_cache):
    fetched = datetime(2026, 5, 28, 6, 41, 0, tzinfo=timezone.utc)
    cached = CachedMembers(
        org="provenant-dev",
        fetched_at=fetched,
        members=frozenset({"dhh1128", "alice"}),
    )
    save_cache(cached)
    loaded = load_cache("provenant-dev")
    assert loaded is not None
    assert loaded.org == "provenant-dev"
    assert loaded.fetched_at == fetched
    assert loaded.members == frozenset({"dhh1128", "alice"})


def test_save_normalizes_fetched_at_to_utc(isolated_cache):
    """A non-UTC tz-aware fetched_at is stored as UTC."""
    tz = timezone(timedelta(hours=-7))
    local = datetime(2026, 5, 27, 23, 41, 0, tzinfo=tz)  # == 06:41 UTC
    cached = CachedMembers(
        org="provenant-dev",
        fetched_at=local,
        members=frozenset({"alice"}),
    )
    save_cache(cached)
    loaded = load_cache("provenant-dev")
    assert loaded is not None
    assert loaded.fetched_at == datetime(2026, 5, 28, 6, 41, 0, tzinfo=timezone.utc)


def test_save_creates_parent_dir_if_missing(isolated_cache):
    """save_cache is robust when the org-members dir somehow doesn't exist."""
    # Remove the dir created by ensure_directories.
    import shutil
    shutil.rmtree(paths.org_members_cache_dir())
    cached = CachedMembers(
        org="provenant-dev",
        fetched_at=datetime.now(timezone.utc),
        members=frozenset({"alice"}),
    )
    save_cache(cached)
    assert paths.org_members_cache_file("provenant-dev").exists()


def test_save_atomic_no_tmp_leftover(isolated_cache):
    cached = CachedMembers(
        org="provenant-dev",
        fetched_at=datetime.now(timezone.utc),
        members=frozenset({"alice"}),
    )
    save_cache(cached)
    org_dir = paths.org_members_cache_dir()
    leftovers = list(org_dir.glob("*.tmp"))
    assert leftovers == [], f"unexpected .tmp leftovers: {leftovers}"


def test_save_empty_members(isolated_cache):
    """A roundtrip with zero members must work (empty org or all excepted)."""
    fetched = datetime(2026, 5, 28, tzinfo=timezone.utc)
    cached = CachedMembers(
        org="empty-org", fetched_at=fetched, members=frozenset()
    )
    save_cache(cached)
    loaded = load_cache("empty-org")
    assert loaded is not None
    assert loaded.members == frozenset()


# ─── load: every "treat as None" path ──────────────────────────────────────


def test_load_missing_file_returns_none(isolated_cache):
    assert load_cache("nonexistent-org") is None


def test_load_malformed_yaml_returns_none(isolated_cache):
    path = paths.org_members_cache_file("broken")
    path.write_text("schema_version: 1\nfetched_at: [unterminated\n")
    assert load_cache("broken") is None


def test_load_yaml_not_a_mapping_returns_none(isolated_cache):
    """Top-level YAML that parses but isn't a mapping is rejected."""
    path = paths.org_members_cache_file("listy")
    path.write_text("- just\n- a\n- list\n")
    assert load_cache("listy") is None


def test_load_yaml_empty_file_returns_none(isolated_cache):
    """An empty file parses to None, which is not a mapping."""
    path = paths.org_members_cache_file("empty")
    path.write_text("")
    assert load_cache("empty") is None


def test_load_wrong_schema_version_returns_none(isolated_cache):
    path = paths.org_members_cache_file("future")
    path.write_text(
        "schema_version: 99\n"
        "fetched_at: 2026-05-28T06:41:00+00:00\n"
        "members:\n  - alice\n"
    )
    assert load_cache("future") is None


def test_load_missing_schema_version_returns_none(isolated_cache):
    path = paths.org_members_cache_file("noschema")
    path.write_text(
        "fetched_at: 2026-05-28T06:41:00+00:00\nmembers:\n  - alice\n"
    )
    assert load_cache("noschema") is None


def test_load_missing_fetched_at_returns_none(isolated_cache):
    path = paths.org_members_cache_file("nofa")
    path.write_text("schema_version: 1\nmembers:\n  - alice\n")
    assert load_cache("nofa") is None


def test_load_missing_members_returns_none(isolated_cache):
    path = paths.org_members_cache_file("nomembers")
    path.write_text(
        "schema_version: 1\nfetched_at: 2026-05-28T06:41:00+00:00\n"
    )
    assert load_cache("nomembers") is None


def test_load_malformed_fetched_at_returns_none(isolated_cache):
    path = paths.org_members_cache_file("badts")
    path.write_text(
        "schema_version: 1\n"
        "fetched_at: 'not a real timestamp'\n"
        "members:\n  - alice\n"
    )
    assert load_cache("badts") is None


def test_load_non_string_non_datetime_fetched_at_returns_none(isolated_cache):
    """An int where a timestamp belongs is rejected."""
    path = paths.org_members_cache_file("intts")
    path.write_text(
        "schema_version: 1\nfetched_at: 12345\nmembers:\n  - alice\n"
    )
    assert load_cache("intts") is None


def test_load_naive_fetched_at_returns_none(isolated_cache):
    """A naive (no-tz) datetime is rejected as ambiguous."""
    path = paths.org_members_cache_file("naivets")
    # YAML 'YYYY-MM-DD HH:MM:SS' without a tz parses to a naive datetime.
    path.write_text(
        "schema_version: 1\n"
        "fetched_at: 2026-05-28 06:41:00\n"
        "members:\n  - alice\n"
    )
    assert load_cache("naivets") is None


def test_load_members_not_a_list_returns_none(isolated_cache):
    path = paths.org_members_cache_file("mapmembers")
    path.write_text(
        "schema_version: 1\n"
        "fetched_at: 2026-05-28T06:41:00+00:00\n"
        "members:\n  alice: true\n"
    )
    assert load_cache("mapmembers") is None


def test_load_member_not_a_string_returns_none(isolated_cache):
    path = paths.org_members_cache_file("intmember")
    path.write_text(
        "schema_version: 1\n"
        "fetched_at: 2026-05-28T06:41:00+00:00\n"
        "members:\n  - 123\n"
    )
    assert load_cache("intmember") is None


def test_load_accepts_native_yaml_datetime(isolated_cache):
    """PyYAML decodes ISO-8601 timestamps to datetime; we accept that."""
    path = paths.org_members_cache_file("native")
    path.write_text(
        "schema_version: 1\n"
        "fetched_at: 2026-05-28T06:41:00+00:00\n"
        "members:\n  - alice\n"
    )
    loaded = load_cache("native")
    assert loaded is not None
    assert loaded.fetched_at == datetime(2026, 5, 28, 6, 41, 0, tzinfo=timezone.utc)


# ─── is_fresh ──────────────────────────────────────────────────────────────


def _cm(fetched_at: datetime) -> CachedMembers:
    return CachedMembers(
        org="org", fetched_at=fetched_at, members=frozenset({"alice"})
    )


def test_is_fresh_within_ttl():
    now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
    cached = _cm(now - timedelta(hours=1))
    assert is_fresh(cached, ttl_hours=24, now=now) is True


def test_is_fresh_just_fetched():
    now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
    cached = _cm(now)
    assert is_fresh(cached, ttl_hours=24, now=now) is True


def test_is_fresh_older_than_ttl():
    now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
    cached = _cm(now - timedelta(hours=48))
    assert is_fresh(cached, ttl_hours=24, now=now) is False


def test_is_fresh_exactly_at_boundary_is_stale():
    """Strict < — equality is treated as stale."""
    now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
    cached = _cm(now - timedelta(hours=24))
    assert is_fresh(cached, ttl_hours=24, now=now) is False


def test_is_fresh_uses_wall_clock_when_now_is_none():
    """With no explicit ``now``, compare against the current UTC wall clock."""
    # Use a recently-fetched cache so the answer is True regardless of wall clock.
    cached = _cm(datetime.now(timezone.utc) - timedelta(seconds=1))
    assert is_fresh(cached, ttl_hours=1) is True


def test_is_fresh_rejects_naive_now():
    cached = _cm(datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="tzinfo"):
        is_fresh(cached, ttl_hours=24, now=datetime(2026, 5, 28, 12, 0, 0))


# ─── refresh_cache ─────────────────────────────────────────────────────────


def test_refresh_cache_fetches_saves_and_returns(isolated_cache):
    gh = FakeGHClient(org_members={"provenant-dev": ["dhh1128", "alice"]})
    cached = refresh_cache(gh, "provenant-dev")
    # Returned value matches the saved file.
    assert cached.org == "provenant-dev"
    assert cached.members == frozenset({"dhh1128", "alice"})
    assert cached.fetched_at.tzinfo is not None
    assert gh.call_count["org_members"] == 1
    # File on disk matches.
    loaded = load_cache("provenant-dev")
    assert loaded is not None
    assert loaded.members == cached.members
    # fetched_at survives the YAML roundtrip to second precision.
    assert loaded.fetched_at.replace(microsecond=0) == cached.fetched_at.replace(
        microsecond=0
    )


def test_refresh_cache_members_is_frozenset(isolated_cache):
    """The result's members field must be a frozenset (not a list)."""
    gh = FakeGHClient(org_members={"provenant-dev": ["dhh1128", "alice"]})
    cached = refresh_cache(gh, "provenant-dev")
    assert isinstance(cached.members, frozenset)


def test_refresh_cache_propagates_gh_error(isolated_cache):
    """If the GHClient raises, refresh_cache lets the error bubble up
    (no swallowing; caller decides whether to abort or fall back)."""
    gh = FakeGHClient()  # nothing configured
    with pytest.raises(GHError):
        refresh_cache(gh, "provenant-dev")


def test_refresh_cache_empty_org(isolated_cache):
    """An org that returns zero members is a valid result, not an error."""
    gh = FakeGHClient(org_members={"empty-org": []})
    cached = refresh_cache(gh, "empty-org")
    assert cached.members == frozenset()
    loaded = load_cache("empty-org")
    assert loaded is not None
    assert loaded.members == frozenset()


# ─── ensure_org_members_fresh (ormrf7kq) ───────────────────────────────────


def _policy(org="provenant-dev", ttl=24) -> Policy:
    return Policy(humans=HumansConfig(org=org, cache_ttl_hours=ttl))


def test_ensure_noop_when_org_unset(isolated_cache):
    """No configured org ⇒ no lookup, no refresh, returns None."""
    gh = FakeGHClient()  # no org_members configured; must not be called
    result = ensure_org_members_fresh(gh, _policy(org=None))
    assert result is None
    assert gh.call_count["org_members"] == 0


def test_ensure_noop_when_cache_fresh(isolated_cache):
    """A fresh on-disk cache is left untouched — no network call."""
    save_cache(
        CachedMembers(
            org="provenant-dev",
            fetched_at=datetime.now(timezone.utc),
            members=frozenset({"dhh1128"}),
        )
    )
    gh = FakeGHClient(org_members={"provenant-dev": ["dhh1128", "alice"]})
    result = ensure_org_members_fresh(gh, _policy())
    assert result is None
    assert gh.call_count["org_members"] == 0


def test_ensure_refreshes_when_missing(isolated_cache):
    gh = FakeGHClient(org_members={"provenant-dev": ["dhh1128", "alice"]})
    result = ensure_org_members_fresh(gh, _policy())
    assert result is not None
    assert result.members == frozenset({"dhh1128", "alice"})
    assert gh.call_count["org_members"] == 1
    assert load_cache("provenant-dev") is not None


def test_ensure_refreshes_when_stale(isolated_cache):
    save_cache(
        CachedMembers(
            org="provenant-dev",
            fetched_at=datetime.now(timezone.utc) - timedelta(hours=48),
            members=frozenset({"dhh1128"}),
        )
    )
    gh = FakeGHClient(org_members={"provenant-dev": ["dhh1128", "alice"]})
    result = ensure_org_members_fresh(gh, _policy(ttl=24))
    assert result is not None
    assert result.members == frozenset({"dhh1128", "alice"})
    assert gh.call_count["org_members"] == 1


def test_ensure_force_refreshes_even_when_fresh(isolated_cache):
    save_cache(
        CachedMembers(
            org="provenant-dev",
            fetched_at=datetime.now(timezone.utc),
            members=frozenset({"dhh1128"}),
        )
    )
    gh = FakeGHClient(org_members={"provenant-dev": ["dhh1128", "alice"]})
    result = ensure_org_members_fresh(gh, _policy(), force=True)
    assert result is not None
    assert gh.call_count["org_members"] == 1


def test_ensure_auto_refresh_failure_raises_wrapped(isolated_cache):
    """A failed automatic refresh raises OrgMembersRefreshError naming the
    automatic trigger, chained from the underlying GHError."""
    gh = FakeGHClient()  # org_members not configured → GHError
    with pytest.raises(OrgMembersRefreshError, match="org-members auto-refresh failed"):
        ensure_org_members_fresh(gh, _policy())
    # Chains from the GHError so the root cause survives.
    try:
        ensure_org_members_fresh(gh, _policy())
    except OrgMembersRefreshError as e:
        assert isinstance(e.__cause__, GHError)


def test_ensure_forced_refresh_failure_names_flag(isolated_cache):
    """A failed forced refresh names the --refresh-org-members trigger."""
    gh = FakeGHClient()
    with pytest.raises(OrgMembersRefreshError, match="--refresh-org-members failed"):
        ensure_org_members_fresh(gh, _policy(), force=True)


# ─── SCHEMA_VERSION is stable ──────────────────────────────────────────────


def test_schema_version_is_one():
    """Phase 2 initial schema. Bumping this is a coordinated migration."""
    assert SCHEMA_VERSION == 1
    assert omc.SCHEMA_VERSION == 1


# ─── org_lock wiring: double-checked refresh (node rsclk7nq / shawk7nq) ──────


def test_ensure_warm_path_does_not_acquire_org_lock(isolated_cache, monkeypatch):
    """A fresh cache returns on the fast path WITHOUT taking org_lock."""
    import contextlib

    save_cache(_cm(datetime.now(timezone.utc)))  # fresh under default 24h ttl
    entered: list[bool] = []

    @contextlib.contextmanager
    def spy_lock(*a, **k):
        entered.append(True)
        yield

    monkeypatch.setattr(omc, "org_lock", spy_lock)
    gh = FakeGHClient(org_members={"org": ["alice"]})

    assert ensure_org_members_fresh(gh, _policy(org="org")) is None
    assert entered == []  # warm path never locked
    assert gh.call_count["org_members"] == 0  # and never refetched


def test_ensure_double_check_skips_refresh_when_concurrently_refreshed(
    isolated_cache, monkeypatch
):
    """If another run refreshes between the outer freshness check and the
    locked re-check, we must NOT refetch (the double-checked inner return)."""
    stale = _cm(datetime.now(timezone.utc) - timedelta(hours=48))
    fresh = _cm(datetime.now(timezone.utc))
    seq = iter([stale, fresh])  # outer load = stale, inner (under lock) = fresh
    monkeypatch.setattr(omc, "load_cache", lambda org: next(seq))
    gh = FakeGHClient(org_members={"org": ["alice"]})

    assert ensure_org_members_fresh(gh, _policy(org="org")) is None
    assert gh.call_count["org_members"] == 0  # double-check avoided the refetch
