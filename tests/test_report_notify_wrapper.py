"""Tests for ``bin/gitbulk-report-notify`` (this.i node ``dgcha7vv``).

Same approach as ``test_cron_wrapper.py``: subprocess the *real* script with
a fake ``gitbulk`` binary on disk, so the wrapper is exercised end-to-end
without the network or the real install.

What matters here is exactly one decision — what reaches stdout, because
cron mails stdout and nothing else. The four cases:

* quiet night (no digest written)          -> nothing mailed
* an empty digest file                     -> nothing mailed
* a digest with content                    -> mailed verbatim
* structural failure                       -> the failure line still mailed

The wrapper must stay this dumb. If a future change has it slicing
summary.md or deciding what is worth sending, that judgment has left the
tested Python and these tests no longer cover it.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

WRAPPER = Path(__file__).resolve().parent.parent / "bin" / "gitbulk-report-notify"


def _make_fake_gitbulk(tmp_path: Path, exit_code: int, digest: str | None) -> Path:
    """A stub that optionally writes ``digest.md`` into a run dir the way a
    real ``report`` run would, then exits with ``exit_code``."""
    runs = tmp_path / "cache" / "gitbulk" / "runs"
    run_dir = runs / "20260916T033001Z-report"
    stub = tmp_path / "fake-gitbulk"
    body = ["#!/usr/bin/env bash", 'echo "fake gitbulk args: $*"']
    if digest is not None:
        body.append(f"mkdir -p {run_dir}")
        if digest == "":
            # A genuinely zero-byte file — the truncated/interrupted-write
            # case that `-s` exists to catch. A heredoc cannot express it.
            body.append(f": > {run_dir}/digest.md")
        else:
            body.append(
                f"cat > {run_dir}/digest.md <<'DIGEST_EOF'\n{digest}\nDIGEST_EOF"
            )
        body.append(f"ln -sfn {run_dir} {runs}/latest-report")
    body.append(f"exit {exit_code}")
    stub.write_text("\n".join(body) + "\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def _run(tmp_path: Path, stub: Path) -> subprocess.CompletedProcess:
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", str(WRAPPER)],
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(tmp_path / "home"),
            "XDG_CACHE_HOME": str(cache),
            "GITBULK_BIN": str(stub),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_quiet_night_mails_nothing(tmp_path: Path) -> None:
    """No digest written (nothing waiting on the user) -> no email, even
    though exit 2 means PRs "need attention"."""
    stub = _make_fake_gitbulk(tmp_path, exit_code=2, digest=None)
    proc = _run(tmp_path, stub)
    assert proc.stdout.strip() == ""
    assert proc.returncode == 2


def test_empty_digest_mails_nothing(tmp_path: Path) -> None:
    """``-s`` not ``-f``: a zero-byte digest is still a quiet night."""
    stub = _make_fake_gitbulk(tmp_path, exit_code=2, digest="")
    proc = _run(tmp_path, stub)
    assert proc.stdout.strip() == ""
    assert proc.returncode == 2


def test_digest_is_mailed_verbatim(tmp_path: Path) -> None:
    digest = (
        "# gitbulk: waiting on you\n\n"
        "## Unresolved review threads (1)\n"
        "https://github.com/x/a/pull/7  3 unresolved threads  — Has feedback"
    )
    stub = _make_fake_gitbulk(tmp_path, exit_code=2, digest=digest)
    proc = _run(tmp_path, stub)
    assert "## Unresolved review threads (1)" in proc.stdout
    assert "https://github.com/x/a/pull/7  3 unresolved threads" in proc.stdout
    assert "full report: gitbulk show report" in proc.stdout
    assert proc.returncode == 2


def test_structural_failure_still_mails_the_status_line(tmp_path: Path) -> None:
    """Exit 1 keeps gitbulk-cron's failure-only stdout; the digest channel
    is additive and must not swallow it."""
    stub = _make_fake_gitbulk(tmp_path, exit_code=1, digest=None)
    proc = _run(tmp_path, stub)
    assert "gitbulk-cron: report exit=1" in proc.stdout
    assert proc.returncode == 1


def test_failure_and_digest_both_reach_stdout(tmp_path: Path) -> None:
    stub = _make_fake_gitbulk(tmp_path, exit_code=1, digest="# gitbulk: waiting on you")
    proc = _run(tmp_path, stub)
    assert "gitbulk-cron: report exit=1" in proc.stdout
    assert "# gitbulk: waiting on you" in proc.stdout
    assert proc.returncode == 1
