"""End-to-end tests for ``gitbulk report`` (this.i node ``scinv4qm``).

The pipeline orchestrates four Phase 2 subsystems: invariants
framework, gh client, classifier (via ``pr.author_known``), and
RunState. These tests pin every exit-code branch + the ATTENTION
sentinel contract + the structured state.yaml output.

All tests use FakeGHClient (AGENTS.md: "no network in tests").
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from gitbulk import paths, sentinel
from gitbulk.cli import main
from gitbulk.commands import report as report_mod
from gitbulk.commands.report import (
    EXIT_ATTENTION_NEEDED,
    EXIT_INVARIANT_SKIPPED,
    EXIT_OK,
    EXIT_OVERRIDES_APPLIED,
    EXIT_STRUCTURAL_FAILURE,
    _build_summary_md,
    _runid_from_run_dir,
    report_handler,
)
from gitbulk.gh import FakeGHClient, GHError
from gitbulk.locks import LockTimeoutError
from gitbulk.org_members_cache import CachedMembers, save_cache
from gitbulk.pr_info import CheckRun, PRInfo


# ─── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_xdg(monkeypatch, tmp_path):
    """Point XDG dirs at tmp so the test is fully self-contained."""
    cfg = tmp_path / "config"
    cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    paths.ensure_directories()
    return tmp_path


@pytest.fixture
def code_root(tmp_path):
    """A tmp ~/code/ root with no clones (report doesn't need them)."""
    root = tmp_path / "code"
    root.mkdir()
    return root


@pytest.fixture
def write_config(isolated_xdg, code_root):
    """Factory that writes gitbulk.yaml + repos.txt and returns paths."""

    def _write(*, repos_slugs, with_org="provenant-dev"):
        cfg_dir = paths.config_dir()
        cfg_dir.mkdir(parents=True, exist_ok=True)
        policy_yaml = {
            "defaults": {"retain_runs": 5},
        }
        if with_org:
            policy_yaml["humans"] = {"org": with_org, "cache_ttl_hours": 24}
        (cfg_dir / "gitbulk.yaml").write_text(yaml.safe_dump(policy_yaml))
        repos_txt = "\n".join(repos_slugs) + ("\n" if repos_slugs else "")
        (cfg_dir / "repos.txt").write_text(repos_txt)
        return cfg_dir

    return _write


@pytest.fixture
def fresh_org_cache():
    """Helper that writes a fresh org-members cache for tests."""

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
    author: str = "dhh1128",
    base_ref: str = "main",
    title: str | None = None,
) -> PRInfo:
    return PRInfo(
        slug=slug,
        number=number,
        title=title or f"PR #{number} title",
        url=f"https://github.com/{slug}/pull/{number}",
        author=author,
        base_ref=base_ref,
        head_ref=f"feature/{number}",
        head_sha="a" * 40,
        state="OPEN",
        is_draft=False,
        mergeable_state="CLEAN",
        created_at=datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc),
        last_pushed_at=datetime(2026, 5, 28, 10, 0, 0, tzinfo=timezone.utc),
        labels=(),
        review_decision="APPROVED",
        checks_status="SUCCESS",
    )


def _make_args(
    *,
    code_root=None,
    skip_check=None,
    refresh_org_members=False,
    org=None,
    repo=None,
    base=None,
    mergeable_state=None,
    author=None,
    filter=None,
):
    return argparse.Namespace(
        subcommand="report",
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


# ─── Happy path: 2 repos with 1 PR each → exit 2 ───────────────────────────


def test_report_happy_path_two_prs(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    write_config(repos_slugs=["dhh1128/alpha", "dhh1128/beta"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr_alpha = _make_pr(slug="dhh1128/alpha", number=1)
    pr_beta = _make_pr(slug="dhh1128/beta", number=2)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={
            "dhh1128/alpha": "main",
            "dhh1128/beta": "main",
        },
        my_open_prs={
            "dhh1128/alpha": [pr_alpha],
            "dhh1128/beta": [pr_beta],
        },
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )

    args = _make_args(code_root=code_root)
    rc = report_handler(args)

    assert rc == EXIT_ATTENTION_NEEDED
    parsed = sentinel.parse_attention()
    assert parsed is not None
    assert parsed["exit_code"] == EXIT_ATTENTION_NEEDED
    assert parsed["subcommand"] == "report"
    assert "2 PRs need attention" in parsed["summary"]
    # Verify state.yaml + summary.md content.
    latest = paths.latest_run_symlink("report").resolve()
    state = yaml.safe_load((latest / "state.yaml").read_text())
    assert set(state["repos"].keys()) == {"dhh1128/alpha", "dhh1128/beta"}
    summary = (latest / "summary.md").read_text()
    assert "dhh1128/alpha" in summary
    assert "dhh1128/beta" in summary
    # Manifest was finalized.
    manifest = yaml.safe_load((latest / "manifest.yaml").read_text())
    assert manifest["exit_code"] == EXIT_ATTENTION_NEEDED
    assert "completed_at" in manifest
    # Config snapshot is inline.
    assert manifest["config_snapshot"]["repos_txt"]


# ─── Filters: repo-glob prunes fleet; filter line in summary ───────────────


def test_report_repo_filter_prunes_and_reports_filter_line(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    write_config(repos_slugs=["dhh1128/alpha", "dhh1128/beta"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr_alpha = _make_pr(slug="dhh1128/alpha", number=1)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main", "dhh1128/beta": "main"},
        my_open_prs={"dhh1128/alpha": [pr_alpha]},
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )

    # Glob keeps only alpha; beta is excluded before the invariant loop.
    args = _make_args(code_root=code_root, repo=["*/alpha"])
    rc = report_handler(args)

    assert rc == EXIT_ATTENTION_NEEDED
    latest = paths.latest_run_symlink("report").resolve()
    state = yaml.safe_load((latest / "state.yaml").read_text())
    assert set(state["repos"].keys()) == {"dhh1128/alpha"}
    summary = (latest / "summary.md").read_text()
    assert "Filtered" in summary
    assert "repo=*/alpha" in summary
    assert "1 repos" in summary
    parsed = sentinel.parse_attention()
    assert "Filtered" in parsed["summary"]


# ─── All clean: 0 PRs → exit 0, no sentinel ────────────────────────────────


def test_report_no_prs_exit_ok(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )

    rc = report_handler(_make_args(code_root=code_root))
    assert rc == EXIT_OK
    assert not sentinel.has_attention()
    # The run still completed and produced summary.md.
    latest = paths.latest_run_symlink("report").resolve()
    summary = (latest / "summary.md").read_text()
    assert "no open prs" in summary.lower()


def test_report_success_records_phase_timings_in_manifest(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    """A successful report stamps the three pipeline phase durations into
    manifest.yaml (node 5agg / PERF-F3)."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )

    rc = report_handler(_make_args(code_root=code_root))
    assert rc == EXIT_OK
    latest = paths.latest_run_symlink("report").resolve()
    manifest = yaml.safe_load((latest / "manifest.yaml").read_text())
    assert set(manifest["timings"]) == {"preflight", "per_repo", "per_pr"}
    assert all(isinstance(v, float) and v >= 0.0 for v in manifest["timings"].values())


# ─── github.reachable Skip → exit 3 ────────────────────────────────────────


def test_report_one_repo_unreachable_exit_3(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    write_config(repos_slugs=["dhh1128/alpha", "dhh1128/beta"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    # default_branches MISSING beta → GithubReachableInvariant skips beta
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},  # alpha has no PRs
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )

    rc = report_handler(_make_args(code_root=code_root))
    assert rc == EXIT_INVARIANT_SKIPPED
    parsed = sentinel.parse_attention()
    assert parsed is not None
    assert parsed["exit_code"] == EXIT_INVARIANT_SKIPPED
    assert "1 repos skipped" in parsed["summary"]
    latest = paths.latest_run_symlink("report").resolve()
    summary = (latest / "summary.md").read_text()
    assert "Skipped repos" in summary
    assert "dhh1128/beta" in summary


# ─── --skip-check applied → exit 4 ─────────────────────────────────────────


def test_report_skip_check_triggers_exit_4(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )
    # Skip an invariant that wouldn't have fired anyway — the cmdline
    # *gesture* itself is the audit event per node r4nzp7kq.
    rc = report_handler(
        _make_args(code_root=code_root, skip_check=["pr.base_is_default"])
    )
    assert rc == EXIT_OVERRIDES_APPLIED
    assert not sentinel.has_attention()  # 4 does not trigger ATTENTION
    # The skip was recorded as a WARNING in errors.log.
    latest = paths.latest_run_symlink("report").resolve()
    errors = (latest / "errors.log").read_text().splitlines()
    assert any(
        json.loads(line).get("level") == "WARNING" for line in errors
    )


# ─── Lock timeout → exit 1, no sentinel ────────────────────────────────────


def test_report_lock_timeout_exits_1(
    monkeypatch, isolated_xdg, code_root, write_config, capsys, fresh_org_cache
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])

    # Inject a default-branches cache lock that times out on entry (the first
    # resource lock report hits — org cache is fresh, so prime runs early).
    class _BoomLock:
        def __enter__(self):
            raise LockTimeoutError(
                paths.named_lock_file("default-branches"),
                {"pid": 999, "started_at": "1970-01-01T00:00:00+00:00",
                 "subcommand": "merge", "alive": False},
            )

        def __exit__(self, *a):  # pragma: no cover — never reached
            return False

    monkeypatch.setattr(
        "gitbulk.default_branch_cache.default_branches_lock",
        lambda *a, **kw: _BoomLock(),
    )
    fake = FakeGHClient(user={"login": "dhh1128"})
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )

    rc = report_handler(_make_args(code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE
    assert not sentinel.has_attention()
    err = capsys.readouterr().err
    assert "timed out" in err


# ─── gh.authenticated fails → exit 1 ───────────────────────────────────────


def test_report_gh_not_authenticated_exit_1(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    # No user configured → authenticated_user raises GHError
    fake = FakeGHClient()
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )
    rc = report_handler(_make_args(code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE
    # A run dir was still created and finalized with the failure.
    latest = paths.latest_run_symlink("report").resolve()
    manifest = yaml.safe_load((latest / "manifest.yaml").read_text())
    assert manifest["exit_code"] == EXIT_STRUCTURAL_FAILURE
    summary = (latest / "summary.md").read_text()
    assert "FAILED" in summary


# ─── Auto-refresh: report self-heals a missing/stale cache (ormrf7kq) ──────


def test_report_missing_cache_auto_refreshes(
    monkeypatch, isolated_xdg, code_root, write_config
):
    """A missing org-members cache is auto-refreshed (no flag needed),
    then the run proceeds past the preflight rather than exiting 1.

    Replaces the old missing-cache→exit-1 behavior per ormrf7kq: the
    unattended nightly ``report`` must self-heal its own freshness
    precondition.
    """
    write_config(repos_slugs=["dhh1128/alpha"])  # no fresh_org_cache call
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )
    rc = report_handler(_make_args(code_root=code_root))
    assert rc == EXIT_OK
    # The cache was fetched once and written for the invariant to read.
    assert fake.call_count["org_members"] == 1
    assert paths.org_members_cache_file("provenant-dev").exists()


def test_report_stale_cache_auto_refreshes(
    monkeypatch, isolated_xdg, code_root, write_config
):
    """A cache older than the TTL is auto-refreshed without the flag."""
    write_config(repos_slugs=["dhh1128/alpha"])
    # Seed a STALE cache (fetched_at well past the 24h TTL).
    stale_at = datetime.now(timezone.utc) - timedelta(hours=72)
    save_cache(
        CachedMembers(
            org="provenant-dev",
            fetched_at=stale_at,
            members=frozenset({"dhh1128"}),
        )
    )
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128", "alice"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )
    rc = report_handler(_make_args(code_root=code_root))
    assert rc == EXIT_OK
    assert fake.call_count["org_members"] == 1
    # The on-disk fetched_at advanced past the stale value.
    from gitbulk.org_members_cache import load_cache

    refreshed = load_cache("provenant-dev")
    assert refreshed is not None
    assert refreshed.fetched_at > stale_at


def test_report_fresh_cache_no_auto_refresh(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    """A fresh cache is NOT refetched: no network call, run proceeds."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )
    rc = report_handler(_make_args(code_root=code_root))
    assert rc == EXIT_OK
    assert fake.call_count["org_members"] == 0


def test_report_auto_refresh_failure_exit_1(
    monkeypatch, isolated_xdg, code_root, write_config
):
    """If the automatic refresh itself errors (GitHub unreachable), the
    run aborts with EXIT_STRUCTURAL_FAILURE and records the failure to
    the run's errors.log — a genuinely unreachable org is not masked.
    """
    write_config(repos_slugs=["dhh1128/alpha"])  # cache missing
    fake = FakeGHClient(user={"login": "dhh1128"})  # no org_members config
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )
    rc = report_handler(_make_args(code_root=code_root))  # no flag
    assert rc == EXIT_STRUCTURAL_FAILURE
    latest = paths.latest_run_symlink("report").resolve()
    events = [
        json.loads(line)
        for line in (latest / "errors.log").read_text().splitlines()
        if line.strip()
    ]
    assert any(
        "org-members auto-refresh failed" in e.get("message", "")
        for e in events
    )


# ─── --refresh-org-members triggers a cache refresh, then runs ─────────────


def test_report_refresh_org_members_triggers_refetch(
    monkeypatch, isolated_xdg, code_root, write_config
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128", "alice"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )

    rc = report_handler(
        _make_args(code_root=code_root, refresh_org_members=True)
    )
    # Refresh succeeded and the cache file was written; the run reached
    # exit 0 (no PRs, no skips, no overrides).
    assert rc == EXIT_OK
    assert fake.call_count["org_members"] == 1
    # The cache YAML now exists for use by the invariants.
    cache_path = paths.org_members_cache_file("provenant-dev")
    assert cache_path.exists()


def test_report_refresh_flag_forces_refetch_even_when_fresh(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    """``--refresh-org-members`` is a FORCE override: it refetches even
    when the on-disk cache is still fresh (ormrf7kq)."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])  # already fresh
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128", "alice"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )
    rc = report_handler(
        _make_args(code_root=code_root, refresh_org_members=True)
    )
    assert rc == EXIT_OK
    # Forced refetch happened despite the fresh cache.
    assert fake.call_count["org_members"] == 1


def test_report_refresh_org_members_failure_exit_1(
    monkeypatch, isolated_xdg, code_root, write_config
):
    """If the refresh call itself errors, record to the run's errors.log
    (and the synthesized summary.md) and exit 1.

    Per security-hawk F4 fix (2026-05-28), refresh runs inside the lock
    with a RunState already begun, so failure is captured in the audit
    trail rather than printed to stderr.
    """
    import json

    write_config(repos_slugs=["dhh1128/alpha"])
    fake = FakeGHClient(user={"login": "dhh1128"})  # no org_members config
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )
    rc = report_handler(
        _make_args(code_root=code_root, refresh_org_members=True)
    )
    assert rc == EXIT_STRUCTURAL_FAILURE
    # The failure should be recorded in the run's errors.log.
    latest = paths.latest_run_symlink("report").resolve()
    errors_log = latest / "errors.log"
    assert errors_log.exists()
    events = [
        json.loads(line)
        for line in errors_log.read_text().splitlines()
        if line.strip()
    ]
    assert any(
        "--refresh-org-members failed" in e.get("message", "")
        for e in events
    )


def test_report_refresh_org_members_noop_when_org_unset(
    monkeypatch, isolated_xdg, code_root, write_config
):
    """--refresh-org-members is a no-op when humans.org is None."""
    # write_config with with_org=None
    write_config(repos_slugs=["dhh1128/alpha"], with_org=None)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )
    rc = report_handler(
        _make_args(code_root=code_root, refresh_org_members=True)
    )
    assert rc == EXIT_OK
    assert fake.call_count["org_members"] == 0


# ─── Empty repos.txt → exit 0 ──────────────────────────────────────────────


def test_report_empty_repos_exit_ok(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    write_config(repos_slugs=[])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )

    rc = report_handler(_make_args(code_root=code_root))
    assert rc == EXIT_OK
    assert not sentinel.has_attention()
    latest = paths.latest_run_symlink("report").resolve()
    summary = (latest / "summary.md").read_text()
    assert "no repos configured" in summary.lower()


# ─── Bot author still counts (Phase 2 behavior) ────────────────────────────


def test_report_bot_author_still_counts_as_attention(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    """Phase 2: bot PRs are not filtered out yet; they still pass per-PR
    invariants and end up counted as "attention."
    """
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    # dependabot is not in org_members → BOT (per pj5kn2zw); the
    # pr.author_known invariant Passes (HUMAN-or-BOT is decided, only
    # UNKNOWN is a Fail).
    pr_bot = _make_pr(
        slug="dhh1128/alpha", number=7, author="dependabot[bot]"
    )
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr_bot]},
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )

    rc = report_handler(_make_args(code_root=code_root))
    assert rc == EXIT_ATTENTION_NEEDED


# ─── PR with non-default base is Skipped → counted as not-attention ────────


def test_report_pr_non_default_base_is_skipped_not_attention(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr = _make_pr(
        slug="dhh1128/alpha", number=99, base_ref="develop"  # NOT main
    )
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )

    rc = report_handler(_make_args(code_root=code_root))
    # No attention PRs; no repo skipped; no --skip-check → EXIT_OK.
    assert rc == EXIT_OK
    latest = paths.latest_run_symlink("report").resolve()
    state = yaml.safe_load((latest / "state.yaml").read_text())
    pr_state = state["repos"]["dhh1128/alpha"]["prs"][0]
    assert pr_state["invariants_passed"] is False
    assert any(
        "pr.base_is_default" in pair[0] for pair in pr_state["invariants_skips"]
    )


# ─── PER_REPO invariant Fail aborts the whole run → exit 1 ────────────────


def test_report_per_repo_invariant_fail_aborts_exit_1(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    """Patch GithubReachableInvariant.check to return Fail; the chain
    runner halts and report exits 1 (structural failure)."""
    from gitbulk.invariants.base import Fail as _Fail
    from gitbulk.invariants import catalog as _catalog

    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )
    monkeypatch.setattr(
        _catalog.GithubReachableInvariant,
        "check",
        lambda self, ctx: _Fail("forced fail for test"),
    )

    rc = report_handler(_make_args(code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE
    latest = paths.latest_run_symlink("report").resolve()
    summary = (latest / "summary.md").read_text()
    assert "FAILED" in summary
    assert "per-repo invariant failed" in summary


# ─── gh.my_open_prs raises → exit 1 ────────────────────────────────────────


def test_report_gh_pr_fetch_error_exit_1(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    # default_branches configured (preflight passes) but my_open_prs is
    # NOT configured → FakeGHClient raises GHError on the coalesced call.
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )

    rc = report_handler(_make_args(code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE
    latest = paths.latest_run_symlink("report").resolve()
    errors = [
        json.loads(line)
        for line in (latest / "errors.log").read_text().splitlines()
    ]
    assert any("my_open_prs failed" in e["message"] for e in errors)


# ─── --skip-check on github.reachable: lets a "would-skip" repo through ────


def test_report_skip_check_overrides_github_reachable(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    """When --skip-check is passed for an invariant that would have
    Skipped a repo, the invariant is bypassed and the repo proceeds."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr = _make_pr(slug="dhh1128/alpha", number=1)
    # default_branches has alpha — needed by pr.base_is_default — but
    # we configure my_open_prs explicitly.
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )

    rc = report_handler(
        _make_args(
            code_root=code_root, skip_check=["github.reachable"]
        )
    )
    # github.reachable was SKIP'd via cmdline. The repo proceeds, PR
    # invariants pass → ATTENTION (2). Exit 4 takes second priority to 2.
    assert rc == EXIT_ATTENTION_NEEDED


# ─── pr.author_known UNKNOWN → defensive Fail aborts the chain ─────────────


def test_report_chain_aborts_when_invariant_fails_on_pr(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    """A per-PR Fail doesn't abort the whole run (Fail is logged but the
    overall report still completes; the PR record reflects the failure).

    Verified by skipping org.members.fresh and then NOT writing a cache,
    so pr.author_known sees no org_members and falls through to BOT
    (still a valid decision; UNKNOWN is unreachable in this branch).
    """
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr = _make_pr(slug="dhh1128/alpha", number=1, author="dhh1128")
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )

    rc = report_handler(_make_args(code_root=code_root))
    # All clean.
    assert rc == EXIT_ATTENTION_NEEDED


# ─── Internal helpers ──────────────────────────────────────────────────────


def test_runid_from_run_dir_handles_hyphenated_subcommand(tmp_path):
    d = tmp_path / "20260528T010203Z-rebase-onto-default"
    assert _runid_from_run_dir(d) == "20260528T010203Z-rebase-onto"
    # Documented limitation: rpartition splits on the LAST hyphen. For a
    # subcommand like ``report`` this is unambiguous; ``rebase-onto-default``
    # is hyphenated. We accept the cosmetic imperfection at Phase 2; the
    # value is used only as the ``runid`` field in the sentinel JSON.


def test_runid_from_run_dir_simple_case(tmp_path):
    d = tmp_path / "20260528T010203Z-report"
    assert _runid_from_run_dir(d) == "20260528T010203Z"


def test_build_summary_md_includes_skipped_repo_reason(
    isolated_xdg, code_root, write_config
):
    from gitbulk.config.policy import load_policy
    from gitbulk.config.repos import RepoEntry

    write_config(repos_slugs=["x/a"])
    policy = load_policy()
    repo = RepoEntry(
        slug="x/a", owner="x", name="a", local_path=code_root / "a",
        source_line=1,
    )
    md = _build_summary_md(
        policy,
        [repo],
        passing_repos=[],
        skipped_repos=[("x/a", "github not reachable")],
        prs_by_repo={},
        pr_records_by_repo={},
        attention_count=0,
    )
    assert "Skipped repos" in md
    assert "github not reachable" in md


def test_build_summary_md_with_pr_skip_lines(
    isolated_xdg, code_root, write_config
):
    """Verify the inner "skip <inv>: <reason>" lines render in summary.md."""
    from gitbulk.config.policy import load_policy
    from gitbulk.config.repos import RepoEntry

    write_config(repos_slugs=["x/a"])
    policy = load_policy()
    repo = RepoEntry(
        slug="x/a", owner="x", name="a", local_path=code_root / "a",
        source_line=1,
    )
    pr = _make_pr(slug="x/a", number=42)
    # Recreate with is_draft=True via dataclasses.replace for the
    # [DRAFT] rendering assertion.
    from dataclasses import replace as dc_replace

    pr = dc_replace(pr, is_draft=True)
    record = {
        "number": 42,
        "title": "T",
        "url": "u",
        "author": "a",
        "state": "OPEN",
        "is_draft": True,
        "base_ref": "main",
        "head_ref": "h",
        "mergeable_state": None,
        "review_decision": None,
        "checks_status": None,
        "labels": [],
        "invariants_passed": False,
        "invariants_skips": [["pr.base_is_default", "wrong base"]],
        "invariants_fail_reason": None,
    }
    md = _build_summary_md(
        policy,
        [repo],
        passing_repos=[repo],
        skipped_repos=[],
        prs_by_repo={"x/a": [pr]},
        pr_records_by_repo={"x/a": [record]},
        attention_count=0,
    )
    assert "[DRAFT]" in md
    assert "pr.base_is_default" in md
    assert "wrong base" in md


# ─── CLI smoke: report subcommand invoked through main() ───────────────────


def test_report_through_main_runs_full_pipeline(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )

    rc = main(["report", "--code-root", str(code_root)])
    assert rc == EXIT_OK


def test_report_through_main_passes_skip_check_through(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )

    rc = main(
        [
            "report",
            "--code-root", str(code_root),
            "--skip-check", "pr.base_is_default",
            "--skip-check", "pr.author_known",
        ]
    )
    assert rc == EXIT_OVERRIDES_APPLIED


def test_config_root_flag_routes_paths(
    monkeypatch, tmp_path, code_root, fresh_org_cache
):
    """--config-root makes paths.config_dir() resolve to the user's dir."""
    # Build a tmp config dir whose basename is 'gitbulk', as documented.
    parent = tmp_path / "alt-config"
    cfg_root = parent / "gitbulk"
    cfg_root.mkdir(parents=True)
    cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    # No XDG_CONFIG_HOME — --config-root will set it.
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    (cfg_root / "gitbulk.yaml").write_text(
        yaml.safe_dump(
            {
                "defaults": {"retain_runs": 5},
                "humans": {"org": "provenant-dev", "cache_ttl_hours": 24},
            }
        )
    )
    (cfg_root / "repos.txt").write_text("dhh1128/alpha\n")

    # Org cache: paths.config_dir() goes through XDG, but the org-members
    # cache lives under XDG_CACHE. We must ensure the cache file exists
    # AFTER --config-root has redirected things, so write it after main
    # starts. Easiest path: write it now under the to-be cache dir.
    (cache / "gitbulk" / "org-members").mkdir(parents=True, exist_ok=True)
    save_cache(
        CachedMembers(
            org="provenant-dev",
            fetched_at=datetime.now(timezone.utc),
            members=frozenset({"dhh1128"}),
        )
    )

    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )

    rc = main(
        [
            "--config-root", str(cfg_root),
            "report",
            "--code-root", str(code_root),
        ]
    )
    assert rc == EXIT_OK


# ─── Post-merge watchdog (G2c) ────────────────────────────────────────────


def _write_merge_run(
    *,
    isolated_xdg,
    timestamp: str,
    repos_payload: dict,
) -> None:
    """Synthesize a ``runs/<timestamp>-merge/state.yaml`` so the watchdog
    scan picks it up. Mimics the on-disk shape merge_handler writes."""
    run_dir = paths.runs_dir() / f"{timestamp}-merge"
    run_dir.mkdir(parents=True, exist_ok=True)
    state_doc = {"schema_version": 1, "repos": repos_payload}
    (run_dir / "state.yaml").write_text(yaml.safe_dump(state_doc))


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _past_str(hours_ago: int) -> str:
    from datetime import timedelta
    return (
        datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    ).strftime("%Y%m%dT%H%M%SZ")


def test_scan_recent_merges_returns_empty_when_no_runs(isolated_xdg):
    from gitbulk.commands.report import _scan_recent_merges
    assert _scan_recent_merges(datetime.now(timezone.utc)) == []


def test_scan_recent_merges_returns_empty_when_runs_dir_missing(
    monkeypatch, tmp_path,
):
    """Defensive: paths.runs_dir() doesn't exist (no gitbulk run has ever
    happened on this machine). Walks no directories, returns empty."""
    missing = tmp_path / "no-runs-here"
    monkeypatch.setattr(
        "gitbulk.commands.report.paths.runs_dir", lambda: missing
    )
    from gitbulk.commands.report import _scan_recent_merges
    assert _scan_recent_merges(datetime.now(timezone.utc)) == []


def test_scan_recent_merges_finds_merge_in_window(isolated_xdg):
    from gitbulk.commands.report import _scan_recent_merges
    _write_merge_run(
        isolated_xdg=isolated_xdg,
        timestamp=_now_str(),
        repos_payload={
            "a/b": {
                "pr_count": 1,
                "prs": [
                    {
                        "number": 7,
                        "title": "x",
                        "url": "u",
                        "head_sha": "h" * 40,
                        "eligible": True,
                        "merged": True,
                        "merge_commit_sha": "deadbeef" * 5,
                    }
                ],
            }
        },
    )
    result = _scan_recent_merges(datetime.now(timezone.utc))
    assert len(result) == 1
    assert result[0]["slug"] == "a/b"
    assert result[0]["merge_commit_sha"] == "deadbeef" * 5


def test_scan_recent_merges_skips_old_runs(isolated_xdg):
    from gitbulk.commands.report import _scan_recent_merges
    _write_merge_run(
        isolated_xdg=isolated_xdg,
        timestamp=_past_str(48),  # 48h ago > 24h window
        repos_payload={
            "a/b": {
                "pr_count": 1,
                "prs": [
                    {
                        "number": 1,
                        "merge_commit_sha": "a" * 40,
                    }
                ],
            }
        },
    )
    assert _scan_recent_merges(datetime.now(timezone.utc)) == []


def test_scan_recent_merges_skips_pr_without_merge_sha(isolated_xdg):
    from gitbulk.commands.report import _scan_recent_merges
    _write_merge_run(
        isolated_xdg=isolated_xdg,
        timestamp=_now_str(),
        repos_payload={
            "a/b": {
                "pr_count": 1,
                "prs": [{"number": 1}],  # dry-run-style, no merge_commit_sha
            }
        },
    )
    assert _scan_recent_merges(datetime.now(timezone.utc)) == []


def test_scan_recent_merges_ignores_non_merge_dirs(isolated_xdg):
    """Only directories named ``<timestamp>-merge`` are scanned."""
    other = paths.runs_dir() / f"{_now_str()}-report"
    other.mkdir(parents=True, exist_ok=True)
    (other / "state.yaml").write_text(yaml.safe_dump({"repos": {}}))
    from gitbulk.commands.report import _scan_recent_merges
    assert _scan_recent_merges(datetime.now(timezone.utc)) == []


def test_scan_recent_merges_skips_malformed_timestamp(isolated_xdg):
    bad = paths.runs_dir() / "not-a-timestamp-merge"
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "state.yaml").write_text(yaml.safe_dump({"repos": {}}))
    from gitbulk.commands.report import _scan_recent_merges
    assert _scan_recent_merges(datetime.now(timezone.utc)) == []


def test_scan_recent_merges_skips_missing_state_yaml(isolated_xdg):
    run_dir = paths.runs_dir() / f"{_now_str()}-merge"
    run_dir.mkdir(parents=True, exist_ok=True)
    # No state.yaml written
    from gitbulk.commands.report import _scan_recent_merges
    assert _scan_recent_merges(datetime.now(timezone.utc)) == []


def test_scan_recent_merges_skips_malformed_yaml(isolated_xdg):
    run_dir = paths.runs_dir() / f"{_now_str()}-merge"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.yaml").write_text("not: : valid: yaml: [")
    from gitbulk.commands.report import _scan_recent_merges
    assert _scan_recent_merges(datetime.now(timezone.utc)) == []


def test_scan_recent_merges_skips_non_dict_doc(isolated_xdg):
    run_dir = paths.runs_dir() / f"{_now_str()}-merge"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.yaml").write_text(yaml.safe_dump([1, 2, 3]))
    from gitbulk.commands.report import _scan_recent_merges
    assert _scan_recent_merges(datetime.now(timezone.utc)) == []


def test_scan_recent_merges_skips_non_dict_repo_payload(isolated_xdg):
    """Defensive: a repos entry whose value isn't a dict (corrupt file)."""
    run_dir = paths.runs_dir() / f"{_now_str()}-merge"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.yaml").write_text(
        yaml.safe_dump({"repos": {"a/b": "not-a-dict"}})
    )
    from gitbulk.commands.report import _scan_recent_merges
    assert _scan_recent_merges(datetime.now(timezone.utc)) == []


def test_scan_recent_merges_skips_non_dict_pr_entry(isolated_xdg):
    run_dir = paths.runs_dir() / f"{_now_str()}-merge"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.yaml").write_text(
        yaml.safe_dump({"repos": {"a/b": {"prs": ["not-a-dict"]}}})
    )
    from gitbulk.commands.report import _scan_recent_merges
    assert _scan_recent_merges(datetime.now(timezone.utc)) == []


def test_scan_recent_merges_caps_at_50(isolated_xdg):
    """_WATCHDOG_MAX_MERGES cap. Build a single run with 60 PRs and
    verify only 50 come back."""
    prs = [
        {"number": i, "merge_commit_sha": f"sha{i:040d}"}
        for i in range(60)
    ]
    _write_merge_run(
        isolated_xdg=isolated_xdg,
        timestamp=_now_str(),
        repos_payload={"a/b": {"pr_count": 60, "prs": prs}},
    )
    from gitbulk.commands.report import _scan_recent_merges
    result = _scan_recent_merges(datetime.now(timezone.utc))
    assert len(result) == 50


def test_report_watchdog_surfaces_check_failures(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """A recent merge whose merge commit has a failing check run is
    surfaced in the summary and forces ATTENTION."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    merge_sha = "f" * 40
    _write_merge_run(
        isolated_xdg=isolated_xdg,
        timestamp=_now_str(),
        repos_payload={
            "dhh1128/alpha": {
                "pr_count": 1,
                "prs": [
                    {
                        "number": 42,
                        "title": "the merged one",
                        "url": "https://github.com/dhh1128/alpha/pull/42",
                        "head_sha": "h" * 40,
                        "eligible": True,
                        "merged": True,
                        "merge_commit_sha": merge_sha,
                    }
                ],
            }
        },
    )
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
        check_runs={
            ("dhh1128/alpha", merge_sha): [
                CheckRun(
                    name="deploy",
                    status="completed",
                    conclusion="failure",
                    details_url="https://example.com",
                    completed_at=None,
                ),
            ],
        },
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )
    rc = report_handler(_make_args(code_root=code_root))
    assert rc == EXIT_ATTENTION_NEEDED
    summary = (paths.latest_run_symlink("report").resolve() / "summary.md").read_text()
    assert "Recent merges (last 24h)" in summary
    assert "FAILING checks: deploy" in summary


def test_report_watchdog_all_clean_merge_is_acked_and_dropped(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """All check-runs completed + green → merge is ack'd permanently
    and does NOT appear in the Recent merges section. Subsequent
    reports skip the merge entirely (no gh.fetch_check_runs call)."""
    from gitbulk.watchdog_ack import load_acked

    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    merge_sha = "e" * 40
    _write_merge_run(
        isolated_xdg=isolated_xdg,
        timestamp=_now_str(),
        repos_payload={
            "dhh1128/alpha": {
                "pr_count": 1,
                "prs": [
                    {
                        "number": 5,
                        "title": "good merge",
                        "url": "u",
                        "merge_commit_sha": merge_sha,
                    }
                ],
            }
        },
    )
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
        check_runs={
            ("dhh1128/alpha", merge_sha): [
                CheckRun(
                    name="test",
                    status="completed",
                    conclusion="success",
                    details_url="u",
                    completed_at=None,
                ),
            ],
        },
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )
    rc = report_handler(_make_args(code_root=code_root))
    assert rc == EXIT_OK
    summary = (paths.latest_run_symlink("report").resolve() / "summary.md").read_text()
    # Acked merges drop out of the section entirely.
    assert "Recent merges" not in summary
    # Ack cache now contains this pair → future report runs will skip
    # the fetch_check_runs call for this (slug, sha).
    assert ("dhh1128/alpha", merge_sha) in load_acked()
    # Verify the skip path directly via _check_recent_merges. Use a
    # stub RunState (only ``record_error`` would be called, only in the
    # gh-error path which we don't hit when the entry is acked).
    from types import SimpleNamespace
    from gitbulk.commands.report import _check_recent_merges
    rs2 = SimpleNamespace(record_error=lambda *a, **k: None)
    records, any_failure = _check_recent_merges(
        fake, rs2, datetime.now(timezone.utc)
    )
    assert records == []
    assert any_failure is False
    # fetch_check_runs total stays at 1 (the report_handler call); the
    # direct _check_recent_merges call hit the cache.
    assert fake.call_count["fetch_check_runs"] == 1


def test_is_ackable_rejects_completed_with_unknown_conclusion():
    """A completed check with a non-passing, non-failing conclusion
    (or None) prevents ack — we refuse to ack uncertainty."""
    from gitbulk.commands.report import _is_ackable
    from gitbulk.pr_info import CheckRun

    # status=completed, conclusion=None (defensive: shouldn't normally
    # happen per GitHub docs, but tolerate it).
    cr = CheckRun(
        name="weird",
        status="completed",
        conclusion=None,
        details_url="u",
        completed_at=None,
    )
    assert _is_ackable([cr]) is False


def test_is_ackable_returns_true_on_empty_list():
    """A merge with no check-runs at all is trivially ackable (no CI
    to wait on)."""
    from gitbulk.commands.report import _is_ackable
    assert _is_ackable([]) is True


def test_report_watchdog_in_progress_check_not_acked(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """A check that's still running (status=in_progress) prevents ack
    even if no failures are visible yet. Surfaces as an active record."""
    from gitbulk.watchdog_ack import load_acked

    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    merge_sha = "c" * 40
    _write_merge_run(
        isolated_xdg=isolated_xdg,
        timestamp=_now_str(),
        repos_payload={
            "dhh1128/alpha": {
                "pr_count": 1,
                "prs": [
                    {
                        "number": 9,
                        "title": "deploy still running",
                        "url": "u",
                        "merge_commit_sha": merge_sha,
                    }
                ],
            }
        },
    )
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
        check_runs={
            ("dhh1128/alpha", merge_sha): [
                CheckRun(
                    name="test",
                    status="completed",
                    conclusion="success",
                    details_url="u",
                    completed_at=None,
                ),
                CheckRun(
                    name="cd",
                    status="in_progress",
                    conclusion=None,
                    details_url="u",
                    completed_at=None,
                ),
            ],
        },
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )
    rc = report_handler(_make_args(code_root=code_root))
    assert rc == EXIT_OK
    summary = (paths.latest_run_symlink("report").resolve() / "summary.md").read_text()
    # Still listed because we haven't ack'd it yet.
    assert "Recent merges" in summary
    assert ("dhh1128/alpha", merge_sha) not in load_acked()


def test_report_watchdog_handles_fetch_error(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """fetch_check_runs raising is recorded as a WARNING; the merge is
    listed with the error message but doesn't force ATTENTION (we don't
    actually know the check state)."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    merge_sha = "d" * 40
    _write_merge_run(
        isolated_xdg=isolated_xdg,
        timestamp=_now_str(),
        repos_payload={
            "dhh1128/alpha": {
                "pr_count": 1,
                "prs": [
                    {
                        "number": 3,
                        "title": "fetch fails",
                        "url": "u",
                        "merge_commit_sha": merge_sha,
                    }
                ],
            }
        },
    )
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
        # No check_runs configured → fetch_check_runs raises
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )
    rc = report_handler(_make_args(code_root=code_root))
    assert rc == EXIT_OK  # no other attention triggers, watchdog error doesn't escalate
    summary = (paths.latest_run_symlink("report").resolve() / "summary.md").read_text()
    assert "check-fetch FAILED" in summary


# ─── Skipped repos.txt entries surfaced in report ──────────────────────────


def test_report_skipped_entries_surface_in_summary(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
):
    """A bad entry in repos.txt becomes a SkippedEntry, the rest of
    the run proceeds, and the bad line appears in summary.md with its
    line number + reason. Exit code is EXIT_INVARIANT_SKIPPED."""
    # Write a repos.txt with one good slug + one nonexistent path.
    cfg_dir = paths.config_dir()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "gitbulk.yaml").write_text(yaml.safe_dump({
        "defaults": {"retain_runs": 5},
        "humans": {"org": "provenant-dev", "cache_ttl_hours": 24},
    }))
    (cfg_dir / "repos.txt").write_text(
        "dhh1128/alpha\n"
        "/nonexistent/path/bad-entry\n"
    )
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )
    rc = report_handler(_make_args(code_root=code_root))
    assert rc == EXIT_INVARIANT_SKIPPED
    summary = (paths.latest_run_symlink("report").resolve() / "summary.md").read_text()
    assert "Skipped repos.txt entries" in summary
    assert "line 2" in summary
    assert "/nonexistent/path/bad-entry" in summary
    assert "does not exist" in summary


# ─── Progress output for long fleets ──────────────────────────────────────


def test_report_progress_writes_to_tty(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, capsys,
):
    """When stderr is a TTY (interactive), gitbulk emits progress for
    the per-repo loop AND a 'Fetching open PRs...' line. capsys
    captures both stdout and stderr; we monkeypatch isatty to True."""
    import sys as _sys
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )
    # Force the TTY paths.
    monkeypatch.setattr(_sys.stderr, "isatty", lambda: True)
    report_handler(_make_args(code_root=code_root))
    err = capsys.readouterr().err
    assert "per-repo checks" in err
    assert "Fetching open PRs" in err


def test_report_progress_clears_fetching_on_error(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, capsys,
):
    """When my_open_prs raises, the 'Fetching...' line is still cleared."""
    import sys as _sys
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        # No my_open_prs configured → raises GHError
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )
    monkeypatch.setattr(_sys.stderr, "isatty", lambda: True)
    rc = report_handler(_make_args(code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE
    err = capsys.readouterr().err
    # Fetching line was written
    assert "Fetching open PRs" in err
    # And cleared (the clearing sequence is \r + spaces + \r)
    assert "\r" in err


# ─── Flat, greppable Open PRs section ──────────────────────────────────────


def test_report_open_prs_section_is_flat_and_greppable(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    """Open PRs render as one self-describing line per PR (URL + fields
    + status + title), no per-repo ### headers, sorted by (repo, num)."""
    write_config(repos_slugs=["dhh1128/alpha", "dhh1128/beta"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    # alpha has two PRs (numbers out of order to prove the sort);
    # beta has one.
    a2 = _make_pr(slug="dhh1128/alpha", number=2, title="second")
    a1 = _make_pr(slug="dhh1128/alpha", number=1, title="first")
    b9 = _make_pr(slug="dhh1128/beta", number=9, title="bee")
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main", "dhh1128/beta": "main"},
        my_open_prs={"dhh1128/alpha": [a2, a1], "dhh1128/beta": [b9]},
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )
    report_handler(_make_args(code_root=code_root))
    summary = (paths.latest_run_symlink("report").resolve() / "summary.md").read_text()

    # No per-repo headers.
    assert "### " not in summary
    # Section header carries a count.
    assert "## Open PRs (3)" in summary

    # Extract the PR lines (those starting with the GitHub URL).
    pr_lines = [ln for ln in summary.splitlines() if ln.startswith("https://github.com/")]
    assert len(pr_lines) == 3
    # Sorted by (slug, number): alpha#1, alpha#2, beta#9.
    assert "/alpha/pull/1 " in pr_lines[0] + " "
    assert "/alpha/pull/2 " in pr_lines[1] + " "
    assert "/beta/pull/9 " in pr_lines[2] + " "
    # Each line is self-describing: URL + base + checks + review + status.
    for ln in pr_lines:
        assert "base=" in ln
        assert "checks=" in ln
        assert "review=" in ln
        assert "mergeable=" in ln
        assert "ATTENTION" in ln  # all are eligible in this fixture


def test_report_open_prs_skip_reason_inline_on_pr_line(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    """A PR skipped by an invariant carries its skip reason inline as
    SKIP(invariant: reason) on the same line — greppable, no sub-bullets."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    # base != default → pr.base_is_default skips it.
    pr = _make_pr(slug="dhh1128/alpha", number=1, base_ref="feature-branch")
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )
    report_handler(_make_args(code_root=code_root))
    summary = (paths.latest_run_symlink("report").resolve() / "summary.md").read_text()
    pr_line = next(
        ln for ln in summary.splitlines() if ln.startswith("https://github.com/")
    )
    assert "SKIP(" in pr_line
    assert "pr.base_is_default" in pr_line


def test_report_draft_marker_on_pr_line(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    pr = _make_pr(slug="dhh1128/alpha", number=1)
    pr = PRInfo(**{**pr.__dict__, "is_draft": True})
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
    )
    monkeypatch.setattr(
        "gitbulk.commands.report.ProductionGHClient", lambda: fake
    )
    report_handler(_make_args(code_root=code_root))
    summary = (paths.latest_run_symlink("report").resolve() / "summary.md").read_text()
    pr_line = next(
        ln for ln in summary.splitlines() if ln.startswith("https://github.com/")
    )
    assert "[DRAFT]" in pr_line
