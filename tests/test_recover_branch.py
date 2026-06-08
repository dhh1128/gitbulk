"""End-to-end tests for ``gitbulk recover-branch`` (tick 6lui).

A "pruned branch" is fabricated as a ``disposition: deleted`` row in a
prune-branches ``state.yaml`` written under the isolated XDG cache — no real
prune run, no real deletion. The single GitHub mutation goes through
:class:`FakeGHClient`, so the whole recovery path runs offline.
"""

from __future__ import annotations

import argparse

import pytest
import yaml

from gitbulk import paths
from gitbulk.commands import recover_branch as rb
from gitbulk.commands.recover_branch import (
    EXIT_ATTENTION_NEEDED,
    EXIT_OK,
    EXIT_STRUCTURAL_FAILURE,
    recover_branch_handler,
)
from gitbulk.gh import FakeGHClient, GHError


# isolated_xdg lives in tests/conftest.py.


def _write_prune_run(runid="20260606T030000Z", *, repos=None, latest=True):
    """Create a prune-branches run dir with a state.yaml and (optionally) the
    latest- symlink, returning its run dir."""
    if repos is None:
        repos = {
            "o/alpha": {
                "default_branch": "main",
                "branches": [
                    {"branch": "feat-a", "sha": "a" * 40, "disposition": "deleted",
                     "pr_number": 11, "reason": "PR #11 merged"},
                    {"branch": "keep", "sha": "c" * 40, "disposition": "kept"},
                ],
            },
            "o/beta": {
                "default_branch": "main",
                "branches": [
                    {"branch": "feat-b", "sha": "b" * 40, "disposition": "deleted"},
                ],
            },
        }
    run_dir = paths.run_dir(runid, "prune-branches")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.yaml").write_text(
        yaml.safe_dump({"schema_version": 1, "repos": repos})
    )
    if latest:
        symlink = paths.latest_run_symlink("prune-branches")
        symlink.symlink_to(run_dir)
    return run_dir


def _args(**kw):
    ns = argparse.Namespace(slug=None, branch=None, run=None, apply=False)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


@pytest.fixture
def fake_gh(monkeypatch):
    """Inject a FakeGHClient in place of ProductionGHClient and return it."""
    fake = FakeGHClient()

    def _install(f):
        monkeypatch.setattr(
            "gitbulk.commands.recover_branch.ProductionGHClient", lambda: f
        )

    _install(fake)
    fake._reinstall = _install  # let a test swap in a configured client
    return fake


# ─── dry-run ───────────────────────────────────────────────────────────────


def test_dry_run_lists_without_mutating(isolated_xdg, fake_gh, capsys):
    _write_prune_run()
    rc = recover_branch_handler(_args())
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    assert "Would recover 2 branch(es)" in out
    assert "o/alpha feat-a" in out and "o/beta feat-b" in out
    assert fake_gh.create_branch_calls == []  # dry-run touches nothing


def test_dry_run_excludes_kept_branches(isolated_xdg, fake_gh, capsys):
    _write_prune_run()
    recover_branch_handler(_args())
    assert "keep" not in capsys.readouterr().out


# ─── apply ─────────────────────────────────────────────────────────────────


def test_apply_recovers_all_deleted_branches(isolated_xdg, fake_gh, capsys):
    _write_prune_run()
    rc = recover_branch_handler(_args(apply=True))
    assert rc == EXIT_OK
    assert {(c["branch"], c["sha"]) for c in fake_gh.create_branch_calls} == {
        ("feat-a", "a" * 40),
        ("feat-b", "b" * 40),
    }
    out = capsys.readouterr().out
    assert "2 recovered, 0 already present, 0 failed." in out


def test_apply_writes_recover_run_audit_trail(isolated_xdg, fake_gh):
    _write_prune_run()
    recover_branch_handler(_args(apply=True))
    run_dir = paths.latest_run_symlink("recover-branch").resolve()
    state = yaml.safe_load((run_dir / "state.yaml").read_text())
    rows = state["repos"]["o/alpha"]["branches"]
    assert rows[0]["branch"] == "feat-a" and rows[0]["status"] == "recovered"
    assert (run_dir / "summary.md").is_file()


def test_apply_narrowed_to_one_slug(isolated_xdg, fake_gh, capsys):
    _write_prune_run()
    rc = recover_branch_handler(_args(slug="o/beta", apply=True))
    assert rc == EXIT_OK
    assert [c["branch"] for c in fake_gh.create_branch_calls] == ["feat-b"]


def test_apply_narrowed_to_one_branch(isolated_xdg, fake_gh, capsys):
    _write_prune_run()
    rc = recover_branch_handler(_args(slug="o/alpha", branch="feat-a", apply=True))
    assert rc == EXIT_OK
    assert [c["branch"] for c in fake_gh.create_branch_calls] == ["feat-a"]


def test_apply_already_present_is_not_recreated(isolated_xdg, fake_gh, capsys):
    _write_prune_run()
    fake = FakeGHClient(branch_ref_shas={("o/alpha", "feat-a"): "a" * 40})
    fake_gh._reinstall(fake)
    rc = recover_branch_handler(_args(slug="o/alpha", apply=True))
    assert rc == EXIT_OK
    assert fake.create_branch_calls == []
    assert "1 already present" in capsys.readouterr().out


def test_apply_partial_failure_returns_attention(isolated_xdg, fake_gh, capsys):
    _write_prune_run()
    fake = FakeGHClient(
        create_branch_responses={("o/beta", "feat-b"): GHError("api blew up")},
    )
    fake_gh._reinstall(fake)
    rc = recover_branch_handler(_args(apply=True))
    assert rc == EXIT_ATTENTION_NEEDED
    out = capsys.readouterr().out
    assert "1 recovered, 0 already present, 1 failed." in out
    # The failure is captured in errors.log for the audit trail.
    run_dir = paths.latest_run_symlink("recover-branch").resolve()
    errors = (run_dir / "errors.log").read_text()
    assert "feat-b" in errors and "api blew up" in errors


# ─── source-run resolution & edge cases ─────────────────────────────────────


def test_specific_run_id_is_honored(isolated_xdg, fake_gh, capsys):
    _write_prune_run(runid="20260601T010000Z", latest=False, repos={
        "o/gamma": {"branches": [
            {"branch": "old", "sha": "f" * 40, "disposition": "deleted"}]},
    })
    _write_prune_run()  # a newer latest run with different branches
    rc = recover_branch_handler(_args(run="20260601T010000Z", apply=True))
    assert rc == EXIT_OK
    assert [c["branch"] for c in fake_gh.create_branch_calls] == ["old"]


def test_missing_latest_run_is_structural_failure(isolated_xdg, fake_gh, capsys):
    rc = recover_branch_handler(_args())
    assert rc == EXIT_STRUCTURAL_FAILURE
    assert "no readable state.yaml" in capsys.readouterr().err


def test_unknown_run_id_is_structural_failure(isolated_xdg, fake_gh, capsys):
    _write_prune_run()
    rc = recover_branch_handler(_args(run="20990101T000000Z"))
    assert rc == EXIT_STRUCTURAL_FAILURE
    assert "no readable state.yaml" in capsys.readouterr().err


def test_no_deleted_branches_is_ok(isolated_xdg, fake_gh, capsys):
    _write_prune_run(repos={"o/alpha": {"branches": [
        {"branch": "keep", "sha": "c" * 40, "disposition": "kept"}]}})
    rc = recover_branch_handler(_args())
    assert rc == EXIT_OK
    assert "No deleted branches" in capsys.readouterr().out


def test_branch_without_slug_is_rejected(isolated_xdg, fake_gh, capsys):
    _write_prune_run()
    rc = recover_branch_handler(_args(branch="feat-a"))
    assert rc == EXIT_STRUCTURAL_FAILURE
    assert "branch requires a slug" in capsys.readouterr().err


def test_unreadable_state_yaml_yields_no_recoveries(isolated_xdg, fake_gh, capsys):
    run_dir = paths.run_dir("20260606T030000Z", "prune-branches")
    run_dir.mkdir(parents=True)
    (run_dir / "state.yaml").write_text(": : not valid yaml :")
    paths.latest_run_symlink("prune-branches").symlink_to(run_dir)
    rc = recover_branch_handler(_args())
    assert rc == EXIT_OK
    assert "No deleted branches" in capsys.readouterr().out


def test_non_mapping_state_yaml_yields_no_recoveries(isolated_xdg, fake_gh, capsys):
    # Valid YAML that parses to a list, not a mapping → defensively ignored.
    run_dir = paths.run_dir("20260606T030000Z", "prune-branches")
    run_dir.mkdir(parents=True)
    (run_dir / "state.yaml").write_text("- a\n- b\n")
    paths.latest_run_symlink("prune-branches").symlink_to(run_dir)
    rc = recover_branch_handler(_args())
    assert rc == EXIT_OK
    assert "No deleted branches" in capsys.readouterr().out
