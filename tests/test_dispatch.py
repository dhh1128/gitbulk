"""End-to-end tests for ``gitbulk dispatch`` (this.i node ``execk7nm``).

Pipeline: invariants → eligible-PR filtering → worktree creation →
bounded-parallel claude → result recording → exit code. Every test
either monkeypatches ``execute_targets`` (so no claude/Popen runs)
and the worktree helpers (so no real ``git worktree`` runs), or
explicitly inspects the dry-run path which short-circuits before
either subsystem is touched.

All gh calls go through :class:`FakeGHClient` per AGENTS.md
"no network in tests."
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from gitbulk import paths, sentinel
from gitbulk.cli import main
from gitbulk.commands import dispatch as dispatch_mod
from gitbulk.commands.dispatch import (
    EXIT_ATTENTION_NEEDED,
    EXIT_INVARIANT_SKIPPED,
    EXIT_OK,
    EXIT_OVERRIDES_APPLIED,
    EXIT_STRUCTURAL_FAILURE,
    _build_summary_md,
    _key_for_pr,
    _parse_agent_outcome,
    _runid_from_run_dir,
    _salvage_escalation,
    _validate_prompt,
    dispatch_handler,
)
from gitbulk.exec import ExecResult
from gitbulk.gh import FakeGHClient
from gitbulk.locks import LockTimeoutError
from gitbulk.org_members_cache import CachedMembers, save_cache
from gitbulk.pr_info import PRInfo


# ─── Fixtures ──────────────────────────────────────────────────────────────


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
def fake_clones(code_root):
    """Factory that materializes a fake "clone" under code_root.

    The per-repo invariants (local.exists, local.remote_matches,
    local.default_branch_in_sync) introspect a git working tree. Most
    tests below pass ``--skip-check local.exists`` etc. to bypass them
    so we don't have to materialize anything fancy. A handful of tests
    need a REAL local clone to exercise the no-skip-check exit-code
    branches; for those, pass ``init=True`` to make this factory shell
    out to ``git init`` and pin origin URL + ``origin/HEAD``.
    """
    import subprocess as _sp

    def _make(slug, *, init: bool = False, default_branch: str = "main"):
        owner, name = slug.split("/", 1)
        path = code_root / name
        path.mkdir(parents=True, exist_ok=True)
        if init:
            # `git init -b <branch>` (modern git) ensures HEAD points at
            # the desired branch without needing a commit first.
            _sp.run(
                ["git", "init", "-q", "-b", default_branch, str(path)],
                check=True, capture_output=True,
            )
            _sp.run(
                [
                    "git", "-C", str(path), "remote", "add", "origin",
                    f"git@github.com:{slug}.git",
                ],
                check=True, capture_output=True,
            )
            # Create a real origin/HEAD symref by faking a remote ref.
            # We need a SHA for origin/<default_branch>: make an initial
            # commit on local <default_branch>, then mirror it to a
            # refs/remotes/origin/<default_branch>.
            _sp.run(
                [
                    "git", "-C", str(path), "-c", "user.email=t@t.t",
                    "-c", "user.name=t", "commit", "--allow-empty", "-q",
                    "-m", "init",
                ],
                check=True, capture_output=True,
            )
            head_sha = _sp.run(
                ["git", "-C", str(path), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            _sp.run(
                [
                    "git", "-C", str(path), "update-ref",
                    f"refs/remotes/origin/{default_branch}", head_sha,
                ],
                check=True, capture_output=True,
            )
            _sp.run(
                [
                    "git", "-C", str(path), "symbolic-ref",
                    "refs/remotes/origin/HEAD",
                    f"refs/remotes/origin/{default_branch}",
                ],
                check=True, capture_output=True,
            )
        return path

    return _make


@pytest.fixture
def write_config(isolated_xdg, code_root):
    def _write(*, repos_slugs, with_org="provenant-dev"):
        cfg_dir = paths.config_dir()
        cfg_dir.mkdir(parents=True, exist_ok=True)
        policy_yaml = {"defaults": {"retain_runs": 5}}
        if with_org:
            policy_yaml["humans"] = {"org": with_org, "cache_ttl_hours": 24}
        (cfg_dir / "gitbulk.yaml").write_text(yaml.safe_dump(policy_yaml))
        repos_txt = "\n".join(repos_slugs) + ("\n" if repos_slugs else "")
        (cfg_dir / "repos.txt").write_text(repos_txt)
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


@pytest.fixture
def prompt_file(tmp_path):
    """A non-empty prompt file the dispatch handler can read."""
    p = tmp_path / "prompt.md"
    p.write_text("Triage this PR and report.\n")
    return p


def _make_pr(
    *,
    slug: str,
    number: int,
    author: str = "dhh1128",
    base_ref: str = "main",
    title: str | None = None,
    head_sha: str = "a" * 40,
) -> PRInfo:
    return PRInfo(
        slug=slug,
        number=number,
        title=title or f"PR #{number} title",
        url=f"https://github.com/{slug}/pull/{number}",
        author=author,
        base_ref=base_ref,
        head_ref=f"feature/{number}",
        head_sha=head_sha,
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
    prompt=None,
    apply=False,
    code_root=None,
    skip_check=None,
    concurrency=2,
    timeout=1800.0,
    filter=None,
    refresh_org_members=False,
):
    return argparse.Namespace(
        subcommand="dispatch",
        prompt=str(prompt) if prompt else None,
        apply=apply,
        code_root=str(code_root) if code_root else None,
        skip_check=list(skip_check) if skip_check else None,
        concurrency=concurrency,
        timeout=timeout,
        filter=filter,
        refresh_org_members=refresh_org_members,
    )


# Shorthand for the local.* invariants we routinely skip in tests; we're
# not exercising the local-clone preflight here, only the dispatch
# orchestration.
_LOCAL_SKIPS = [
    "local.exists",
    "local.remote_matches",
    "local.default_branch_in_sync",
]


# ─── Prompt-validation branches ────────────────────────────────────────────


def test_dispatch_missing_prompt_arg_exits_structural(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(user={"login": "dhh1128"})
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionGHClient", lambda: fake
    )
    rc = dispatch_handler(_make_args(prompt=None, code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE


def test_dispatch_prompt_path_missing_exits_structural(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, tmp_path
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(user={"login": "dhh1128"})
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionGHClient", lambda: fake
    )
    nonexistent = tmp_path / "does-not-exist.md"
    rc = dispatch_handler(_make_args(prompt=nonexistent, code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE


def test_dispatch_prompt_file_empty_exits_structural(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache, tmp_path
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient(user={"login": "dhh1128"})
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionGHClient", lambda: fake
    )
    empty = tmp_path / "empty.md"
    empty.write_text("")
    rc = dispatch_handler(_make_args(prompt=empty, code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE


def test_validate_prompt_unit():
    # Missing arg
    p, err = _validate_prompt(argparse.Namespace(prompt=None))
    assert p is None and "requires --prompt" in err


# ─── Auto-refresh of the org-members cache (ormrf7kq) ──────────────────────


def test_dispatch_auto_refreshes_missing_cache(
    monkeypatch, isolated_xdg, code_root, write_config, prompt_file
):
    """A missing org-members cache auto-refreshes inside the lock rather
    than hard-failing the preflight; the run proceeds past it."""
    write_config(repos_slugs=["dhh1128/alpha"])  # no fresh_org_cache
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionGHClient", lambda: fake
    )
    rc = dispatch_handler(
        _make_args(prompt=prompt_file, code_root=code_root, skip_check=_LOCAL_SKIPS)
    )
    # Got past the org.members.fresh preflight (not a structural failure).
    assert rc != EXIT_STRUCTURAL_FAILURE
    assert fake.call_count["org_members"] == 1
    assert paths.org_members_cache_file("provenant-dev").exists()


def test_dispatch_auto_refresh_failure_exits_structural(
    monkeypatch, isolated_xdg, code_root, write_config, prompt_file
):
    """A failed automatic refresh (GitHub unreachable) aborts with exit 1
    and records the failure."""
    import json

    write_config(repos_slugs=["dhh1128/alpha"])  # cache missing
    fake = FakeGHClient(user={"login": "dhh1128"})  # no org_members → raises
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionGHClient", lambda: fake
    )
    rc = dispatch_handler(_make_args(prompt=prompt_file, code_root=code_root))
    assert rc == EXIT_STRUCTURAL_FAILURE
    latest = paths.latest_run_symlink("dispatch").resolve()
    events = [
        json.loads(line)
        for line in (latest / "errors.log").read_text().splitlines()
        if line.strip()
    ]
    assert any(
        "org-members auto-refresh failed" in e.get("message", "") for e in events
    )


# ─── Dry-run: default behavior ─────────────────────────────────────────────


def test_dispatch_dry_run_with_eligible_prs(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
    prompt_file, fake_clones,
):
    """Dry-run lists what WOULD dispatch and exits OK; no claude, no
    worktree."""
    write_config(repos_slugs=["dhh1128/alpha", "dhh1128/beta"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake_clones("dhh1128/alpha")
    fake_clones("dhh1128/beta")
    pr1 = _make_pr(slug="dhh1128/alpha", number=1)
    pr2 = _make_pr(slug="dhh1128/beta", number=2)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={
            "dhh1128/alpha": "main",
            "dhh1128/beta": "main",
        },
        my_open_prs={
            "dhh1128/alpha": [pr1],
            "dhh1128/beta": [pr2],
        },
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionGHClient", lambda: fake
    )

    # The worktree + execute_targets must not be called on the dry path;
    # patch with sentinels that raise if invoked.
    def _no_worktree(*a, **kw):
        raise AssertionError("worktree must not be created during dry-run")

    monkeypatch.setattr(
        "gitbulk.commands.dispatch.create_worktree", _no_worktree
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.execute_targets",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("execute_targets must not run during dry-run")
        ),
    )

    rc = dispatch_handler(
        _make_args(
            prompt=prompt_file,
            code_root=code_root,
            skip_check=_LOCAL_SKIPS,
        )
    )
    # --skip-check was applied → exit 4 (audit signal). The dry-run
    # still ran end-to-end; only the exit code reflects the override.
    assert rc == EXIT_OVERRIDES_APPLIED
    assert not sentinel.has_attention()
    latest = paths.latest_run_symlink("dispatch").resolve()
    summary = (latest / "summary.md").read_text()
    assert "DRY-RUN" in summary
    assert "Would dispatch" in summary
    assert "dhh1128/alpha" in summary
    assert "dhh1128/beta" in summary
    manifest = yaml.safe_load((latest / "manifest.yaml").read_text())
    assert manifest["config_snapshot"]["apply"] is False
    assert manifest["exit_code"] == EXIT_OVERRIDES_APPLIED


def test_dispatch_dry_run_no_eligible_prs_no_attention(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
    prompt_file, fake_clones,
):
    """Dry-run with zero eligible PRs still exits 0 and writes a summary."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake_clones("dhh1128/alpha")
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionGHClient", lambda: fake
    )
    rc = dispatch_handler(
        _make_args(
            prompt=prompt_file,
            code_root=code_root,
            skip_check=_LOCAL_SKIPS,
        )
    )
    assert rc == EXIT_OVERRIDES_APPLIED  # --skip-check applied
    latest = paths.latest_run_symlink("dispatch").resolve()
    summary = (latest / "summary.md").read_text()
    assert "no eligible PRs" in summary


# ─── --apply: happy path with 2 PRs succeeding ─────────────────────────────


def _stub_worktree_create(monkeypatch, *, paths_seen):
    """Patch worktree.create_worktree to return a tmp dir per call,
    record the args, and skip the real ``git worktree add`` shellout.
    """

    def _impl(
        repo_path,
        slug,
        pr_number,
        pr_head_ref,
        pr_head_sha,
        *,
        worktree_root,
        runid,
    ):
        target = (
            worktree_root
            / runid
            / slug.replace("/", "__")
            / f"pr{pr_number}"
        )
        target.mkdir(parents=True, exist_ok=True)
        paths_seen.append(target)
        return target

    monkeypatch.setattr(
        "gitbulk.commands.dispatch.create_worktree", _impl
    )


def _stub_worktree_remove(monkeypatch, *, removed):
    def _impl(repo_path, worktree_path):
        removed.append(worktree_path)

    monkeypatch.setattr(
        "gitbulk.commands.dispatch.remove_worktree", _impl
    )


def _stub_is_conflict(monkeypatch, *, conflicts_for_paths):
    """conflicts_for_paths is a set/list of paths considered in-conflict."""
    conflicts_set = set(map(str, conflicts_for_paths))

    def _impl(worktree_path):
        return str(worktree_path) in conflicts_set

    monkeypatch.setattr(
        "gitbulk.commands.dispatch.is_worktree_in_conflict", _impl
    )


def _stub_execute_targets(monkeypatch, *, results_by_key):
    """Patch execute_targets to return canned ExecResult per key."""

    def _impl(targets, *, claude, log_dir, concurrency, timeout_per_target):
        log_dir.mkdir(parents=True, exist_ok=True)
        out: list[ExecResult] = []
        now = datetime.now(timezone.utc)
        for t in targets:
            cfg = results_by_key.get(
                t.key,
                {"status": "completed", "exit_code": 0},
            )
            stdout_path = log_dir / f"{t.key}.stdout.log"
            stderr_path = log_dir / f"{t.key}.stderr.log"
            stdout_path.write_text(cfg.get("stdout", ""))
            stderr_path.write_text("")
            # Simulate the agent writing ESCALATION.md into its worktree.
            if "escalation" in cfg:
                (t.working_directory / "ESCALATION.md").write_text(
                    cfg["escalation"]
                )
            out.append(
                ExecResult(
                    key=t.key,
                    status=cfg["status"],
                    exit_code=cfg.get("exit_code"),
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    started_at=now,
                    finished_at=now,
                    duration_seconds=0.0,
                )
            )
        return out

    monkeypatch.setattr(
        "gitbulk.commands.dispatch.execute_targets", _impl
    )


def test_dispatch_apply_happy_path_two_prs(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
    prompt_file, fake_clones,
):
    write_config(repos_slugs=["dhh1128/alpha", "dhh1128/beta"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake_clones("dhh1128/alpha")
    fake_clones("dhh1128/beta")
    pr1 = _make_pr(slug="dhh1128/alpha", number=1)
    pr2 = _make_pr(slug="dhh1128/beta", number=2)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={
            "dhh1128/alpha": "main",
            "dhh1128/beta": "main",
        },
        my_open_prs={
            "dhh1128/alpha": [pr1],
            "dhh1128/beta": [pr2],
        },
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionGHClient", lambda: fake
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionClaudeClient",
        lambda: object(),  # never called because execute_targets is stubbed
    )

    paths_seen: list[Path] = []
    removed: list[Path] = []
    _stub_worktree_create(monkeypatch, paths_seen=paths_seen)
    _stub_worktree_remove(monkeypatch, removed=removed)
    _stub_is_conflict(monkeypatch, conflicts_for_paths=[])
    _stub_execute_targets(monkeypatch, results_by_key={})

    rc = dispatch_handler(
        _make_args(
            prompt=prompt_file,
            apply=True,
            code_root=code_root,
            skip_check=_LOCAL_SKIPS,
        )
    )
    # --skip-check applied, no failures → exit 4 (audit signal)
    assert rc == EXIT_OVERRIDES_APPLIED
    assert not sentinel.has_attention()
    # Two worktrees were created, two were torn down.
    assert len(paths_seen) == 2
    assert set(removed) == set(paths_seen)
    latest = paths.latest_run_symlink("dispatch").resolve()
    state = yaml.safe_load((latest / "state.yaml").read_text())
    # state.yaml records per-repo PR dispositions.
    assert set(state["repos"].keys()) == {"dhh1128/alpha", "dhh1128/beta"}
    for slug in state["repos"]:
        prs = state["repos"][slug]["prs"]
        assert len(prs) == 1
        assert prs[0]["status"] == "completed"
        assert prs[0]["worktree_preserved"] is False


# ─── --apply: 1 PR claude-fails → exit 2 ───────────────────────────────────


def test_dispatch_apply_one_failure_attention(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
    prompt_file, fake_clones,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake_clones("dhh1128/alpha")
    pr = _make_pr(slug="dhh1128/alpha", number=42)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionGHClient", lambda: fake
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionClaudeClient", lambda: object()
    )

    paths_seen: list[Path] = []
    removed: list[Path] = []
    _stub_worktree_create(monkeypatch, paths_seen=paths_seen)
    _stub_worktree_remove(monkeypatch, removed=removed)
    _stub_is_conflict(monkeypatch, conflicts_for_paths=[])
    _stub_execute_targets(
        monkeypatch,
        results_by_key={
            _key_for_pr("dhh1128/alpha", 42): {
                "status": "failed",
                "exit_code": 1,
            }
        },
    )

    rc = dispatch_handler(
        _make_args(
            prompt=prompt_file,
            apply=True,
            code_root=code_root,
            skip_check=_LOCAL_SKIPS,
        )
    )
    assert rc == EXIT_ATTENTION_NEEDED
    parsed = sentinel.parse_attention()
    assert parsed is not None
    assert parsed["exit_code"] == EXIT_ATTENTION_NEEDED
    assert parsed["subcommand"] == "dispatch"
    # Worktree was torn down (failure without conflict markers).
    assert set(removed) == set(paths_seen)


# ─── --apply: timeout without conflict → torn down ─────────────────────────


def test_dispatch_apply_timeout_no_conflict_tears_down(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
    prompt_file, fake_clones,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake_clones("dhh1128/alpha")
    pr = _make_pr(slug="dhh1128/alpha", number=7)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionGHClient", lambda: fake
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionClaudeClient", lambda: object()
    )

    paths_seen: list[Path] = []
    removed: list[Path] = []
    _stub_worktree_create(monkeypatch, paths_seen=paths_seen)
    _stub_worktree_remove(monkeypatch, removed=removed)
    _stub_is_conflict(monkeypatch, conflicts_for_paths=[])
    _stub_execute_targets(
        monkeypatch,
        results_by_key={
            _key_for_pr("dhh1128/alpha", 7): {
                "status": "timed-out",
                "exit_code": None,
            }
        },
    )

    rc = dispatch_handler(
        _make_args(
            prompt=prompt_file,
            apply=True,
            code_root=code_root,
            skip_check=_LOCAL_SKIPS,
        )
    )
    assert rc == EXIT_ATTENTION_NEEDED
    assert set(removed) == set(paths_seen)  # torn down


# ─── --apply: worktree in conflict → preserved + CONFLICT.md ───────────────


def test_dispatch_apply_conflict_preserved_with_marker(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
    prompt_file, fake_clones,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake_clones("dhh1128/alpha")
    pr = _make_pr(slug="dhh1128/alpha", number=99, title="conflict pr")
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionGHClient", lambda: fake
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionClaudeClient", lambda: object()
    )

    paths_seen: list[Path] = []
    removed: list[Path] = []
    _stub_worktree_create(monkeypatch, paths_seen=paths_seen)
    _stub_worktree_remove(monkeypatch, removed=removed)

    # We want THIS worktree to be classified as in-conflict; we need to
    # know the path before the kernel runs, but the path is built inside
    # the handler. The stub puts every created worktree in paths_seen;
    # we patch is_worktree_in_conflict to return True for all of them.
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.is_worktree_in_conflict",
        lambda p: True,
    )
    _stub_execute_targets(
        monkeypatch,
        results_by_key={
            _key_for_pr("dhh1128/alpha", 99): {
                "status": "completed",
                "exit_code": 0,
            }
        },
    )

    rc = dispatch_handler(
        _make_args(
            prompt=prompt_file,
            apply=True,
            code_root=code_root,
            skip_check=_LOCAL_SKIPS,
        )
    )
    # The exec was "completed" so no failed/timed-out → not attention by
    # exec status. _attention_results only counts failed/timed-out/
    # interrupted; --skip-check WAS applied so exit 4 (override audit).
    assert rc == EXIT_OVERRIDES_APPLIED
    # Worktree NOT torn down.
    assert removed == []
    # CONFLICT.md exists at the worktree path.
    assert len(paths_seen) == 1
    marker = paths_seen[0] / "CONFLICT.md"
    assert marker.exists()
    text = marker.read_text()
    assert "dhh1128/alpha" in text
    assert "#99" in text
    # State.yaml records the preservation.
    latest = paths.latest_run_symlink("dispatch").resolve()
    state = yaml.safe_load((latest / "state.yaml").read_text())
    pr_state = state["repos"]["dhh1128/alpha"]["prs"][0]
    assert pr_state["worktree_preserved"] is True
    assert pr_state["in_conflict"] is True


# ─── --apply: worktree teardown failure preserves ──────────────────────────


def test_dispatch_apply_teardown_error_preserves(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
    prompt_file, fake_clones,
):
    """If remove_worktree raises, treat as preserve+warn (don't crash)."""
    from gitbulk.worktree import WorktreeError

    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake_clones("dhh1128/alpha")
    pr = _make_pr(slug="dhh1128/alpha", number=5)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionGHClient", lambda: fake
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionClaudeClient", lambda: object()
    )

    paths_seen: list[Path] = []
    _stub_worktree_create(monkeypatch, paths_seen=paths_seen)
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.remove_worktree",
        lambda repo, p: (_ for _ in ()).throw(
            WorktreeError("simulated teardown failure", stderr="boom")
        ),
    )
    _stub_is_conflict(monkeypatch, conflicts_for_paths=[])
    _stub_execute_targets(
        monkeypatch,
        results_by_key={
            _key_for_pr("dhh1128/alpha", 5): {
                "status": "completed",
                "exit_code": 0,
            }
        },
    )

    rc = dispatch_handler(
        _make_args(
            prompt=prompt_file,
            apply=True,
            code_root=code_root,
            skip_check=_LOCAL_SKIPS,
        )
    )
    assert rc == EXIT_OVERRIDES_APPLIED  # --skip-check applied
    latest = paths.latest_run_symlink("dispatch").resolve()
    state = yaml.safe_load((latest / "state.yaml").read_text())
    pr_state = state["repos"]["dhh1128/alpha"]["prs"][0]
    assert pr_state["worktree_preserved"] is True
    # Teardown failure logged.
    errors = [
        json.loads(line)
        for line in (latest / "errors.log").read_text().splitlines()
        if line.strip()
    ]
    assert any("teardown failed" in e["message"] for e in errors)


# ─── --apply: worktree creation failure for one PR ─────────────────────────


def test_dispatch_apply_worktree_create_error_skips_pr(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
    prompt_file, fake_clones,
):
    """If create_worktree raises for one PR, the other PRs still run."""
    from gitbulk.worktree import WorktreeError

    write_config(repos_slugs=["dhh1128/alpha", "dhh1128/beta"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake_clones("dhh1128/alpha")
    fake_clones("dhh1128/beta")
    pr_a = _make_pr(slug="dhh1128/alpha", number=1)
    pr_b = _make_pr(slug="dhh1128/beta", number=2)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={
            "dhh1128/alpha": "main",
            "dhh1128/beta": "main",
        },
        my_open_prs={
            "dhh1128/alpha": [pr_a],
            "dhh1128/beta": [pr_b],
        },
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionGHClient", lambda: fake
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionClaudeClient", lambda: object()
    )

    paths_seen: list[Path] = []
    removed: list[Path] = []

    def _impl(
        repo_path, slug, pr_number, pr_head_ref, pr_head_sha, *,
        worktree_root, runid,
    ):
        if slug == "dhh1128/alpha":
            raise WorktreeError("simulated wt create error", stderr="oops")
        target = worktree_root / runid / slug.replace("/", "__") / f"pr{pr_number}"
        target.mkdir(parents=True, exist_ok=True)
        paths_seen.append(target)
        return target

    monkeypatch.setattr("gitbulk.commands.dispatch.create_worktree", _impl)
    _stub_worktree_remove(monkeypatch, removed=removed)
    _stub_is_conflict(monkeypatch, conflicts_for_paths=[])
    _stub_execute_targets(monkeypatch, results_by_key={})

    rc = dispatch_handler(
        _make_args(
            prompt=prompt_file,
            apply=True,
            code_root=code_root,
            skip_check=_LOCAL_SKIPS,
        )
    )
    assert rc == EXIT_OVERRIDES_APPLIED  # --skip-check applied
    # Only the beta worktree was created.
    assert len(paths_seen) == 1
    latest = paths.latest_run_symlink("dispatch").resolve()
    errors = [
        json.loads(line)
        for line in (latest / "errors.log").read_text().splitlines()
        if line.strip()
    ]
    assert any("worktree creation failed" in e["message"] for e in errors)


# ─── Universal invariant Fail (gh.authenticated) → exit 1 ──────────────────


def test_dispatch_gh_not_authenticated_exit_1(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
    prompt_file,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake = FakeGHClient()  # no user → authenticated_user raises
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionGHClient", lambda: fake
    )
    rc = dispatch_handler(
        _make_args(prompt=prompt_file, code_root=code_root)
    )
    assert rc == EXIT_STRUCTURAL_FAILURE
    latest = paths.latest_run_symlink("dispatch").resolve()
    summary = (latest / "summary.md").read_text()
    assert "FAILED" in summary


# ─── Per-repo Fail → exit 1 ────────────────────────────────────────────────


def test_dispatch_per_repo_fail_aborts(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
    prompt_file, fake_clones,
):
    from gitbulk.invariants import catalog as _catalog
    from gitbulk.invariants.base import Fail as _Fail

    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake_clones("dhh1128/alpha")
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionGHClient", lambda: fake
    )
    monkeypatch.setattr(
        _catalog.GithubReachableInvariant,
        "check",
        lambda self, ctx: _Fail("forced fail"),
    )
    rc = dispatch_handler(
        _make_args(
            prompt=prompt_file,
            code_root=code_root,
            skip_check=_LOCAL_SKIPS,
        )
    )
    assert rc == EXIT_STRUCTURAL_FAILURE


# ─── Per-repo Skip drops the repo; others continue → exit 3 ────────────────


def test_dispatch_per_repo_skip_drops_repo_exit_3(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
    prompt_file, fake_clones,
):
    """Beta is unreachable (github.reachable Skip) → exit 3 in dry-run
    mode (no eligible PRs to dispatch from alpha)."""
    write_config(repos_slugs=["dhh1128/alpha", "dhh1128/beta"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake_clones("dhh1128/alpha")
    fake_clones("dhh1128/beta")
    # default_branches missing beta → github.reachable Skip
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionGHClient", lambda: fake
    )
    rc = dispatch_handler(
        _make_args(
            prompt=prompt_file,
            code_root=code_root,
            skip_check=_LOCAL_SKIPS,
        )
    )
    assert rc == EXIT_INVARIANT_SKIPPED
    parsed = sentinel.parse_attention()
    assert parsed is not None
    assert parsed["exit_code"] == EXIT_INVARIANT_SKIPPED
    latest = paths.latest_run_symlink("dispatch").resolve()
    summary = (latest / "summary.md").read_text()
    assert "Skipped repos" in summary
    assert "dhh1128/beta" in summary


# ─── --skip-check WITHOUT any other concern → exit 4 ───────────────────────


def test_dispatch_skip_check_applied_exit_4(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
    prompt_file, fake_clones,
):
    """--skip-check used and nothing else fires → exit 4 (audit signal)."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake_clones("dhh1128/alpha")
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},  # nothing to dispatch
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionGHClient", lambda: fake
    )
    rc = dispatch_handler(
        _make_args(
            prompt=prompt_file,
            code_root=code_root,
            skip_check=_LOCAL_SKIPS,
        )
    )
    assert rc == EXIT_OVERRIDES_APPLIED
    assert not sentinel.has_attention()
    latest = paths.latest_run_symlink("dispatch").resolve()
    errors = (latest / "errors.log").read_text().splitlines()
    assert any(
        json.loads(line).get("level") == "WARNING" for line in errors
    )


# ─── Lock timeout → exit 1, no sentinel ────────────────────────────────────


def test_dispatch_lock_timeout_exits_1(
    monkeypatch, isolated_xdg, code_root, write_config, capsys,
    fresh_org_cache, prompt_file,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])

    class _BoomLock:
        def __enter__(self):
            raise LockTimeoutError(
                paths.global_lock_file(),
                {
                    "pid": 999,
                    "started_at": "1970-01-01T00:00:00+00:00",
                    "subcommand": "merge",
                    "alive": False,
                },
            )

        def __exit__(self, *a):  # pragma: no cover — never reached
            return False

    monkeypatch.setattr(
        "gitbulk.commands.dispatch.global_lock", lambda *a, **kw: _BoomLock()
    )
    fake = FakeGHClient(user={"login": "dhh1128"})
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionGHClient", lambda: fake
    )
    rc = dispatch_handler(
        _make_args(prompt=prompt_file, code_root=code_root)
    )
    assert rc == EXIT_STRUCTURAL_FAILURE
    assert not sentinel.has_attention()
    err = capsys.readouterr().err
    assert "timed out" in err


# ─── gh.my_open_prs raises → exit 1 ────────────────────────────────────────


def test_dispatch_gh_pr_fetch_error_exit_1(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
    prompt_file, fake_clones,
):
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake_clones("dhh1128/alpha")
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        # my_open_prs NOT configured → raises GHError on the call
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionGHClient", lambda: fake
    )
    rc = dispatch_handler(
        _make_args(
            prompt=prompt_file,
            code_root=code_root,
            skip_check=_LOCAL_SKIPS,
        )
    )
    assert rc == EXIT_STRUCTURAL_FAILURE


# ─── CLI smoke (through main()) ────────────────────────────────────────────


def test_dispatch_through_main_dry_run_no_args_is_dry_run(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
    prompt_file, fake_clones,
):
    """Mutating subcommand must default to dry-run when --apply absent."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake_clones("dhh1128/alpha")
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionGHClient", lambda: fake
    )

    rc = main(
        [
            "dispatch",
            "--prompt", str(prompt_file),
            "--code-root", str(code_root),
            "--skip-check", "local.exists",
            "--skip-check", "local.remote_matches",
            "--skip-check", "local.default_branch_in_sync",
        ]
    )
    assert rc == EXIT_OVERRIDES_APPLIED
    latest = paths.latest_run_symlink("dispatch").resolve()
    manifest = yaml.safe_load((latest / "manifest.yaml").read_text())
    assert manifest["config_snapshot"]["apply"] is False


# ─── Helper-function unit tests ────────────────────────────────────────────


def test_runid_from_run_dir_simple(tmp_path):
    d = tmp_path / "20260528T010203Z-dispatch"
    assert _runid_from_run_dir(d) == "20260528T010203Z"


def test_runid_from_run_dir_fallback(tmp_path):
    """If the dir doesn't end in -dispatch, the rpartition fallback is used."""
    d = tmp_path / "20260528T010203Z-something-else"
    # rpartition on the last hyphen → "20260528T010203Z-something"
    assert _runid_from_run_dir(d) == "20260528T010203Z-something"


# ─── Gap 1: agent outcome parsing ──────────────────────────────────────────


def test_parse_agent_outcome_resolved(tmp_path):
    p = tmp_path / "o.log"
    p.write_text("rebasing...\nRESOLVED: union-merged poetry.lock\n")
    assert _parse_agent_outcome(p) == (
        "RESOLVED",
        "RESOLVED: union-merged poetry.lock",
    )


def test_parse_agent_outcome_escalated_strips_backticks(tmp_path):
    p = tmp_path / "o.log"
    p.write_text("`ESCALATED: add-add in README.md; see ESCALATION.md`\n")
    assert _parse_agent_outcome(p) == (
        "ESCALATED",
        "ESCALATED: add-add in README.md; see ESCALATION.md",
    )


def test_parse_agent_outcome_no_detail(tmp_path):
    p = tmp_path / "o.log"
    p.write_text("ESCALATED:\n")
    assert _parse_agent_outcome(p) == ("ESCALATED", "ESCALATED")


def test_parse_agent_outcome_last_match_wins(tmp_path):
    p = tmp_path / "o.log"
    p.write_text("RESOLVED: early guess\nmore work\nESCALATED: final word\n")
    assert _parse_agent_outcome(p) == ("ESCALATED", "ESCALATED: final word")


def test_parse_agent_outcome_no_status_line(tmp_path):
    p = tmp_path / "o.log"
    p.write_text("just some logs\nno verdict here\n")
    assert _parse_agent_outcome(p) == (None, None)


def test_parse_agent_outcome_missing_file(tmp_path):
    assert _parse_agent_outcome(tmp_path / "nope.log") == (None, None)


# ─── Gap 2: ESCALATION.md salvage ──────────────────────────────────────────


def test_salvage_escalation_present(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "ESCALATION.md").write_text("why it is not mechanical\n")
    dest = tmp_path / "run" / "escalations"
    out = _salvage_escalation(wt, dest, "x__a__pr3")
    assert out == str(dest / "x__a__pr3.md")
    assert (dest / "x__a__pr3.md").read_text() == "why it is not mechanical\n"


def test_salvage_escalation_absent_returns_none(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    assert _salvage_escalation(wt, tmp_path / "run" / "esc", "k") is None


def test_salvage_escalation_copy_failure_returns_none(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "ESCALATION.md").write_text("x")
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a dir")
    # dest_dir lives *under* a regular file → mkdir(parents=True) raises OSError.
    assert _salvage_escalation(wt, blocker / "esc", "k") is None


# ─── Gap 1+2 end-to-end: outcome surfaced + escalation salvaged ────────────


def _one_pr_apply_run(monkeypatch, code_root, write_config, fresh_org_cache,
                      prompt_file, fake_clones, *, cfg, conflict=False):
    """Drive a single-PR --apply run with a stubbed agent result `cfg`."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake_clones("dhh1128/alpha")
    pr = _make_pr(slug="dhh1128/alpha", number=7)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionGHClient", lambda: fake
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionClaudeClient", lambda: object()
    )
    paths_seen: list = []
    removed: list = []
    _stub_worktree_create(monkeypatch, paths_seen=paths_seen)
    _stub_worktree_remove(monkeypatch, removed=removed)
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.is_worktree_in_conflict",
        lambda p: conflict,
    )
    _stub_execute_targets(
        monkeypatch, results_by_key={_key_for_pr("dhh1128/alpha", 7): cfg}
    )
    rc = dispatch_handler(
        _make_args(prompt=prompt_file, apply=True, code_root=code_root,
                   skip_check=_LOCAL_SKIPS)
    )
    latest = paths.latest_run_symlink("dispatch").resolve()
    return rc, latest, removed


def test_dispatch_apply_surfaces_escalated_and_salvages_note(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
    prompt_file, fake_clones,
):
    """A clean escalation: verdict shown in summary/state, ESCALATION.md
    salvaged into the run dir, and the (non-conflict) worktree torn down."""
    cfg = {
        "status": "completed",
        "exit_code": 0,
        "stdout": "`ESCALATED: add-add conflict in README.md; see ESCALATION.md`\n",
        "escalation": "# Escalation\nPR head vs base both added CI badges.\n",
    }
    rc, latest, removed = _one_pr_apply_run(
        monkeypatch, code_root, write_config, fresh_org_cache, prompt_file,
        fake_clones, cfg=cfg, conflict=False,
    )
    # summary.md surfaces the verdict + tally (not just "completed").
    summary = (latest / "summary.md").read_text()
    assert "ESCALATED: add-add conflict in README.md" in summary
    assert "Escalated: 1" in summary
    # state.yaml records the parsed outcome + salvaged path.
    state = yaml.safe_load((latest / "state.yaml").read_text())
    pr_state = state["repos"]["dhh1128/alpha"]["prs"][0]
    assert pr_state["outcome"] == "ESCALATED"
    assert pr_state["outcome_detail"].startswith("ESCALATED: add-add")
    assert pr_state["escalation_file"]
    # The ESCALATION.md was salvaged into the durable run dir...
    saved = latest / "escalations" / "dhh1128__alpha__pr7.md"
    assert saved.is_file()
    assert "added CI badges" in saved.read_text()
    # ...and the clean (non-conflict) worktree was torn down.
    assert len(removed) == 1
    assert pr_state["worktree_preserved"] is False


def test_dispatch_apply_surfaces_resolved_no_escalation_file(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
    prompt_file, fake_clones,
):
    """A RESOLVED run: verdict surfaced, no escalation artifact."""
    cfg = {
        "status": "completed",
        "exit_code": 0,
        "stdout": "rebased clean\nRESOLVED: union-merged poetry.lock + CHANGELOG\n",
    }
    rc, latest, removed = _one_pr_apply_run(
        monkeypatch, code_root, write_config, fresh_org_cache, prompt_file,
        fake_clones, cfg=cfg, conflict=False,
    )
    summary = (latest / "summary.md").read_text()
    assert "RESOLVED: union-merged poetry.lock" in summary
    assert "Resolved: 1" in summary
    state = yaml.safe_load((latest / "state.yaml").read_text())
    pr_state = state["repos"]["dhh1128/alpha"]["prs"][0]
    assert pr_state["outcome"] == "RESOLVED"
    assert pr_state["escalation_file"] is None
    assert not (latest / "escalations").exists()


def test_key_for_pr_shape():
    assert _key_for_pr("dhh1128/gitbulk", 42) == "dhh1128__gitbulk__pr42"


def test_build_summary_md_apply_with_results(isolated_xdg, code_root, write_config):
    """Apply-mode summary includes per-PR statuses with exit codes."""
    from gitbulk.config.policy import load_policy
    from gitbulk.config.repos import RepoEntry

    write_config(repos_slugs=["x/a"])
    policy = load_policy()
    repo = RepoEntry(
        slug="x/a", owner="x", name="a", local_path=code_root / "a",
        source_line=1,
    )
    pr = _make_pr(slug="x/a", number=3)
    now = datetime.now(timezone.utc)
    result = ExecResult(
        key=_key_for_pr("x/a", 3),
        status="completed",
        exit_code=0,
        stdout_path=code_root / "out.log",
        stderr_path=code_root / "err.log",
        started_at=now,
        finished_at=now,
        duration_seconds=1.0,
    )
    md = _build_summary_md(
        policy,
        all_repos=[repo],
        passing_repos=[repo],
        skipped_repos=[],
        eligible_prs=[("x/a", pr)],
        results=[result],
        apply=True,
        prompt_path=Path("/tmp/p.md"),
    )
    assert "APPLY" in md
    assert "Dispatch results" in md
    assert "completed" in md
    assert "exit 0" in md


def test_build_summary_md_apply_missing_result(isolated_xdg, code_root, write_config):
    """If an eligible PR has no matching result (e.g. worktree creation
    failed), the summary records it explicitly."""
    from gitbulk.config.policy import load_policy
    from gitbulk.config.repos import RepoEntry

    write_config(repos_slugs=["x/a"])
    policy = load_policy()
    repo = RepoEntry(
        slug="x/a", owner="x", name="a", local_path=code_root / "a",
        source_line=1,
    )
    pr = _make_pr(slug="x/a", number=9)
    md = _build_summary_md(
        policy,
        all_repos=[repo],
        passing_repos=[repo],
        skipped_repos=[],
        eligible_prs=[("x/a", pr)],
        results=[],
        apply=True,
        prompt_path=Path("/tmp/p.md"),
    )
    assert "no result recorded" in md


# ─── Skipped repos + --apply: exit code priority (attention > skip) ────────


def test_dispatch_apply_attention_beats_skip(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
    prompt_file, fake_clones,
):
    """When one repo is skipped AND another PR fails on apply, exit 2
    (attention) wins over exit 3 (skip)."""
    write_config(repos_slugs=["dhh1128/alpha", "dhh1128/beta"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake_clones("dhh1128/alpha")
    fake_clones("dhh1128/beta")
    pr = _make_pr(slug="dhh1128/alpha", number=1)
    # Beta missing from default_branches → github.reachable Skip.
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionGHClient", lambda: fake
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionClaudeClient", lambda: object()
    )

    paths_seen: list[Path] = []
    removed: list[Path] = []
    _stub_worktree_create(monkeypatch, paths_seen=paths_seen)
    _stub_worktree_remove(monkeypatch, removed=removed)
    _stub_is_conflict(monkeypatch, conflicts_for_paths=[])
    _stub_execute_targets(
        monkeypatch,
        results_by_key={
            _key_for_pr("dhh1128/alpha", 1): {
                "status": "failed",
                "exit_code": 2,
            }
        },
    )

    rc = dispatch_handler(
        _make_args(
            prompt=prompt_file,
            apply=True,
            code_root=code_root,
            skip_check=_LOCAL_SKIPS,
        )
    )
    assert rc == EXIT_ATTENTION_NEEDED


# ─── Real-clone tests: exercise the no-skip-check branches ────────────────


def test_dispatch_dry_run_no_skip_check_clean_exit_ok(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
    prompt_file, fake_clones,
):
    """Dry-run with a REAL local clone and no --skip-check → exit 0
    (exercises the EXIT_OK branch in the dry-run exit-code ladder)."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake_clones("dhh1128/alpha", init=True, default_branch="main")
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionGHClient", lambda: fake
    )
    rc = dispatch_handler(
        _make_args(prompt=prompt_file, code_root=code_root)
    )
    assert rc == EXIT_OK
    assert not sentinel.has_attention()


def test_dispatch_apply_no_skip_check_clean_exit_ok(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
    prompt_file, fake_clones,
):
    """Apply with REAL local clone, no --skip-check, succeeded run →
    exit 0 (exercises the apply-path EXIT_OK branch + state recording
    with worktree teardown)."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake_clones("dhh1128/alpha", init=True, default_branch="main")
    pr = _make_pr(slug="dhh1128/alpha", number=11)
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionGHClient", lambda: fake
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionClaudeClient", lambda: object()
    )

    paths_seen: list[Path] = []
    removed: list[Path] = []
    _stub_worktree_create(monkeypatch, paths_seen=paths_seen)
    _stub_worktree_remove(monkeypatch, removed=removed)
    _stub_is_conflict(monkeypatch, conflicts_for_paths=[])
    _stub_execute_targets(monkeypatch, results_by_key={})

    rc = dispatch_handler(
        _make_args(prompt=prompt_file, apply=True, code_root=code_root)
    )
    assert rc == EXIT_OK
    assert not sentinel.has_attention()
    assert set(removed) == set(paths_seen)


def test_dispatch_apply_with_skipped_repo_no_failures_exit_3(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
    prompt_file, fake_clones,
):
    """Apply path: one repo skipped, other repo's PR completed → exit 3
    (skip wins because no failures)."""
    write_config(repos_slugs=["dhh1128/alpha", "dhh1128/beta"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake_clones("dhh1128/alpha", init=True)
    fake_clones("dhh1128/beta", init=True)
    pr = _make_pr(slug="dhh1128/alpha", number=21)
    # default_branches missing beta → github.reachable Skip
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionGHClient", lambda: fake
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionClaudeClient", lambda: object()
    )

    paths_seen: list[Path] = []
    removed: list[Path] = []
    _stub_worktree_create(monkeypatch, paths_seen=paths_seen)
    _stub_worktree_remove(monkeypatch, removed=removed)
    _stub_is_conflict(monkeypatch, conflicts_for_paths=[])
    _stub_execute_targets(monkeypatch, results_by_key={})

    rc = dispatch_handler(
        _make_args(prompt=prompt_file, apply=True, code_root=code_root)
    )
    assert rc == EXIT_INVARIANT_SKIPPED
    assert sentinel.has_attention()


def test_dispatch_dry_run_pr_with_non_default_base_not_eligible(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
    prompt_file, fake_clones,
):
    """A PR targeting a non-default base is Skipped by pr.base_is_default
    and therefore NOT in the eligible list (exercises the
    ``not eligible`` branch of the PER_PR filter loop)."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake_clones("dhh1128/alpha", init=True, default_branch="main")
    pr = _make_pr(slug="dhh1128/alpha", number=33, base_ref="develop")
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": [pr]},
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionGHClient", lambda: fake
    )
    rc = dispatch_handler(
        _make_args(prompt=prompt_file, code_root=code_root)
    )
    assert rc == EXIT_OK
    latest = paths.latest_run_symlink("dispatch").resolve()
    summary = (latest / "summary.md").read_text()
    # PR did not enter the "Would dispatch" list.
    assert "no eligible PRs" in summary


def test_dispatch_dry_run_all_repos_skipped_no_pr_fetch(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
    prompt_file, fake_clones,
):
    """When EVERY repo is skipped, ``passing_repos`` is empty and the
    pipeline skips the gh.my_open_prs call (exercising the
    ``prs_by_repo = {}`` branch)."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake_clones("dhh1128/alpha", init=True)
    # default_branches missing alpha → github.reachable Skip → no
    # passing repos → no my_open_prs call.
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={},  # alpha not present
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionGHClient", lambda: fake
    )
    rc = dispatch_handler(
        _make_args(prompt=prompt_file, code_root=code_root)
    )
    assert rc == EXIT_INVARIANT_SKIPPED
    # my_open_prs was NEVER called (we skipped the gh fetch entirely).
    assert fake.call_count["my_open_prs"] == 0


# ─── Apply with no eligible PRs (skip_check used) → exit 4 ─────────────────


def test_dispatch_apply_no_eligible_and_skip_check_exit_4(
    monkeypatch, isolated_xdg, code_root, write_config, fresh_org_cache,
    prompt_file, fake_clones,
):
    """Apply path with --skip-check applied and no eligible PRs → exit 4."""
    write_config(repos_slugs=["dhh1128/alpha"])
    fresh_org_cache("provenant-dev", ["dhh1128"])
    fake_clones("dhh1128/alpha")
    fake = FakeGHClient(
        user={"login": "dhh1128"},
        org_members={"provenant-dev": ["dhh1128"]},
        default_branches={"dhh1128/alpha": "main"},
        my_open_prs={"dhh1128/alpha": []},
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionGHClient", lambda: fake
    )
    monkeypatch.setattr(
        "gitbulk.commands.dispatch.ProductionClaudeClient", lambda: object()
    )
    _stub_worktree_create(monkeypatch, paths_seen=[])
    _stub_worktree_remove(monkeypatch, removed=[])
    _stub_is_conflict(monkeypatch, conflicts_for_paths=[])
    _stub_execute_targets(monkeypatch, results_by_key={})

    rc = dispatch_handler(
        _make_args(
            prompt=prompt_file,
            apply=True,
            code_root=code_root,
            skip_check=_LOCAL_SKIPS,
        )
    )
    assert rc == EXIT_OVERRIDES_APPLIED
