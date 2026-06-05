"""Tests for the in-tree parallel claude execution kernel.

Per AGENTS.md "no network in tests" / "no subprocess.run in tests":
every test in this file injects a fake Popen-like object via the
documented internal ``_popen_factory`` seam of
:func:`gitbulk.exec.execute_targets`. No real ``claude`` invocation
happens.

The fake ``_FakePopen`` simulates the lifecycle of a real Popen
sufficient for the kernel: ``poll()`` returns ``None`` until the test
script asks it to finish, ``send_signal``/``kill`` are recorded for
later assertion, and ``returncode`` is set on completion.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path

import pytest
import yaml

from gitbulk import exec as exec_mod
from gitbulk.claude import FakeClaudeClient
from gitbulk.exec import ExecTarget, execute_targets, trigger_drain


# ─── Fake Popen ────────────────────────────────────────────────────────────


class _FakePopen:
    """Popen-like double for the exec kernel.

    A test creates a :class:`_FakeRunnerScript` which produces one of
    these per child. The test drives completion via :meth:`finish`
    (which sets ``returncode`` and closes any captured stdio).
    """

    def __init__(
        self,
        argv: list[str],
        *,
        cwd: str | None,
        stdout,
        stderr,
        exit_after: float | None = None,
        exit_code: int = 0,
        stdout_text: str = "",
        stderr_text: str = "",
        hang: bool = False,
        raise_on_init: Exception | None = None,
        on_complete=None,
    ) -> None:
        if raise_on_init is not None:
            raise raise_on_init
        self.argv = argv
        self.cwd = cwd
        self._stdout_fh = stdout
        self._stderr_fh = stderr
        self._stdout_text = stdout_text
        self._stderr_text = stderr_text
        self._exit_after = exit_after
        self._exit_code = exit_code
        self._hang = hang
        self._signals: list[int] = []
        self._killed = False
        self._start = time.monotonic()
        self._lock = threading.Lock()
        self._returncode: int | None = None
        self._stdin_text: str | None = None
        # Called exactly once, synchronously, when this popen transitions
        # from "running" to "complete". Used by _FakeRunnerScript to
        # decrement its live-count atomically with the same lock the
        # bounded-concurrency assertion reads. Replaces the older drain-
        # thread approach which raced under CI scheduling (2026-05-28).
        self._on_complete = on_complete
        self._completed_signaled = False

    @property
    def returncode(self) -> int | None:
        return self._returncode

    @property
    def signals_received(self) -> list[int]:
        return list(self._signals)

    @property
    def killed(self) -> bool:
        return self._killed

    @property
    def stdin_text(self) -> str | None:
        return self._stdin_text

    def poll(self) -> int | None:
        with self._lock:
            if self._returncode is not None:
                return self._returncode
            if self._hang:
                return None
            if self._exit_after is None:
                # Default: complete immediately on first poll.
                self._finish_locked()
                return self._returncode
            if time.monotonic() - self._start >= self._exit_after:
                self._finish_locked()
                return self._returncode
            return None

    def _finish_locked(self) -> None:
        # Write the canned output, then close-out.
        try:
            self._stdout_fh.write(self._stdout_text)
            self._stderr_fh.write(self._stderr_text)
        except Exception:  # pragma: no cover — stdio already closed
            pass
        self._returncode = self._exit_code
        # Signal on_complete exactly once, under _lock, so the live-count
        # decrement happens synchronously with the returncode transition.
        if self._on_complete is not None and not self._completed_signaled:
            self._completed_signaled = True
            self._on_complete(self)

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            rc = self.poll()
            if rc is not None:
                return rc
            if deadline is not None and time.monotonic() >= deadline:
                # Mimic subprocess.TimeoutExpired by returning rc=None
                # (the kernel does not check return value of wait).
                return -1  # pragma: no cover
            time.sleep(0.01)

    def send_signal(self, sig: int) -> None:
        with self._lock:
            self._signals.append(sig)
            if sig == signal.SIGTERM:
                # Cooperative children honor SIGTERM promptly.
                self._hang = False
                self._exit_after = 0
                self._exit_code = 143  # 128 + SIGTERM
                # Note: don't finish here; let the kernel observe via
                # next poll, which keeps the timing realistic.

    def kill(self) -> None:
        with self._lock:
            self._killed = True
            self._hang = False
            self._exit_code = 137  # 128 + SIGKILL
            self._returncode = self._exit_code
            # SIGKILL path also marks completion — same single-signal rule.
            if self._on_complete is not None and not self._completed_signaled:
                self._completed_signaled = True
                self._on_complete(self)

    def communicate(
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[str, str]:
        self._stdin_text = input
        rc = self.wait(timeout=timeout)
        del rc
        return "", ""


class _FakeRunnerScript:
    """Per-test scripting for :class:`_FakePopen` instances.

    Maps a target ``key`` (extracted from argv tail or from a counter)
    to a dict of kwargs that the next :class:`_FakePopen` should be
    constructed with. The kernel doesn't know about the script; it
    just sees a popen_factory callable.
    """

    def __init__(self) -> None:
        self.by_prompt: dict[str, dict] = {}
        self.default: dict = {"exit_code": 0, "stdout_text": "ok\n"}
        self.created: list[_FakePopen] = []
        self.live_lock = threading.Lock()
        self.live: list[_FakePopen] = []
        self.peak_live: int = 0
        # Per-prompt barrier: when set for a prompt, the FakePopen for
        # that prompt waits on it before transitioning to "complete".
        self.barriers: dict[str, threading.Event] = {}

    def __call__(self, argv, *, cwd, stdin, stdout, stderr, text):
        del stdin, text
        # The prompt is argv[2] because argv is [claude, -p, <prompt>, ...]
        prompt = argv[2] if len(argv) >= 3 else ""
        cfg = self.by_prompt.get(prompt, self.default)
        # If the test wants this one to fail at launch, raise.
        if "raise_on_init" in cfg:
            raise cfg["raise_on_init"]
        # If a barrier is set for this prompt, install hang+release.
        barrier = self.barriers.get(prompt)

        def _on_complete(p: "_FakePopen") -> None:
            # Called synchronously by _FakePopen when its returncode
            # transitions from None → set. Decrements live atomically
            # with the same lock the bounded-concurrency assertion reads.
            with self.live_lock:
                if p in self.live:
                    self.live.remove(p)

        popen = _FakePopen(
            argv,
            cwd=cwd,
            stdout=stdout,
            stderr=stderr,
            exit_after=cfg.get("exit_after"),
            exit_code=cfg.get("exit_code", 0),
            stdout_text=cfg.get("stdout_text", ""),
            stderr_text=cfg.get("stderr_text", ""),
            hang=cfg.get("hang", barrier is not None),
            on_complete=_on_complete,
        )
        self.created.append(popen)
        # Track live count for the bounded-concurrency assertion. We hold
        # the lock around BOTH the append and the peak read so a parallel
        # _on_complete cannot interleave a decrement between them.
        with self.live_lock:
            self.live.append(popen)
            self.peak_live = max(self.peak_live, len(self.live))
        if barrier is not None:
            # Spawn a tiny watcher that flips the popen once the
            # barrier is released. We don't block here because the
            # kernel's monitor loop is the caller.
            def _release_when_set(p=popen, b=barrier, cfg=cfg):
                b.wait()
                p._hang = False
                p._exit_after = 0
                p._exit_code = cfg.get("exit_code", 0)
            threading.Thread(target=_release_when_set, daemon=True).start()
        return popen


# ─── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def claude_fake() -> FakeClaudeClient:
    return FakeClaudeClient({"": "unused — kernel uses Popen path"})


@pytest.fixture
def tmp_log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "logs"
    return d


@pytest.fixture
def tmp_wd(tmp_path: Path) -> Path:
    d = tmp_path / "wd"
    d.mkdir()
    return d


# ─── Tests ─────────────────────────────────────────────────────────────────


def test_empty_targets_returns_empty_list(claude_fake, tmp_log_dir):
    results = execute_targets(
        [],
        claude=claude_fake,
        log_dir=tmp_log_dir,
        _popen_factory=_FakeRunnerScript(),
    )
    assert results == []


def test_happy_path_three_targets_all_complete(claude_fake, tmp_log_dir, tmp_wd):
    script = _FakeRunnerScript()
    script.default = {"exit_code": 0, "stdout_text": "ok\n"}
    targets = [
        ExecTarget(key=f"t{i}", working_directory=tmp_wd, prompt=f"p{i}")
        for i in range(3)
    ]
    results = execute_targets(
        targets,
        claude=claude_fake,
        log_dir=tmp_log_dir,
        concurrency=2,
        _popen_factory=script,
    )
    assert [r.key for r in results] == ["t0", "t1", "t2"]
    assert all(r.status == "completed" for r in results)
    assert all(r.exit_code == 0 for r in results)
    # Logs landed in the right place.
    for r in results:
        assert r.stdout_path.exists()
        assert r.stderr_path.exists()
        assert r.stdout_path.read_text() == "ok\n"
        meta = yaml.safe_load((tmp_log_dir / f"{r.key}.meta.yaml").read_text())
        assert meta["status"] == "completed"
        assert meta["exit_code"] == 0
        assert meta["timed_out"] is False


def test_redact_argv_whole_token_and_embedded():
    """SEC-F5: the prompt is redacted whether it's a standalone token OR
    embedded in a larger token (a custom template may use --prompt={prompt})."""
    from gitbulk.exec import _redact_argv

    # Standalone token (the built-in preset shape).
    assert _redact_argv(["tool", "-p", "SECRET"], "SECRET") == [
        "tool", "-p", "<prompt:6 chars>"
    ]
    # Embedded in a longer token — must still be redacted.
    assert _redact_argv(["tool", "--prompt=SECRET", "x"], "SECRET") == [
        "tool", "--prompt=<prompt:6 chars>", "x"
    ]


def test_redact_argv_empty_prompt_is_noop():
    from gitbulk.exec import _redact_argv

    assert _redact_argv(["a", "b"], "") == ["a", "b"]


def test_meta_records_redacted_agent_argv_env_none(claude_fake, tmp_log_dir, tmp_wd):
    """SEC-F5: meta.yaml records the effective argv with the prompt elided and
    env-keys null when the full environment is inherited."""
    script = _FakeRunnerScript()
    script.default = {"exit_code": 0, "stdout_text": "ok\n"}
    execute_targets(
        [ExecTarget(key="t", working_directory=tmp_wd, prompt="SECRET PROMPT")],
        claude=claude_fake,
        log_dir=tmp_log_dir,
        _popen_factory=script,
    )
    meta = yaml.safe_load((tmp_log_dir / "t.meta.yaml").read_text())
    assert "SECRET PROMPT" not in meta["agent_argv"]
    assert "<prompt:13 chars>" in meta["agent_argv"]
    assert meta["agent_argv"][0] == "claude"
    assert meta["agent_env_keys"] is None  # FakeClaudeClient.plan inherits


def test_meta_records_scoped_env_keys(tmp_log_dir, tmp_wd):
    """SEC-F5: when a backend scopes the env, meta records the var NAMES only."""

    class _ScopedBackend:
        def plan(self, prompt, **kwargs):
            from gitbulk.claude import AgentInvocation

            return AgentInvocation(
                argv=["/canonical/agent", "-p", prompt],
                use_stdin=False,
                stdin_data=None,
                env={"AGENT_KEY": "v", "PATH": "/bin"},
                timeout=10.0,
            )

    captured_env = {}

    def factory(argv, *, cwd, stdin, stdout, stderr, text, env=None):
        captured_env["env"] = env
        return _FakePopen(argv, cwd=None, stdout=stdout, stderr=stderr)

    execute_targets(
        [ExecTarget(key="t", working_directory=tmp_wd, prompt="p")],
        claude=_ScopedBackend(),
        log_dir=tmp_log_dir,
        _popen_factory=factory,
    )
    # The scoped env reached the child...
    assert captured_env["env"] == {"AGENT_KEY": "v", "PATH": "/bin"}
    # ...and only the NAMES (sorted) are persisted — never the values.
    meta = yaml.safe_load((tmp_log_dir / "t.meta.yaml").read_text())
    assert meta["agent_env_keys"] == ["AGENT_KEY", "PATH"]


def test_one_target_fails_others_continue(claude_fake, tmp_log_dir, tmp_wd):
    script = _FakeRunnerScript()
    script.by_prompt["bad"] = {"exit_code": 1, "stderr_text": "boom\n"}
    targets = [
        ExecTarget("ok1", tmp_wd, "good1"),
        ExecTarget("bad1", tmp_wd, "bad"),
        ExecTarget("ok2", tmp_wd, "good2"),
    ]
    results = execute_targets(
        targets,
        claude=claude_fake,
        log_dir=tmp_log_dir,
        _popen_factory=script,
    )
    by_key = {r.key: r for r in results}
    assert by_key["ok1"].status == "completed"
    assert by_key["bad1"].status == "failed"
    assert by_key["bad1"].exit_code == 1
    assert by_key["ok2"].status == "completed"
    assert by_key["bad1"].stderr_path.read_text() == "boom\n"


def test_target_with_input_text_gets_stdin(claude_fake, tmp_log_dir, tmp_wd):
    script = _FakeRunnerScript()
    targets = [ExecTarget("t", tmp_wd, "p", input_text="hello-stdin")]
    results = execute_targets(
        targets, claude=claude_fake, log_dir=tmp_log_dir, _popen_factory=script
    )
    assert results[0].status == "completed"
    # The fake popen records what was fed in.
    assert script.created[0].stdin_text == "hello-stdin"


def test_popen_launch_failure_is_failed_status(claude_fake, tmp_log_dir, tmp_wd):
    script = _FakeRunnerScript()
    script.by_prompt["launchfail"] = {"raise_on_init": FileNotFoundError("no claude")}
    targets = [ExecTarget("t", tmp_wd, "launchfail")]
    results = execute_targets(
        targets, claude=claude_fake, log_dir=tmp_log_dir, _popen_factory=script
    )
    assert results[0].status == "failed"
    assert results[0].exit_code is None
    assert "failed to launch" in results[0].stderr_path.read_text()


def test_timeout_escalates_sigterm_then_continues(
    claude_fake, tmp_log_dir, tmp_wd, monkeypatch
):
    # Speed up the grace window so the test runs quickly.
    monkeypatch.setattr(exec_mod, "_TERM_TO_KILL_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(exec_mod, "_MONITOR_TICK_SECONDS", 0.01)
    script = _FakeRunnerScript()
    script.by_prompt["hang"] = {"hang": True}
    targets = [ExecTarget("t", tmp_wd, "hang")]
    results = execute_targets(
        targets,
        claude=claude_fake,
        log_dir=tmp_log_dir,
        timeout_per_target=0.1,
        _popen_factory=script,
    )
    assert results[0].status == "timed-out"
    assert results[0].exit_code is None
    # The fake popen recorded a SIGTERM signal from the kernel.
    assert signal.SIGTERM in script.created[0].signals_received


def test_timeout_uncooperative_child_gets_sigkill(
    claude_fake, tmp_log_dir, tmp_wd, monkeypatch
):
    monkeypatch.setattr(exec_mod, "_TERM_TO_KILL_GRACE_SECONDS", 0.02)
    monkeypatch.setattr(exec_mod, "_MONITOR_TICK_SECONDS", 0.005)

    # A truly uncooperative fake: ignores SIGTERM, only kill() finishes.
    class _StubbornFake(_FakePopen):
        def send_signal(self, sig):
            # Record it but DON'T transition to exit.
            self._signals.append(sig)

    # Override the factory inline so we control the class.
    created: list[_StubbornFake] = []

    def factory(argv, *, cwd, stdin, stdout, stderr, text):
        del stdin, text
        p = _StubbornFake(argv, cwd=cwd, stdout=stdout, stderr=stderr, hang=True)
        created.append(p)
        return p

    targets = [ExecTarget("t", tmp_wd, "p")]
    results = execute_targets(
        targets,
        claude=claude_fake,
        log_dir=tmp_log_dir,
        timeout_per_target=0.05,
        _popen_factory=factory,
    )
    assert results[0].status == "timed-out"
    assert created[0].killed is True


def test_bounded_concurrency_never_exceeds_limit(claude_fake, tmp_log_dir, tmp_wd):
    script = _FakeRunnerScript()
    # Each target hangs on its own barrier so the kernel must hold
    # workers at the pool limit while we inspect peak_live.
    barriers = []
    targets = []
    for i in range(5):
        prompt = f"p{i}"
        ev = threading.Event()
        barriers.append(ev)
        script.barriers[prompt] = ev
        targets.append(ExecTarget(f"t{i}", tmp_wd, prompt))

    # Release barriers in a background thread so the kernel can drain.
    def release_all():
        # Wait until at least the pool size of children exist.
        time.sleep(0.1)
        for ev in barriers:
            ev.set()
            time.sleep(0.05)

    threading.Thread(target=release_all, daemon=True).start()
    results = execute_targets(
        targets,
        claude=claude_fake,
        log_dir=tmp_log_dir,
        concurrency=2,
        _popen_factory=script,
    )
    assert len(results) == 5
    assert script.peak_live <= 2, f"peak_live={script.peak_live}"


def test_on_progress_fires_per_target(claude_fake, tmp_log_dir, tmp_wd):
    script = _FakeRunnerScript()
    events: list[tuple[str, str]] = []
    lock = threading.Lock()

    def on_progress(key, status):
        with lock:
            events.append((key, status))

    targets = [
        ExecTarget("a", tmp_wd, "pa"),
        ExecTarget("b", tmp_wd, "pb"),
    ]
    results = execute_targets(
        targets,
        claude=claude_fake,
        log_dir=tmp_log_dir,
        _popen_factory=script,
        on_progress=on_progress,
    )
    keys_seen = {k for k, _ in events}
    assert keys_seen == {"a", "b"}
    statuses_seen = {(k, s) for k, s in events}
    assert ("a", "running") in statuses_seen
    assert ("a", "completed") in statuses_seen
    assert ("b", "running") in statuses_seen
    assert ("b", "completed") in statuses_seen
    assert all(r.status == "completed" for r in results)


def test_argv_picks_up_default_model_and_path(tmp_log_dir, tmp_wd):
    """The kernel must read _claude_path and _default_model from the
    injected ClaudeClient so production and test argv stay aligned."""
    fake = FakeClaudeClient({"": "x"})
    fake._claude_path = "/usr/local/bin/claude"  # type: ignore[attr-defined]
    fake._default_model = "claude-test-model"  # type: ignore[attr-defined]
    captured: list[list[str]] = []

    def factory(argv, *, cwd, stdin, stdout, stderr, text):
        del stdin, text, cwd
        captured.append(list(argv))
        return _FakePopen(argv, cwd=None, stdout=stdout, stderr=stderr)

    execute_targets(
        [ExecTarget("k", tmp_wd, "PROMPT-X")],
        claude=fake,
        log_dir=tmp_log_dir,
        _popen_factory=factory,
    )
    assert captured[0] == [
        "/usr/local/bin/claude",
        "-p",
        "PROMPT-X",
        "--model",
        "claude-test-model",
        "--dangerously-skip-permissions",
    ]


def test_argv_falls_back_when_client_has_no_attrs(tmp_log_dir, tmp_wd):
    """Minimal ClaudeClient (no private attrs) → kernel uses safe defaults."""

    class _MinimalClient:
        def run_prompt(self, prompt, **kwargs):  # pragma: no cover
            return ""

    captured: list[list[str]] = []

    def factory(argv, *, cwd, stdin, stdout, stderr, text):
        del stdin, text, cwd
        captured.append(list(argv))
        return _FakePopen(argv, cwd=None, stdout=stdout, stderr=stderr)

    execute_targets(
        [ExecTarget("k", tmp_wd, "PROMPT-Y")],
        claude=_MinimalClient(),  # type: ignore[arg-type]
        log_dir=tmp_log_dir,
        _popen_factory=factory,
    )
    assert captured[0][0] == "claude"
    # The model slot should be the kernel default.
    assert captured[0][4] == "claude-sonnet-4-6"


def test_explicit_model_overrides_client_default(tmp_log_dir, tmp_wd, claude_fake):
    captured: list[list[str]] = []

    def factory(argv, *, cwd, stdin, stdout, stderr, text):
        del stdin, text, cwd
        captured.append(list(argv))
        return _FakePopen(argv, cwd=None, stdout=stdout, stderr=stderr)

    execute_targets(
        [ExecTarget("k", tmp_wd, "p")],
        claude=claude_fake,
        log_dir=tmp_log_dir,
        model="my-custom-model",
        _popen_factory=factory,
    )
    assert captured[0][4] == "my-custom-model"


def test_working_directory_honored(claude_fake, tmp_log_dir, tmp_path):
    wd = tmp_path / "specific-wd"
    wd.mkdir()
    captured: dict = {}

    def factory(argv, *, cwd, stdin, stdout, stderr, text):
        del stdin, text
        captured["cwd"] = cwd
        return _FakePopen(argv, cwd=cwd, stdout=stdout, stderr=stderr)

    execute_targets(
        [ExecTarget("k", wd, "p")],
        claude=claude_fake,
        log_dir=tmp_log_dir,
        _popen_factory=factory,
    )
    assert captured["cwd"] == str(wd)


# ─── CTRL+C / drain semantics ──────────────────────────────────────────────


def test_sigint_drains_remaining_queued_targets(
    claude_fake, tmp_log_dir, tmp_wd, monkeypatch
):
    """First SIGINT during a run sets stop_event. New workers picking up
    queued targets see it and emit ``interrupted`` results without ever
    launching a child."""
    monkeypatch.setattr(exec_mod, "_MONITOR_TICK_SECONDS", 0.005)
    script = _FakeRunnerScript()

    # Capture the run's _RunCtx so the signaler can synchronise on stop_event
    # ACTUALLY being set by the handler, instead of betting on a fixed sleep
    # (the bet raced the async signal delivery and made this test flaky).
    # _install_sigint_handler already receives the ctx, so wrapping it is a
    # zero-production-change seam.
    captured: dict = {}
    _real_install = exec_mod._install_sigint_handler

    def _capturing_install(ctx):
        captured["ctx"] = ctx
        return _real_install(ctx)

    monkeypatch.setattr(exec_mod, "_install_sigint_handler", _capturing_install)

    # Target 0 hangs on a barrier so it's still in-flight when we
    # signal. Targets 1 and 2 are queued and should become interrupted.
    barrier = threading.Event()
    script.barriers["hang-me"] = barrier
    targets = [
        ExecTarget("t0", tmp_wd, "hang-me"),
        ExecTarget("t1", tmp_wd, "p1"),
        ExecTarget("t2", tmp_wd, "p2"),
    ]

    # Fire SIGINT once the first worker is in-flight, then release t0 only
    # after stop_event is set — so t1/t2 are guaranteed to be dequeued with it
    # already set (concurrency=1 keeps the worker blocked on t0 until then).
    def signaler():
        # Wait for the first FakePopen AND the captured ctx (both exist well
        # before t0 finishes, since the worker is blocked on the barrier).
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not (
            script.created and "ctx" in captured
        ):
            time.sleep(0.001)
        # Send SIGINT to ourselves → the handler sets stop_event.
        os.kill(os.getpid(), signal.SIGINT)
        # Deterministic gate: only release the in-flight worker once the
        # handler has observably set stop_event.
        assert captured["ctx"].stop_event.wait(timeout=5.0)
        barrier.set()

    threading.Thread(target=signaler, daemon=True).start()
    results = execute_targets(
        targets,
        claude=claude_fake,
        log_dir=tmp_log_dir,
        concurrency=1,  # serial so t1/t2 are clearly queued
        _popen_factory=script,
    )
    by_key = {r.key: r for r in results}
    # The first target launched before the signal → completed.
    assert by_key["t0"].status == "completed"
    # The queued targets saw stop_event and recorded interrupted.
    assert by_key["t1"].status == "interrupted"
    assert by_key["t2"].status == "interrupted"
    assert by_key["t1"].exit_code is None


def test_second_sigint_within_window_signals_active_children(
    claude_fake, tmp_log_dir, tmp_wd, monkeypatch
):
    """Second SIGINT within the hard-kill window sends SIGTERM to every
    in-flight child."""
    monkeypatch.setattr(exec_mod, "_MONITOR_TICK_SECONDS", 0.005)
    monkeypatch.setattr(exec_mod, "_TERM_TO_KILL_GRACE_SECONDS", 0.05)
    script = _FakeRunnerScript()
    barrier = threading.Event()
    script.barriers["hang-me"] = barrier
    targets = [ExecTarget("t0", tmp_wd, "hang-me")]

    def signaler():
        for _ in range(200):
            if script.created:
                break
            time.sleep(0.005)
        # First SIGINT: drain.
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(0.01)
        # Second SIGINT inside the window: hard kill.
        os.kill(os.getpid(), signal.SIGINT)
        # Give the hard-kill thread time to send SIGTERM.
        time.sleep(0.1)
        # Release the barrier so the worker can finish in case it
        # didn't already.
        barrier.set()

    threading.Thread(target=signaler, daemon=True).start()
    results = execute_targets(
        targets,
        claude=claude_fake,
        log_dir=tmp_log_dir,
        concurrency=1,
        _popen_factory=script,
    )
    assert len(results) == 1
    # The child got SIGTERM as part of the second-SIGINT hard kill.
    sigs = script.created[0].signals_received
    assert signal.SIGTERM in sigs


def test_trigger_drain_helper_invokes_sigint(monkeypatch):
    """The helper sends SIGINT to the current PID."""
    sent: list[tuple[int, int]] = []

    def fake_kill(pid, sig):
        sent.append((pid, sig))

    monkeypatch.setattr(exec_mod.os, "kill", fake_kill)
    trigger_drain()
    assert sent == [(os.getpid(), signal.SIGINT)]


def test_dataclasses_are_frozen():
    """Frozen-ness keeps ExecTarget/ExecResult hashable and prevents
    in-place mutation after results land in run state."""
    t = ExecTarget(key="k", working_directory=Path("/tmp"), prompt="p")
    with pytest.raises(Exception):
        t.key = "other"  # type: ignore[misc]


# ─── Old SIGINT handler is restored ───────────────────────────────────────


def test_on_progress_called_for_interrupted_status(
    claude_fake, tmp_log_dir, tmp_wd
):
    """The interrupted branch must fire on_progress too — otherwise a
    drained run would silently swallow the status update."""
    script = _FakeRunnerScript()
    barrier = threading.Event()
    script.barriers["hang"] = barrier
    events: list[tuple[str, str]] = []
    lock = threading.Lock()

    def on_progress(key, status):
        with lock:
            events.append((key, status))

    def signaler():
        for _ in range(200):
            if script.created:
                break
            time.sleep(0.005)
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(0.05)
        barrier.set()

    threading.Thread(target=signaler, daemon=True).start()
    execute_targets(
        [
            ExecTarget("t0", tmp_wd, "hang"),
            ExecTarget("t1", tmp_wd, "p1"),
        ],
        claude=claude_fake,
        log_dir=tmp_log_dir,
        concurrency=1,
        _popen_factory=script,
        on_progress=on_progress,
    )
    assert ("t1", "interrupted") in events


def test_on_progress_called_for_failed_launch(claude_fake, tmp_log_dir, tmp_wd):
    """Failed-to-launch path must also notify the progress callback."""
    script = _FakeRunnerScript()
    script.by_prompt["bad"] = {"raise_on_init": OSError("ENOENT")}
    events: list[tuple[str, str]] = []

    def on_progress(key, status):
        events.append((key, status))

    execute_targets(
        [ExecTarget("t", tmp_wd, "bad")],
        claude=claude_fake,
        log_dir=tmp_log_dir,
        _popen_factory=script,
        on_progress=on_progress,
    )
    assert ("t", "failed") in events


def test_sigterm_swallows_processlookuperror(
    claude_fake, tmp_log_dir, tmp_wd, monkeypatch
):
    """If the child dies between the poll() that found it alive and
    the SIGTERM call, send_signal raises ProcessLookupError. The
    kernel must handle this gracefully."""
    monkeypatch.setattr(exec_mod, "_TERM_TO_KILL_GRACE_SECONDS", 0.02)
    monkeypatch.setattr(exec_mod, "_MONITOR_TICK_SECONDS", 0.005)

    class _RacingFake(_FakePopen):
        def send_signal(self, sig):
            raise ProcessLookupError("vanished")

    def factory(argv, *, cwd, stdin, stdout, stderr, text):
        del stdin, text
        return _RacingFake(argv, cwd=cwd, stdout=stdout, stderr=stderr, hang=True)

    results = execute_targets(
        [ExecTarget("t", tmp_wd, "p")],
        claude=claude_fake,
        log_dir=tmp_log_dir,
        timeout_per_target=0.05,
        _popen_factory=factory,
    )
    assert results[0].status == "timed-out"


def test_kill_swallows_processlookuperror(
    claude_fake, tmp_log_dir, tmp_wd, monkeypatch
):
    """If the child dies between the SIGTERM grace expiry and the
    SIGKILL call, kill() raises ProcessLookupError. Handle gracefully."""
    monkeypatch.setattr(exec_mod, "_TERM_TO_KILL_GRACE_SECONDS", 0.02)
    monkeypatch.setattr(exec_mod, "_MONITOR_TICK_SECONDS", 0.005)

    class _StubbornVanisher(_FakePopen):
        def send_signal(self, sig):
            # Ignore SIGTERM (don't transition); stay alive past grace.
            self._signals.append(sig)

        def kill(self):
            raise ProcessLookupError("vanished before kill")

        def wait(self, timeout=None):
            # Returning to satisfy the kernel's wait() in the kill branch.
            return -1

    def factory(argv, *, cwd, stdin, stdout, stderr, text):
        del stdin, text
        return _StubbornVanisher(
            argv, cwd=cwd, stdout=stdout, stderr=stderr, hang=True
        )

    results = execute_targets(
        [ExecTarget("t", tmp_wd, "p")],
        claude=claude_fake,
        log_dir=tmp_log_dir,
        timeout_per_target=0.03,
        _popen_factory=factory,
    )
    assert results[0].status == "timed-out"


def test_wait_after_kill_swallows_exceptions(
    claude_fake, tmp_log_dir, tmp_wd, monkeypatch
):
    """If wait() after the kill raises (e.g., TimeoutExpired), the
    kernel must continue without propagating."""
    monkeypatch.setattr(exec_mod, "_TERM_TO_KILL_GRACE_SECONDS", 0.02)
    monkeypatch.setattr(exec_mod, "_MONITOR_TICK_SECONDS", 0.005)

    class _WaitRaiser(_FakePopen):
        def send_signal(self, sig):
            self._signals.append(sig)  # ignore — stay alive past grace

        def kill(self):
            # Don't actually finish; force wait() to be called.
            self._signals.append(-1)

        def wait(self, timeout=None):
            raise RuntimeError("wait failed")

    def factory(argv, *, cwd, stdin, stdout, stderr, text):
        del stdin, text
        return _WaitRaiser(argv, cwd=cwd, stdout=stdout, stderr=stderr, hang=True)

    results = execute_targets(
        [ExecTarget("t", tmp_wd, "p")],
        claude=claude_fake,
        log_dir=tmp_log_dir,
        timeout_per_target=0.03,
        _popen_factory=factory,
    )
    assert results[0].status == "timed-out"


def test_feed_stdin_swallows_communicate_failure(
    claude_fake, tmp_log_dir, tmp_wd
):
    """If communicate() raises (e.g., child died mid-write), the
    background stdin thread swallows the error so the run continues."""

    class _StdinBrokenFake(_FakePopen):
        def communicate(self, input=None, timeout=None):
            self._stdin_text = input
            raise BrokenPipeError("child gone")

    def factory(argv, *, cwd, stdin, stdout, stderr, text):
        del stdin, text
        # Exit promptly after the stdin thread tries to feed.
        return _StdinBrokenFake(
            argv, cwd=cwd, stdout=stdout, stderr=stderr, exit_after=0
        )

    results = execute_targets(
        [ExecTarget("t", tmp_wd, "p", input_text="hello")],
        claude=claude_fake,
        log_dir=tmp_log_dir,
        _popen_factory=factory,
    )
    # The target itself completes (exit_after=0 → exit_code=0).
    assert results[0].status == "completed"


def test_old_sigint_handler_is_restored(claude_fake, tmp_log_dir, tmp_wd):
    """After ``execute_targets`` returns, the previous SIGINT handler
    must be in place — otherwise we'd leak our drain handler into the
    parent process (e.g., the test runner)."""
    sentinel = signal.getsignal(signal.SIGINT)
    script = _FakeRunnerScript()
    execute_targets(
        [ExecTarget("k", tmp_wd, "p")],
        claude=claude_fake,
        log_dir=tmp_log_dir,
        _popen_factory=script,
    )
    assert signal.getsignal(signal.SIGINT) == sentinel
