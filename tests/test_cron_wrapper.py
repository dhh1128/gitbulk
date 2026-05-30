"""Tests for the cron wrapper ``bin/gitbulk-cron``.

These tests subprocess the *real* wrapper script with a fake ``gitbulk``
binary on disk (a tiny shell stub) so we exercise the wrapper end-to-end
without touching the network or the real gitbulk install.

The wrapper is the only shell in the repo (see AGENTS.md). It must:

* write a self-describing preamble to the LOG before running gitbulk,
* append the inner gitbulk output after the preamble (not truncate it),
* end the log with a ``gitbulk-cron: <subcmd> exit=<rc> log=<path>`` line,
* create the correct exit-code-aware ``last-*.log`` symlink, and
* exit with the inner gitbulk return code.

No network: the fake stub only echoes and exits. ``GITBULK_BIN`` points at
the stub so binary resolution is deterministic and PATH is left untouched
by the wrapper (so the wrapper's own coreutils/git stay on the test PATH).
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

WRAPPER = Path(__file__).resolve().parent.parent / "bin" / "gitbulk-cron"


def _make_fake_gitbulk(tmp_path: Path, exit_code: int, marker: str) -> Path:
    """Write an executable shell stub that echoes a marker and exits."""
    stub = tmp_path / "fake-gitbulk"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "{marker} args: $*"\n'
        f'echo "{marker} stderr line" >&2\n'
        f"exit {exit_code}\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def _run_wrapper(tmp_path: Path, stub: Path, *args: str) -> subprocess.CompletedProcess:
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    env = {
        # Minimal, cron-like but with real coreutils/git available.
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "XDG_CACHE_HOME": str(cache),
        "GITBULK_BIN": str(stub),
    }
    (tmp_path / "home").mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", str(WRAPPER), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _log_dir(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "gitbulk" / "cron"


def _only_timestamped_log(tmp_path: Path) -> Path:
    logs = [
        p
        for p in _log_dir(tmp_path).iterdir()
        if p.is_file() and not p.is_symlink() and p.suffix == ".log"
    ]
    assert len(logs) == 1, f"expected exactly one timestamped log, got {logs}"
    return logs[0]


def test_preamble_contains_binary_argv_and_path(tmp_path: Path) -> None:
    stub = _make_fake_gitbulk(tmp_path, exit_code=0, marker="FAKEOUT")
    result = _run_wrapper(tmp_path, stub, "report", "--flag", "value")

    assert result.returncode == 0
    log_text = _only_timestamped_log(tmp_path).read_text()

    # Banner so the preamble is easy to skip past.
    assert "gitbulk-cron preamble" in log_text
    # Resolved binary path (the stub, via GITBULK_BIN).
    assert str(stub) in log_text
    # The full argv / subcommand.
    assert "report --flag value" in log_text
    # PATH as the wrapper saw it.
    assert "/usr/local/bin:/usr/bin:/bin" in log_text
    # Inner gitbulk output appended AFTER the preamble (not truncated away).
    assert "FAKEOUT args: report --flag value" in log_text
    assert "FAKEOUT stderr line" in log_text


def test_log_ends_with_exit_line_matching_code(tmp_path: Path) -> None:
    stub = _make_fake_gitbulk(tmp_path, exit_code=2, marker="FAKEOUT")
    result = _run_wrapper(tmp_path, stub, "report")

    log_lines = _only_timestamped_log(tmp_path).read_text().splitlines()
    last = log_lines[-1]
    assert "gitbulk-cron:" in last
    assert "report" in last
    assert "exit=2" in last


def test_attention_symlink_for_exit_2(tmp_path: Path) -> None:
    stub = _make_fake_gitbulk(tmp_path, exit_code=2, marker="X")
    _run_wrapper(tmp_path, stub, "report")

    target = _only_timestamped_log(tmp_path)
    link = _log_dir(tmp_path) / "last-attention.log"
    assert link.is_symlink()
    assert os.path.realpath(link) == str(target)
    # No failure/audit symlink for an attention exit.
    assert not (_log_dir(tmp_path) / "last-failure.log").exists()
    assert not (_log_dir(tmp_path) / "last-audit.log").exists()


@pytest.mark.parametrize(
    "code,linkname",
    [
        (0, None),
        (1, "last-failure.log"),
        (2, "last-attention.log"),
        (3, "last-attention.log"),
        (4, "last-audit.log"),
        (99, None),
        (7, "last-failure.log"),  # catch-all
    ],
)
def test_symlink_mapping_preserved(tmp_path: Path, code: int, linkname) -> None:
    stub = _make_fake_gitbulk(tmp_path, exit_code=code, marker="X")
    result = _run_wrapper(tmp_path, stub, "report")

    # Process exit code must equal the inner gitbulk rc (MAILTO depends on it).
    assert result.returncode == code

    if linkname is None:
        for name in ("last-failure.log", "last-attention.log", "last-audit.log"):
            assert not (_log_dir(tmp_path) / name).exists(), name
    else:
        link = _log_dir(tmp_path) / linkname
        assert link.is_symlink()
        assert os.path.realpath(link) == str(_only_timestamped_log(tmp_path))


def test_exit_code_is_inner_rc(tmp_path: Path) -> None:
    stub = _make_fake_gitbulk(tmp_path, exit_code=4, marker="X")
    result = _run_wrapper(tmp_path, stub, "report")
    assert result.returncode == 4
    # exit 4 is audit-only, not a structural failure: the status line is
    # recorded in the log but NOT echoed to stdout, so cron's MAILTO stays
    # silent on it (node shkd5crn).
    assert "exit=4" not in result.stdout
    assert "exit=4" in _only_timestamped_log(tmp_path).read_text()


@pytest.mark.parametrize(
    "code,emails",
    [
        (0, False),   # clean: silent
        (1, True),    # structural failure: email
        (2, False),   # PRs need attention: sentinel handles it, no email
        (3, False),   # repos skipped: sentinel handles it, no email
        (4, False),   # override audit: no email
        (99, False),  # subcommand not implemented: no email
        (7, True),    # unexpected/catch-all: email
    ],
)
def test_stdout_emits_only_on_structural_failure(
    tmp_path: Path, code: int, emails: bool
) -> None:
    """cron mails whatever a job writes to stdout, so the wrapper writes the
    status line to stdout ONLY for exit 1 and unexpected codes — making MAILTO
    a failure-only channel. The status line is ALWAYS recorded in the log
    regardless of exit code (node shkd5crn / tp4kq2nr)."""
    stub = _make_fake_gitbulk(tmp_path, exit_code=code, marker="X")
    result = _run_wrapper(tmp_path, stub, "report")

    assert result.returncode == code
    on_stdout = "gitbulk-cron:" in result.stdout and f"exit={code}" in result.stdout
    assert on_stdout is emails
    # Self-describing log holds the status line whatever the exit code.
    assert f"exit={code}" in _only_timestamped_log(tmp_path).read_text()
