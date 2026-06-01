"""End-to-end tests for ``gitbulk close-stale``.

Pipeline: invariants → stale candidates → comments fetched → decide →
(dry-run gate) → post_comment / close_pr → exit code. Every gh call
goes through :class:`FakeGHClient`; no network in tests.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from gitbulk import paths, sentinel
from gitbulk.cli import main
from gitbulk.commands.close_stale import (
    EXIT_ATTENTION_NEEDED,
    EXIT_INVARIANT_SKIPPED,
    EXIT_OK,
    EXIT_OVERRIDES_APPLIED,
    EXIT_STRUCTURAL_FAILURE,
    STALE_WARNING_MARKER,
    _decide_action,
    _find_latest_warning,
    close_stale_handler,
)
from gitbulk.gh import FakeGHClient, GHError
from gitbulk.invariants import catalog as _catalog
from gitbulk.org_members_cache import CachedMembers, save_cache
from gitbulk.pr_info import PRComment, PRInfo


# ─── Fixtures (mirror test_merge) ──────────────────────────────────────────


@pytest.fixture
def isolated_xdg(monkeypatch, tmp_path):
    cfg = tmp_path / "config"
    cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    paths.ensure_directories()
    return tmp_path


@pytest.fixture
def code_root(tmp_path):
    root = tmp_path / "code"
    root.mkdir()
    return root


@pytest.fixture
def write_config(isolated_xdg, code_root):
    """gitbulk.yaml + repos.txt. stale_age_days=30 by default to keep
    test dates manageable."""

    def _write(*, repos_slugs, defaults_extra=None, repo_overrides=None):
        cfg_dir = paths.config_dir()
        cfg_dir.mkdir(parents=True, exist_ok=True)
        defaults = {
            "retain_runs": 5,
            "stale_age_days": 30,
            "stale_cooloff_days": 7,
        }
        if defaults_extra:
            defaults.update(defaults_extra)
        policy_yaml: dict = {"defaults": defaults}
        policy_yaml["humans"] = {"org": "provenant-dev", "cache_ttl_hours": 24}
        if repo_overrides:
            policy_yaml["repos"] = repo_overrides
        (cfg_dir / "gitbulk.yaml").write_text(yaml.safe_dump(policy_yaml))
        repos_txt = "\n".join(repos_slugs) + ("\n" if repos_slugs else "")
        (cfg_dir / "repos.txt").write_text(repos_txt)
        for slug in repos_slugs:
            _, name = slug.split("/", 1)
            (code_root / name).mkdir(parents=True, exist_ok=True)
        return cfg_dir

    return _write


@pytest.fixture
def fresh_org_cache():
    def _save(org, members):
        save_cache(
            CachedMembers(
                org=org,
                fetched_at=datetime.now(timezone.utc),
                members=frozenset(members),
            )
        )

    return _save


def _make_pr(
    *,
    slug: str,
    number: int,
    updated_at: datetime,
    author: str = "dhh1128",
    base_ref: str = "main",
) -> PRInfo:
    return PRInfo(
        slug=slug,
        number=number,
        title=f"PR #{number}",
        url=f"https://github.com/{slug}/pull/{number}",
        author=author,
        base_ref=base_ref,
        head_ref=f"feature/{number}",
        head_sha="a" * 40,
        state="OPEN",
        is_draft=False,
        mergeable_state="CLEAN",
        created_at=updated_at - timedelta(days=10),
        updated_at=updated_at,
        last_pushed_at=updated_at,
        labels=(),
        review_decision=None,
        checks_status="SUCCESS",
    )


def _make_args(*, apply=False, code_root=None, skip_check=None, refresh_org_members=False,
               org=None, repo=None, base=None, mergeable_state=None, author=None,
               filter=None):
    return argparse.Namespace(
        subcommand="close-stale",
        apply=apply,
        code_root=str(code_root) if code_root else None,
        skip_check=list(skip_check) if skip_check else None,
        refresh_org_members=refresh_org_members,
        org=org,
        repo=repo,
        base=base,
        mergeable_state=mergeable_state,
        author=author,
        filter=filter,
    )


def _freeze_catalog_now(monkeypatch, when: datetime) -> None:
    monkeypatch.setattr(_catalog, "_utc_now", lambda: when)


def _freeze_handler_now(monkeypatch, when: datetime) -> None:
    from gitbulk.commands import close_stale as cs_mod
    monkeypatch.setattr(cs_mod, "_utc_now", lambda: when)


# ─── Pure decision-function tests ──────────────────────────────────────────


def _pr_with_updated(at: datetime) -> PRInfo:
    return _make_pr(slug="x/y", number=1, updated_at=at)


def test_decide_no_warning_returns_warn():
    now = datetime(2026, 5, 28, tzinfo=timezone.utc)
    pr = _pr_with_updated(now - timedelta(days=40))
    assert _decide_action(
        pr, None,
        stale_age_days=30,
        cooloff_days=7,
        stale_policy="warn-and-close",
        now=now,
    ) == "warn"


def test_decide_warning_with_later_activity_and_re_stale_returns_warn():
    """User came back, then went quiet again past stale_age_days → re-warn."""
    now = datetime(2026, 5, 28, tzinfo=timezone.utc)
    warning = PRComment(
        author_login="me",
        body=STALE_WARNING_MARKER,
        at=now - timedelta(days=60),
    )
    pr = _pr_with_updated(now - timedelta(days=35))  # came back, now re-stale
    assert _decide_action(
        pr, warning,
        stale_age_days=30,
        cooloff_days=7,
        stale_policy="warn-and-close",
        now=now,
    ) == "warn"


def test_decide_warning_with_recent_activity_returns_noop():
    """User came back recently — not yet stale_age_days untouched again."""
    now = datetime(2026, 5, 28, tzinfo=timezone.utc)
    warning = PRComment(
        author_login="me",
        body=STALE_WARNING_MARKER,
        at=now - timedelta(days=30),
    )
    pr = _pr_with_updated(now - timedelta(days=10))  # within stale_age_days
    assert _decide_action(
        pr, warning,
        stale_age_days=30,
        cooloff_days=7,
        stale_policy="warn-and-close",
        now=now,
    ) == "noop"


def test_decide_no_warning_within_stale_age_returns_noop():
    """Past pr.inactive's cooloff threshold but not past stale_age_days.

    Defensive: the cooloff threshold on pr.inactive admits PRs that are
    only 7-30 days inactive (under defaults). _decide_action must enforce
    stale_age_days so we don't warn prematurely.
    """
    now = datetime(2026, 5, 28, tzinfo=timezone.utc)
    pr = _pr_with_updated(now - timedelta(days=15))  # past cooloff, not stale_age
    assert _decide_action(
        pr, None,
        stale_age_days=30,
        cooloff_days=7,
        stale_policy="warn-and-close",
        now=now,
    ) == "noop"


def test_decide_warning_cooloff_elapsed_returns_close():
    now = datetime(2026, 5, 28, tzinfo=timezone.utc)
    warning_at = now - timedelta(days=10)
    warning = PRComment(author_login="me", body="x", at=warning_at)
    pr = _pr_with_updated(warning_at)  # no activity since warning
    assert _decide_action(
        pr, warning,
        stale_age_days=30,
        cooloff_days=7,
        stale_policy="warn-and-close",
        now=now,
    ) == "close"


def test_decide_warning_in_cooloff_returns_wait():
    now = datetime(2026, 5, 28, tzinfo=timezone.utc)
    warning_at = now - timedelta(days=3)
    warning = PRComment(author_login="me", body="x", at=warning_at)
    pr = _pr_with_updated(warning_at)
    assert _decide_action(
        pr, warning,
        stale_age_days=30,
        cooloff_days=7,
        stale_policy="warn-and-close",
        now=now,
    ) == "wait"


def test_decide_warn_only_never_closes():
    """stale_policy=warn-only converts a would-be close into a wait."""
    now = datetime(2026, 5, 28, tzinfo=timezone.utc)
    warning_at = now - timedelta(days=20)
    warning = PRComment(author_login="me", body="x", at=warning_at)
    pr = _pr_with_updated(warning_at)
    assert _decide_action(
        pr, warning,
        stale_age_days=30,
        cooloff_days=7,
        stale_policy="warn-only",
        now=now,
    ) == "wait"


# ─── _find_latest_warning helper ───────────────────────────────────────────


def test_find_latest_warning_empty_returns_none():
    assert _find_latest_warning([]) is None


def test_find_latest_warning_picks_most_recent():
    older = PRComment(
        author_login="me",
        body=f"first {STALE_WARNING_MARKER}",
        at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    newer = PRComment(
        author_login="me",
        body=f"second {STALE_WARNING_MARKER}",
        at=datetime(2026, 5, 20, tzinfo=timezone.utc),
    )
    other = PRComment(
        author_login="reviewer",
        body="LGTM",
        at=datetime(2026, 5, 25, tzinfo=timezone.utc),
    )
    result = _find_latest_warning([older, newer, other])
    assert result == newer


def test_find_latest_warning_ignores_comments_without_marker():
    no_marker = PRComment(
        author_login="me",
        body="this looks fine",
        at=datetime(2026, 5, 20, tzinfo=timezone.utc),
    )
    assert _find_latest_warning([no_marker]) is None


# ─── Dry-run path ──────────────────────────────────────────────────────────


def test_dry_run_stale_pr_no_prior_warning_would_warn(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
    pr = _make_pr(
        slug="dhh1128/alpha", number=1, updated_at=now - timedelta(days=40)
    )
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        pr_comments={},  # no prior warning
    )
    monkeypatch.setattr(
        "gitbulk.commands.close_stale.ProductionGHClient", lambda: fake
    )
    _freeze_catalog_now(monkeypatch, now)
    _freeze_handler_now(monkeypatch, now)

    rc = close_stale_handler(_make_args(code_root=code_root))
    assert rc == EXIT_ATTENTION_NEEDED  # dry-run that would act → ATTENTION
    assert fake.call_count["post_comment"] == 0
    assert fake.call_count["close_pr"] == 0
    summary = (paths.latest_run_symlink("close-stale").resolve() / "summary.md").read_text()
    assert "Would warn" in summary
    assert "dhh1128/alpha" in summary


def test_dry_run_no_stale_candidates_exit_ok(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """All PRs are fresh — nothing to do."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
    pr = _make_pr(
        slug="dhh1128/alpha", number=1, updated_at=now - timedelta(days=5)
    )
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        pr_comments={},
    )
    monkeypatch.setattr(
        "gitbulk.commands.close_stale.ProductionGHClient", lambda: fake
    )
    _freeze_catalog_now(monkeypatch, now)
    _freeze_handler_now(monkeypatch, now)

    rc = close_stale_handler(_make_args(code_root=code_root))
    assert rc == EXIT_OK
    assert not sentinel.has_attention()


def test_dry_run_org_filter_reports_filter_line(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """An org filter prunes the fleet and the summary records it."""
    write_config(repos_slugs=["provenant-dev/alpha", "dhh1128/beta"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    now = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
    pr = _make_pr(
        slug="provenant-dev/alpha", number=1, updated_at=now - timedelta(days=5)
    )
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={
            "provenant-dev/alpha": "main",
            "dhh1128/beta": "main",
        },
        my_open_prs={"provenant-dev/alpha": [pr]},
        pr_comments={},
    )
    monkeypatch.setattr(
        "gitbulk.commands.close_stale.ProductionGHClient", lambda: fake
    )
    _freeze_catalog_now(monkeypatch, now)
    _freeze_handler_now(monkeypatch, now)

    rc = close_stale_handler(_make_args(code_root=code_root, org=["provenant-dev"]))
    assert rc == EXIT_OK
    summary = (paths.latest_run_symlink("close-stale").resolve()
               / "summary.md").read_text()
    assert "Filtered" in summary
    assert "org=provenant-dev" in summary
    assert "dhh1128/beta" not in summary


# ─── --apply path: warn flow ───────────────────────────────────────────────


def test_apply_stale_pr_no_warning_posts_comment(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    now = datetime(2026, 5, 28, tzinfo=timezone.utc)
    pr = _make_pr(
        slug="dhh1128/alpha", number=1, updated_at=now - timedelta(days=40)
    )
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        pr_comments={},
        post_comment_responses={("dhh1128/alpha", 1): {"url": "...c"}},
    )
    monkeypatch.setattr(
        "gitbulk.commands.close_stale.ProductionGHClient", lambda: fake
    )
    _freeze_catalog_now(monkeypatch, now)
    _freeze_handler_now(monkeypatch, now)

    rc = close_stale_handler(_make_args(apply=True, code_root=code_root))
    assert rc == EXIT_ATTENTION_NEEDED  # warn issued → ATTENTION
    assert fake.call_count["post_comment"] == 1
    assert fake.call_count["close_pr"] == 0
    body = fake.post_comment_calls[0]["body"]
    assert STALE_WARNING_MARKER in body
    assert "30+ days" in body  # stale_age_days
    assert "7 days" in body    # stale_cooloff_days


# ─── --apply path: close flow ──────────────────────────────────────────────


def test_apply_warned_pr_with_elapsed_cooloff_closes(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    now = datetime(2026, 5, 28, tzinfo=timezone.utc)
    warning_at = now - timedelta(days=10)  # cooloff = 7 → elapsed
    # PR's updated_at equals warning_at — no activity since warning.
    pr = _make_pr(slug="dhh1128/alpha", number=1, updated_at=warning_at)
    warning = PRComment(
        author_login="dhh1128",
        body=f"heads up {STALE_WARNING_MARKER}",
        at=warning_at,
    )
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        pr_comments={("dhh1128/alpha", 1): [warning]},
        close_responses={("dhh1128/alpha", 1): {}},
    )
    monkeypatch.setattr(
        "gitbulk.commands.close_stale.ProductionGHClient", lambda: fake
    )
    _freeze_catalog_now(monkeypatch, now)
    _freeze_handler_now(monkeypatch, now)

    rc = close_stale_handler(_make_args(apply=True, code_root=code_root))
    assert rc == EXIT_OK  # close completed, no warn issued
    assert fake.call_count["close_pr"] == 1
    assert fake.call_count["post_comment"] == 0
    # Default: do NOT delete the branch on stale-close
    assert fake.close_calls[0]["delete_branch"] is False


# ─── --apply path: user came back ──────────────────────────────────────────


def test_apply_warned_pr_with_later_activity_re_warns(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """User commented after the warning → not closed, instead re-warn
    (because PR is still stale by stale_age_days from current updated_at)."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    now = datetime(2026, 5, 28, tzinfo=timezone.utc)
    warning_at = now - timedelta(days=50)
    # updated_at is AFTER warning — but still 35 days ago, so still stale
    updated_at = now - timedelta(days=35)
    pr = _make_pr(slug="dhh1128/alpha", number=1, updated_at=updated_at)
    warning = PRComment(
        author_login="dhh1128",
        body=f"heads up {STALE_WARNING_MARKER}",
        at=warning_at,
    )
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        pr_comments={("dhh1128/alpha", 1): [warning]},
        post_comment_responses={("dhh1128/alpha", 1): {}},
    )
    monkeypatch.setattr(
        "gitbulk.commands.close_stale.ProductionGHClient", lambda: fake
    )
    _freeze_catalog_now(monkeypatch, now)
    _freeze_handler_now(monkeypatch, now)

    rc = close_stale_handler(_make_args(apply=True, code_root=code_root))
    assert rc == EXIT_ATTENTION_NEEDED  # re-warn
    assert fake.call_count["close_pr"] == 0
    assert fake.call_count["post_comment"] == 1


# ─── --apply path: warn-only policy ────────────────────────────────────────


def test_apply_warn_only_never_closes(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(
        repos_slugs=["dhh1128/alpha"],
        defaults_extra={"stale_policy": "warn-only"},
    )
    fresh_org_cache("provenant-dev", ["dhh1128"])
    now = datetime(2026, 5, 28, tzinfo=timezone.utc)
    warning_at = now - timedelta(days=20)  # cooloff elapsed
    pr = _make_pr(slug="dhh1128/alpha", number=1, updated_at=warning_at)
    warning = PRComment(
        author_login="dhh1128",
        body=f"prior warning {STALE_WARNING_MARKER}",
        at=warning_at,
    )
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        pr_comments={("dhh1128/alpha", 1): [warning]},
    )
    monkeypatch.setattr(
        "gitbulk.commands.close_stale.ProductionGHClient", lambda: fake
    )
    _freeze_catalog_now(monkeypatch, now)
    _freeze_handler_now(monkeypatch, now)

    rc = close_stale_handler(_make_args(apply=True, code_root=code_root))
    # Decision was 'wait' (warn-only suppresses the close). No mutations.
    assert rc == EXIT_OK
    assert fake.call_count["close_pr"] == 0
    assert fake.call_count["post_comment"] == 0


# ─── --apply path: stale_policy=never opts out at invariant ────────────────


def test_apply_stale_policy_never_skips_invariant(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(
        repos_slugs=["dhh1128/alpha"],
        defaults_extra={"stale_policy": "never"},
    )
    fresh_org_cache("provenant-dev", ["dhh1128"])
    now = datetime(2026, 5, 28, tzinfo=timezone.utc)
    pr = _make_pr(
        slug="dhh1128/alpha", number=1, updated_at=now - timedelta(days=100)
    )
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        pr_comments={},
    )
    monkeypatch.setattr(
        "gitbulk.commands.close_stale.ProductionGHClient", lambda: fake
    )
    _freeze_catalog_now(monkeypatch, now)
    _freeze_handler_now(monkeypatch, now)

    rc = close_stale_handler(_make_args(apply=True, code_root=code_root))
    # PR is filtered out at pr.inactive (intrinsic skip), but the only
    # invariant that skips is per-PR not per-repo — so the run still
    # exits OK with nothing to do.
    assert rc == EXIT_OK
    assert fake.call_count["post_comment"] == 0
    assert fake.call_count["close_pr"] == 0


# ─── --apply path: gh failure handling ─────────────────────────────────────


def test_apply_post_comment_failure_records_error(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    now = datetime(2026, 5, 28, tzinfo=timezone.utc)
    pr = _make_pr(
        slug="dhh1128/alpha", number=1, updated_at=now - timedelta(days=40)
    )
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        pr_comments={},
        post_comment_responses={("dhh1128/alpha", 1): GHError("rate limited")},
    )
    monkeypatch.setattr(
        "gitbulk.commands.close_stale.ProductionGHClient", lambda: fake
    )
    _freeze_catalog_now(monkeypatch, now)
    _freeze_handler_now(monkeypatch, now)

    rc = close_stale_handler(_make_args(apply=True, code_root=code_root))
    assert rc == EXIT_ATTENTION_NEEDED  # failure → ATTENTION


def test_apply_close_pr_failure_records_error(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    now = datetime(2026, 5, 28, tzinfo=timezone.utc)
    warning_at = now - timedelta(days=10)
    pr = _make_pr(slug="dhh1128/alpha", number=1, updated_at=warning_at)
    warning = PRComment(
        author_login="dhh1128",
        body=f"heads up {STALE_WARNING_MARKER}",
        at=warning_at,
    )
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        pr_comments={("dhh1128/alpha", 1): [warning]},
        close_responses={("dhh1128/alpha", 1): GHError("nope")},
    )
    monkeypatch.setattr(
        "gitbulk.commands.close_stale.ProductionGHClient", lambda: fake
    )
    _freeze_catalog_now(monkeypatch, now)
    _freeze_handler_now(monkeypatch, now)

    rc = close_stale_handler(_make_args(apply=True, code_root=code_root))
    assert rc == EXIT_ATTENTION_NEEDED


def test_apply_fetch_comments_failure_records_error(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """fetch_pr_comments failing is fatal-for-that-PR but not the run."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    now = datetime(2026, 5, 28, tzinfo=timezone.utc)
    pr = _make_pr(
        slug="dhh1128/alpha", number=1, updated_at=now - timedelta(days=40)
    )
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        # No pr_comments configured → raises on fetch
    )
    monkeypatch.setattr(
        "gitbulk.commands.close_stale.ProductionGHClient", lambda: fake
    )
    _freeze_catalog_now(monkeypatch, now)
    _freeze_handler_now(monkeypatch, now)

    rc = close_stale_handler(_make_args(apply=True, code_root=code_root))
    assert rc == EXIT_ATTENTION_NEEDED


# ─── CLI smoke ─────────────────────────────────────────────────────────────


# ─── Coverage fillers for handler edge paths ────────────────────────────


def test_lock_timeout_returns_structural_failure(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])

    def _raise_timeout(*a, **k):
        from gitbulk.locks import LockTimeoutError
        raise LockTimeoutError(Path("/tmp/fake.lock"), None)

    monkeypatch.setattr(
        "gitbulk.commands.close_stale.global_lock", _raise_timeout
    )
    rc = close_stale_handler(_make_args(code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE


def test_universal_preflight_failure_returns_structural_failure(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """A universal invariant Fail → structural failure. Triggered via
    gh.authenticated (no user); a fresh cache keeps auto-refresh a no-op
    so we actually reach the preflight."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient()  # no user → authenticated_user raises
    monkeypatch.setattr(
        "gitbulk.commands.close_stale.ProductionGHClient", lambda: fake
    )
    rc = close_stale_handler(_make_args(code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE


def test_close_stale_auto_refreshes_missing_cache(
    monkeypatch, isolated_xdg, code_root, write_config,
):
    """A missing org-members cache auto-refreshes (ormrf7kq) and the run
    proceeds rather than hard-failing the preflight."""
    write_config(repos_slugs=["dhh1128/alpha"])  # no fresh_org_cache
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.close_stale.ProductionGHClient", lambda: fake
    )
    rc = close_stale_handler(_make_args(code_root=code_root))
    assert rc == EXIT_OK
    assert fake.call_count["org_members"] == 1
    assert paths.org_members_cache_file("provenant-dev").exists()


def test_close_stale_auto_refresh_failure_exits_structural(
    monkeypatch, isolated_xdg, code_root, write_config,
):
    """A failed automatic refresh (GitHub unreachable) aborts with exit 1
    and records the failure."""
    import json

    write_config(repos_slugs=["dhh1128/alpha"])  # cache missing
    fake = FakeGHClient(user={"login": "dhh1128"})  # no org_members → raises
    monkeypatch.setattr(
        "gitbulk.commands.close_stale.ProductionGHClient", lambda: fake
    )
    rc = close_stale_handler(_make_args(code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE
    latest = paths.latest_run_symlink("close-stale").resolve()
    events = [
        json.loads(line)
        for line in (latest / "errors.log").read_text().splitlines()
        if line.strip()
    ]
    assert any(
        "org-members auto-refresh failed" in e.get("message", "") for e in events
    )


def test_my_open_prs_failure_returns_structural_failure(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        # No my_open_prs configured → raises
    )
    monkeypatch.setattr(
        "gitbulk.commands.close_stale.ProductionGHClient", lambda: fake
    )
    rc = close_stale_handler(_make_args(code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE


def test_skip_check_flag_records_warning_and_changes_exit(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """--skip-check pr.inactive lets every PR through (no candidates,
    since the skip means we don't even check inactivity). Exit code is
    EXIT_OVERRIDES_APPLIED (4) per the merge convention."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    now = datetime(2026, 5, 28, tzinfo=timezone.utc)
    pr = _make_pr(
        slug="dhh1128/alpha", number=1, updated_at=now - timedelta(days=1)
    )
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        pr_comments={},
        post_comment_responses={("dhh1128/alpha", 1): {}},
    )
    monkeypatch.setattr(
        "gitbulk.commands.close_stale.ProductionGHClient", lambda: fake
    )
    _freeze_catalog_now(monkeypatch, now)
    _freeze_handler_now(monkeypatch, now)
    rc = close_stale_handler(
        _make_args(code_root=code_root, skip_check=["pr.inactive"])
    )
    # Dry-run: skip_list took effect, no eligible PRs would warn/close.
    assert rc == EXIT_OVERRIDES_APPLIED


def test_per_repo_invariant_skip_lists_repo_as_skipped(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """A repo whose per-repo invariant intrinsic-skips (local clone
    missing, etc.) appears in skipped_repos. close-stale has no per-repo
    skip-y invariants in its chain — but if a future one is added, the
    plumbing must surface the skip. Force the path by making one slug
    point at a directory but using a default_branches map that omits it,
    causing github.reachable to Skip per its GHError-Skip convention."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        # default_branches missing dhh1128/alpha → github.reachable Skips
        default_branches={},
    )
    monkeypatch.setattr(
        "gitbulk.commands.close_stale.ProductionGHClient", lambda: fake
    )
    rc = close_stale_handler(_make_args(code_root=code_root))
    assert rc == EXIT_INVARIANT_SKIPPED
    summary = (paths.latest_run_symlink("close-stale").resolve() / "summary.md").read_text()
    assert "Skipped repos" in summary


def test_apply_warn_only_after_warning_in_cooloff_returns_ok(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """warn-only repo + existing warning + cooloff NOT elapsed → wait
    decision; --apply makes no calls and exits OK."""
    write_config(
        repos_slugs=["dhh1128/alpha"],
        defaults_extra={"stale_policy": "warn-only"},
    )
    fresh_org_cache("provenant-dev", ["dhh1128"])
    now = datetime(2026, 5, 28, tzinfo=timezone.utc)
    warning_at = now - timedelta(days=2)  # well within cooloff
    pr = _make_pr(slug="dhh1128/alpha", number=1, updated_at=warning_at)
    warning = PRComment(
        author_login="dhh1128",
        body=f"prior {STALE_WARNING_MARKER}",
        at=warning_at,
    )
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        pr_comments={("dhh1128/alpha", 1): [warning]},
    )
    monkeypatch.setattr(
        "gitbulk.commands.close_stale.ProductionGHClient", lambda: fake
    )
    _freeze_catalog_now(monkeypatch, now)
    _freeze_handler_now(monkeypatch, now)
    rc = close_stale_handler(_make_args(apply=True, code_root=code_root))
    assert rc == EXIT_OK


def test_per_repo_fail_aborts(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """A per-repo invariant Fail (not Skip) returns EXIT_STRUCTURAL_FAILURE."""
    from gitbulk.invariants.base import Fail as _Fail

    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.close_stale.ProductionGHClient", lambda: fake
    )
    monkeypatch.setattr(
        _catalog.GithubReachableInvariant,
        "check",
        lambda self, ctx: _Fail("forced fail"),
    )
    rc = close_stale_handler(_make_args(code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE


def test_apply_with_skipped_repo_no_failures_exit_skipped(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """--apply path + skipped_repos + no failures → EXIT_INVARIANT_SKIPPED."""
    write_config(repos_slugs=["dhh1128/alpha", "dhh1128/beta"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    now = datetime(2026, 5, 28, tzinfo=timezone.utc)
    # alpha has a stale PR that would be warned; beta is unreachable.
    pr = _make_pr(slug="dhh1128/alpha", number=1, updated_at=now - timedelta(days=2))
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        # default_branches missing beta → github.reachable Skips it
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        pr_comments={},
    )
    monkeypatch.setattr(
        "gitbulk.commands.close_stale.ProductionGHClient", lambda: fake
    )
    _freeze_catalog_now(monkeypatch, now)
    _freeze_handler_now(monkeypatch, now)
    rc = close_stale_handler(_make_args(apply=True, code_root=code_root))
    # alpha's PR is past cooloff (2 days > stale_cooloff_days=7? no, 2 < 7)
    # — actually no, 2 < 7 so pr.inactive SKIPS that PR too. Result is
    # no actions, but beta is skipped, so EXIT_INVARIANT_SKIPPED applies.
    assert rc == EXIT_INVARIANT_SKIPPED


def test_apply_with_skip_check_no_warns_exit_overrides(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """--apply + --skip-check + no failures + no warn_count → EXIT_OVERRIDES_APPLIED.

    PR is so fresh that even with pr.inactive skipped, _decide_action
    returns 'noop' (not stale by stale_age_days). So warn_count stays 0
    and the skip-list exit code wins.
    """
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    now = datetime(2026, 5, 28, tzinfo=timezone.utc)
    pr = _make_pr(
        slug="dhh1128/alpha", number=1, updated_at=now - timedelta(days=1)
    )
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        pr_comments={},
    )
    monkeypatch.setattr(
        "gitbulk.commands.close_stale.ProductionGHClient", lambda: fake
    )
    _freeze_catalog_now(monkeypatch, now)
    _freeze_handler_now(monkeypatch, now)
    rc = close_stale_handler(
        _make_args(
            apply=True,
            code_root=code_root,
            skip_check=["pr.inactive"],
        )
    )
    assert rc == EXIT_OVERRIDES_APPLIED


def test_runid_from_run_dir_handles_non_close_stale_suffix():
    """Defensive: if someone passes a non-close-stale run dir, fall back
    to the trailing-dash heuristic."""
    from gitbulk.commands.close_stale import _runid_from_run_dir
    assert _runid_from_run_dir(Path("/tmp/20260528T120000Z-merge")) == "20260528T120000Z"


def test_state_for_repo_excludes_other_slugs():
    """state_for_repo only includes actions matching the slug."""
    from gitbulk.commands.close_stale import _state_for_repo
    actions = [
        {"slug": "a/b", "number": 1, "title": "x", "url": "u", "decision": "warn"},
        {"slug": "c/d", "number": 9, "title": "y", "url": "u", "decision": "warn"},
    ]
    result = _state_for_repo("a/b", actions, {})
    assert result["pr_count"] == 1
    assert result["prs"][0]["number"] == 1


def test_main_close_stale_default_is_dry_run(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """``gitbulk close-stale`` (no --apply) must NOT mutate."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    now = datetime(2026, 5, 28, tzinfo=timezone.utc)
    pr = _make_pr(
        slug="dhh1128/alpha", number=1, updated_at=now - timedelta(days=40)
    )
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
        pr_comments={},
    )
    monkeypatch.setattr(
        "gitbulk.commands.close_stale.ProductionGHClient", lambda: fake
    )
    _freeze_catalog_now(monkeypatch, now)
    _freeze_handler_now(monkeypatch, now)

    rc = main(["close-stale", "--code-root", str(code_root)])
    assert rc == EXIT_ATTENTION_NEEDED  # would act → ATTENTION
    assert fake.call_count["post_comment"] == 0
    assert fake.call_count["close_pr"] == 0


# ─── Skipped repos.txt entries surfaced in close-stale ────────────────────


def test_close_stale_skipped_entries_surface_in_summary(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """A bad entry in repos.txt appears in close-stale summary.md."""
    cfg_dir = paths.config_dir()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "gitbulk.yaml").write_text(yaml.safe_dump({
        "defaults": {"retain_runs": 5, "stale_age_days": 30, "stale_cooloff_days": 7},
        "humans": {"org": "provenant-dev", "cache_ttl_hours": 24},
    }))
    (cfg_dir / "repos.txt").write_text(
        "dhh1128/alpha\n"
        "/nonexistent/bad-entry\n"
    )
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
        pr_comments={},
    )
    monkeypatch.setattr(
        "gitbulk.commands.close_stale.ProductionGHClient", lambda: fake
    )
    rc = close_stale_handler(_make_args(code_root=code_root))
    assert rc == EXIT_INVARIANT_SKIPPED
    summary = (paths.latest_run_symlink("close-stale").resolve() / "summary.md").read_text()
    assert "Skipped repos.txt entries" in summary
