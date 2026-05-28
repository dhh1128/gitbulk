"""Tests for ``gitbulk show`` (this.i nodes ``tp4kq2nr``, ``tmlk5pq3``,
``kp7nw4mq``).

The handler is purely read-only over the run-state layout written by
:mod:`gitbulk.runstate`. Tests construct the run directory + symlink by
hand (using :class:`RunState`) so they remain fully offline and
independent of the report/summarize/dispatch handlers.
"""

from __future__ import annotations

import argparse
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

import pytest

from gitbulk import paths
from gitbulk.cli import main
from gitbulk.commands.show import (
    EXIT_OK,
    EXIT_STRUCTURAL_FAILURE,
    show_handler,
)
from gitbulk.locks import LockTimeoutError
from gitbulk.runstate import RunState


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
def report_run(isolated_xdg):
    """Create a completed ``report`` run with all artifacts written and the
    ``latest-report`` symlink pointing at it. Returns the run dir."""
    rs = RunState.begin(
        "report",
        argv=["gitbulk", "report"],
        config_snapshot={"policy": {"defaults": {}}, "repos_txt": ""},
    )
    rs.record_invariant("gh.authenticated", "global", "PASS")
    rs.record_error("test warning", level="WARNING")
    rs.record_repo_state(
        "owner/alpha",
        {"pr_count": 1, "prs": [{"number": 1, "title": "x"}]},
    )
    rs.write_summary("# gitbulk report\n\nNothing to flag.\n")
    rs.complete(EXIT_OK)
    return rs.run_dir


def _make_args(**kwargs) -> argparse.Namespace:
    """Default to no subcommand arg + summary as the artifact."""
    defaults = {
        "show_subcommand": None,
        "state": False,
        "invariants": False,
        "errors": False,
        "manifest": False,
        "path": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ─── No subcommand → dashboard ─────────────────────────────────────────────


def test_show_no_arg_prints_dashboard_when_present(isolated_xdg):
    dash = paths.dashboard_file()
    dash.parent.mkdir(parents=True, exist_ok=True)
    dash.write_text("# DASH-CONTENT\n")
    out = StringIO()
    with redirect_stdout(out):
        rc = show_handler(_make_args())
    assert rc == EXIT_OK
    assert "DASH-CONTENT" in out.getvalue()


def test_show_no_arg_when_dashboard_missing_exits_zero_with_hint(isolated_xdg):
    # Dashboard not written yet — first-install state, not an error.
    err = StringIO()
    out = StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = show_handler(_make_args())
    assert rc == EXIT_OK
    assert out.getvalue() == ""
    assert "no dashboard yet" in err.getvalue().lower()


# ─── Subcommand artifact selection ─────────────────────────────────────────


def test_show_subcommand_default_prints_summary(report_run):
    out = StringIO()
    with redirect_stdout(out):
        rc = show_handler(_make_args(show_subcommand="report"))
    assert rc == EXIT_OK
    assert "gitbulk report" in out.getvalue()


def test_show_subcommand_state_prints_state_yaml(report_run):
    out = StringIO()
    with redirect_stdout(out):
        rc = show_handler(_make_args(show_subcommand="report", state=True))
    assert rc == EXIT_OK
    text = out.getvalue()
    assert "owner/alpha" in text
    assert "schema_version" in text


def test_show_subcommand_invariants_prints_invariants_log(report_run):
    out = StringIO()
    with redirect_stdout(out):
        rc = show_handler(
            _make_args(show_subcommand="report", invariants=True)
        )
    assert rc == EXIT_OK
    text = out.getvalue()
    assert "gh.authenticated" in text
    assert "PASS" in text


def test_show_subcommand_errors_prints_errors_log(report_run):
    out = StringIO()
    with redirect_stdout(out):
        rc = show_handler(_make_args(show_subcommand="report", errors=True))
    assert rc == EXIT_OK
    text = out.getvalue()
    assert "test warning" in text
    assert "WARNING" in text


def test_show_subcommand_manifest_prints_manifest_yaml(report_run):
    out = StringIO()
    with redirect_stdout(out):
        rc = show_handler(_make_args(show_subcommand="report", manifest=True))
    assert rc == EXIT_OK
    text = out.getvalue()
    assert "subcommand: report" in text
    assert "argv" in text
    assert "completed_at" in text


def test_show_subcommand_path_prints_run_dir(report_run):
    out = StringIO()
    with redirect_stdout(out):
        rc = show_handler(_make_args(show_subcommand="report", path=True))
    assert rc == EXIT_OK
    # Print just the path, one line, ending with the run dir name.
    assert str(report_run) in out.getvalue()
    assert out.getvalue().strip() == str(report_run)


# ─── Failure modes ─────────────────────────────────────────────────────────


def test_show_unknown_subcommand_exits_1(isolated_xdg):
    err = StringIO()
    with redirect_stderr(err):
        rc = show_handler(_make_args(show_subcommand="not-a-real-sub"))
    assert rc == EXIT_STRUCTURAL_FAILURE
    assert "unknown subcommand" in err.getvalue().lower()


def test_show_subcommand_without_prior_run_exits_1(isolated_xdg):
    err = StringIO()
    with redirect_stderr(err):
        rc = show_handler(_make_args(show_subcommand="report"))
    assert rc == EXIT_STRUCTURAL_FAILURE
    assert "no report runs yet" in err.getvalue().lower()


def test_show_subcommand_dangling_symlink_exits_1(isolated_xdg):
    """If latest-<sub> exists but its target is gone, treat as no run."""
    symlink = paths.latest_run_symlink("report")
    symlink.parent.mkdir(parents=True, exist_ok=True)
    target = paths.runs_dir() / "ghost-report"
    # Don't create target; just symlink to it.
    os.symlink(str(target), symlink)
    err = StringIO()
    with redirect_stderr(err):
        rc = show_handler(_make_args(show_subcommand="report"))
    assert rc == EXIT_STRUCTURAL_FAILURE
    assert "no report runs yet" in err.getvalue().lower()


def test_show_subcommand_with_run_but_missing_summary_exits_1(report_run):
    # Remove summary.md after the fixture wrote it.
    (report_run / "summary.md").unlink()
    err = StringIO()
    with redirect_stderr(err):
        rc = show_handler(_make_args(show_subcommand="report"))
    assert rc == EXIT_STRUCTURAL_FAILURE
    msg = err.getvalue().lower()
    assert "no summary.md" in msg
    assert "report" in msg


def test_show_subcommand_with_run_but_missing_invariants_log_exits_1(
    report_run,
):
    # invariants.log is optional (only written if anything is recorded);
    # we recorded one PASS in the fixture so it exists — delete it.
    (report_run / "invariants.log").unlink()
    err = StringIO()
    with redirect_stderr(err):
        rc = show_handler(
            _make_args(show_subcommand="report", invariants=True)
        )
    assert rc == EXIT_STRUCTURAL_FAILURE
    assert "no invariants.log" in err.getvalue().lower()


# ─── Mutually-exclusive flag group ─────────────────────────────────────────


def test_show_mutually_exclusive_flags_rejected_by_argparse():
    """``--state`` + ``--invariants`` (etc.) must fail argparse parsing."""
    err = StringIO()
    with pytest.raises(SystemExit) as exc, redirect_stderr(err):
        main(["show", "report", "--state", "--invariants"])
    # argparse exits 2 on a usage error; that's distinct from our
    # EXIT_STRUCTURAL_FAILURE (1) but the test cares only that we did
    # not silently accept both.
    assert exc.value.code == 2
    assert "not allowed with" in err.getvalue()


# ─── Lock timeout → exit 1, no sentinel ────────────────────────────────────


def test_show_lock_timeout_exits_1(monkeypatch, isolated_xdg, capsys):
    class _BoomLock:
        def __enter__(self):
            raise LockTimeoutError(
                paths.global_lock_file(),
                {
                    "pid": 999,
                    "started_at": "1970-01-01T00:00:00+00:00",
                    "subcommand": "dispatch",
                    "alive": False,
                },
            )

        def __exit__(self, *a):  # pragma: no cover — never reached
            return False

    def _fake_global_lock(*a, **kw):
        return _BoomLock()

    monkeypatch.setattr(
        "gitbulk.commands.show.global_lock", _fake_global_lock
    )
    rc = show_handler(_make_args(show_subcommand="report"))
    assert rc == EXIT_STRUCTURAL_FAILURE
    err = capsys.readouterr().err
    assert "timed out" in err


# ─── End-to-end through main() ─────────────────────────────────────────────


def test_show_via_main_subcommand_default_summary(report_run):
    out = StringIO()
    with redirect_stdout(out):
        rc = main(["show", "report"])
    assert rc == EXIT_OK
    assert "gitbulk report" in out.getvalue()


def test_show_via_main_no_arg_dashboard(isolated_xdg):
    dash = paths.dashboard_file()
    dash.parent.mkdir(parents=True, exist_ok=True)
    dash.write_text("# DASH-MAIN\n")
    out = StringIO()
    with redirect_stdout(out):
        rc = main(["show"])
    assert rc == EXIT_OK
    assert "DASH-MAIN" in out.getvalue()


def test_show_via_main_path_flag(report_run):
    out = StringIO()
    with redirect_stdout(out):
        rc = main(["show", "report", "--path"])
    assert rc == EXIT_OK
    assert str(report_run) in out.getvalue()
