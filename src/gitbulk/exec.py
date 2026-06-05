"""In-tree parallel claude execution kernel for ``gitbulk dispatch``.

Per this.i node ``execk7nm`` (resolves tension mp7kn4qz), this module
reimplements the bounded-parallel claude executor that
``../origin-platform/scripts/multiprompt.py`` provides for its own use
case. The in-tree implementation lets gitbulk's dispatch subcommand
ship without taking a cross-repo dependency.

Surface:

  - :class:`ExecTarget` — one unit of work (typically one PR).
  - :class:`ExecResult` — terminal record per target.
  - :func:`execute_targets` — runs the bounded pool, returns results
    in input order.

Design decisions worth re-reading in ``execk7nm``:

  1. **Subprocess.Popen directly, not ClaudeClient.run_prompt.** The
     parallel path needs to hold a live process handle to escalate
     SIGTERM → SIGKILL on per-target timeout, and to signal every
     in-flight child on second-CTRL+C hard-kill. The
     :class:`gitbulk.claude.ClaudeClient` Protocol exposes only a
     blocking ``run_prompt``; adding ``cancel()`` would couple every
     implementation to cancellation machinery it doesn't otherwise
     need. The kernel reads ``claude_path``/``default_model`` from the
     :class:`~gitbulk.claude.ClaudeClient` passed in so the argv shape
     stays consistent with the rest of the codebase, but does its own
     Popen.

  2. **Test seam.** ``_popen_factory`` is a documented internal seam
     for tests; the production default is :class:`subprocess.Popen`.
     Tests pass a fake Popen-like that records argv and lets the test
     drive exit/timing without spawning a real process. This honors
     AGENTS.md "no network in tests" without warping the API for
     production callers.

  3. **CTRL+C drain.** First SIGINT during a run stops dequeuing new
     targets; in-flight workers run to completion (subject to their
     own per-target timeout). Second SIGINT within 10s sends SIGTERM
     to every in-flight child, waits 5s, then SIGKILL to survivors —
     and remaining queued targets are recorded as ``interrupted``.

  4. **Resume out of scope.** Single-shot; if interrupted, the user
     re-invokes ``gitbulk dispatch`` (cheap because the invariant
     filter re-applies).
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Protocol, runtime_checkable

import yaml

from gitbulk.claude import ClaudeClient

#: Terminal status of an :class:`ExecResult`.
ExecStatus = Literal["completed", "failed", "timed-out", "interrupted"]

# Time (seconds) between the first SIGINT (drain) and the second SIGINT
# (hard kill). A second SIGINT later than this re-arms the drain rather
# than escalating, on the theory that the user has had time to reflect.
_HARD_KILL_WINDOW_SECONDS = 10.0

# Time (seconds) we wait after SIGTERM before sending SIGKILL.
_TERM_TO_KILL_GRACE_SECONDS = 5.0

# Loop tick for the timeout monitor (small enough that timing tests
# don't have to sleep long, large enough not to spin).
_MONITOR_TICK_SECONDS = 0.05


@dataclass(frozen=True)
class ExecTarget:
    """One unit of work for :func:`execute_targets`.

    Attributes:
        key: stable identifier used in log paths and progress events
            (e.g., ``"owner__repo__pr42"``). Must be filesystem-safe;
            the kernel does not sanitize it.
        working_directory: cwd for the claude subprocess (typically a
            disposable worktree under
            :func:`gitbulk.paths.default_worktree_root`).
        prompt: the claude prompt text, already templated for this
            target.
        input_text: optional stdin content for claude (e.g., a PR
            diff). ``None`` means no stdin.
    """

    key: str
    working_directory: Path
    prompt: str
    input_text: str | None = None


@dataclass(frozen=True)
class ExecResult:
    """Terminal record for one :class:`ExecTarget`.

    The kernel returns one of these per input target, in input order,
    after :func:`execute_targets` has joined every worker.

    ``exit_code`` is ``None`` for ``timed-out`` and ``interrupted``
    statuses (no return code was observed). ``stdout_path`` and
    ``stderr_path`` always point at the files the kernel created in
    ``log_dir``, even when capture is partial (e.g., the child was
    killed mid-write); a follow-up summary tool can inspect them.
    """

    key: str
    status: ExecStatus
    exit_code: int | None
    stdout_path: Path
    stderr_path: Path
    started_at: datetime
    finished_at: datetime
    duration_seconds: float


# ─── Internal: Popen-like seam ─────────────────────────────────────────────

@runtime_checkable
class _PopenLike(Protocol):
    """Minimum surface :func:`execute_targets` needs from a process
    object. Exists as a type only; tests pass a fake that records the
    argv and lets the test drive exit timing."""

    @property
    def returncode(self) -> int | None: ...  # pragma: no cover

    def poll(self) -> int | None: ...  # pragma: no cover

    def wait(self, timeout: float | None = None) -> int: ...  # pragma: no cover

    def send_signal(self, sig: int) -> None: ...  # pragma: no cover

    def kill(self) -> None: ...  # pragma: no cover

    def communicate(
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[str, str]: ...  # pragma: no cover


PopenFactory = Callable[..., _PopenLike]


# ─── Argv construction ─────────────────────────────────────────────────────


def _claude_argv(claude: ClaudeClient, prompt: str, model: str | None) -> list[str]:
    """Build the claude argv for one target.

    Reads ``_claude_path`` and ``_default_model`` off the
    :class:`~gitbulk.claude.ClaudeClient` if it exposes them (both the
    production and fake clients do as of Phase 3). Falls back to
    ``"claude"`` and the kernel-call-site default otherwise so a
    minimal user-supplied implementation still works.

    Mirrors :class:`gitbulk.claude.ProductionClaudeClient.run_prompt`
    argv shape — see that class for the deprecation-verification note.
    """
    claude_path = getattr(claude, "_claude_path", "claude")
    effective_model = (
        model
        if model is not None
        else getattr(claude, "_default_model", "claude-sonnet-4-6")
    )
    return [
        claude_path,
        "-p",
        prompt,
        "--model",
        effective_model,
        "--dangerously-skip-permissions",
    ]


# ─── Internal worker state ─────────────────────────────────────────────────


@dataclass
class _RunCtx:
    """Shared mutable state for one :func:`execute_targets` call.

    Owned by the orchestrator; workers and the SIGINT handler observe
    it. The lock guards ``active`` only — every other field is either
    immutable after construction or atomic enough on its own.
    """

    stop_event: threading.Event = field(default_factory=threading.Event)
    first_sigint_at: float = 0.0
    active: dict[str, _PopenLike] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


def _install_sigint_handler(ctx: _RunCtx) -> Any:
    """Install the drain/hard-kill SIGINT handler. Returns the old
    handler for the caller to restore in a ``finally``."""

    def handler(signum, frame):  # pragma: no cover — exercised via os.kill in tests
        now = time.monotonic()
        if ctx.stop_event.is_set() and (now - ctx.first_sigint_at) < _HARD_KILL_WINDOW_SECONDS:
            # Second SIGINT inside the window → hard kill in-flight.
            with ctx.lock:
                procs = list(ctx.active.values())
            for proc in procs:
                try:
                    proc.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass
            # Allow children up to _TERM_TO_KILL_GRACE_SECONDS to clean
            # up, then escalate. Done off-thread so the signal handler
            # returns promptly.
            threading.Thread(
                target=_hard_kill_after_grace, args=(ctx,), daemon=True
            ).start()
        else:
            ctx.first_sigint_at = now
            ctx.stop_event.set()

    old = signal.signal(signal.SIGINT, handler)
    return old


def _hard_kill_after_grace(ctx: _RunCtx) -> None:  # pragma: no cover — timing
    time.sleep(_TERM_TO_KILL_GRACE_SECONDS)
    with ctx.lock:
        procs = list(ctx.active.values())
    for proc in procs:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


# ─── Log file helpers ──────────────────────────────────────────────────────


def _log_paths(log_dir: Path, key: str) -> tuple[Path, Path, Path]:
    return (
        log_dir / f"{key}.stdout.log",
        log_dir / f"{key}.stderr.log",
        log_dir / f"{key}.meta.yaml",
    )


def _redact_argv(argv: list[str], prompt: str) -> list[str]:
    """Return ``argv`` with the (possibly large/sensitive) prompt replaced by a
    length placeholder, for the audit record (SEC-F5).

    Redacts the prompt as a SUBSTRING of each token, not just whole-token
    matches: a custom template may embed ``{prompt}`` inside a larger token
    (e.g. ``--prompt={prompt}``), and the full prompt must not survive there
    either. An empty prompt is a no-op (``str.replace("")`` would otherwise
    splice the placeholder between every character)."""
    if not prompt:
        return list(argv)
    placeholder = f"<prompt:{len(prompt)} chars>"
    return [a.replace(prompt, placeholder) for a in argv]


def _write_meta(
    meta_path: Path,
    *,
    key: str,
    status: ExecStatus,
    exit_code: int | None,
    started_at: datetime,
    finished_at: datetime,
    duration_seconds: float,
    timed_out: bool,
    agent_argv: list[str] | None = None,
    agent_env_keys: list[str] | None = None,
) -> None:
    payload = {
        "key": key,
        "status": status,
        "exit_code": exit_code,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": round(duration_seconds, 6),
        "timed_out": timed_out,
        # SEC-F5: persist the EFFECTIVE agent invocation (prompt elided) so the
        # granted authority — which binary, sandbox wrapper, and which env vars
        # — is auditable after the fact. ``agent_env_keys`` is the sorted list
        # of env-var NAMES (never values); ``null`` means the full parent
        # environment was inherited.
        "agent_argv": agent_argv,
        "agent_env_keys": agent_env_keys,
    }
    meta_path.write_text(yaml.safe_dump(payload, sort_keys=False))


# ─── The per-target worker ─────────────────────────────────────────────────


def _run_one(
    target: ExecTarget,
    *,
    claude: ClaudeClient,
    log_dir: Path,
    timeout_per_target: float,
    model: str | None,
    popen_factory: PopenFactory,
    ctx: _RunCtx,
    on_progress: Callable[[str, str], None] | None,
) -> ExecResult:
    """Run one target. Honors ``ctx.stop_event`` for the drain case.

    If the stop event fires BEFORE we launch, we record an
    ``interrupted`` result without ever spawning a child. If the stop
    event fires AFTER launch (i.e., first CTRL+C while we're running),
    we let the child run to completion subject to its own timeout —
    only a second CTRL+C escalates to signal-the-child.
    """
    stdout_path, stderr_path, meta_path = _log_paths(log_dir, target.key)
    started_at = datetime.now(timezone.utc)
    start_mono = time.monotonic()

    if ctx.stop_event.is_set():
        # Don't even start; record as interrupted.
        stdout_path.write_text("")
        stderr_path.write_text("")
        finished_at = datetime.now(timezone.utc)
        duration = time.monotonic() - start_mono
        _write_meta(
            meta_path,
            key=target.key,
            status="interrupted",
            exit_code=None,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration,
            timed_out=False,
        )
        if on_progress is not None:
            on_progress(target.key, "interrupted")
        return ExecResult(
            key=target.key,
            status="interrupted",
            exit_code=None,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration,
        )

    if on_progress is not None:
        on_progress(target.key, "running")

    # Source the argv (and stdin/env) from the backend's launch plan when it
    # exposes one (both built-in clients do). A minimal backend with only
    # ``run_prompt`` falls back to the legacy claude-shaped argv builder so a
    # user-supplied implementation still works (see this.i ``agbknd7q`` /
    # ``execk7nm``). The plan also carries the exact stdin bytes (so
    # ``prompt_via: stdin`` agents work) and the scoped ``env`` (agenv6q).
    plan = getattr(claude, "plan", None)
    if callable(plan):
        inv = plan(
            target.prompt,
            input_text=target.input_text,
            model=model,
            working_directory=target.working_directory,
            timeout=timeout_per_target,
        )
        argv = inv.argv
        stdin_payload = inv.stdin_data
        use_stdin = inv.use_stdin
        env = inv.env
    else:
        argv = _claude_argv(claude, target.prompt, model)
        stdin_payload = target.input_text
        use_stdin = target.input_text is not None
        env = None
    # SEC-F5 audit record: the effective argv (prompt elided) + env-var names.
    audit_argv = _redact_argv(argv, target.prompt)
    audit_env_keys = sorted(env.keys()) if env is not None else None

    # Children write directly to the log files; we never buffer
    # gigabytes of triage output in memory.
    stdout_fh = stdout_path.open("w")
    stderr_fh = stderr_path.open("w")

    # Pass ``env`` only when scoped (not None) so the legacy/inherit path and
    # the existing test popen-factories — whose signatures omit ``env`` — are
    # unaffected; a scoped env is opt-in per profile (agenv6q).
    extra_kw: dict[str, Any] = {} if env is None else {"env": env}

    proc: _PopenLike | None = None
    try:
        proc = popen_factory(
            argv,
            cwd=str(target.working_directory),
            stdin=subprocess.PIPE if use_stdin else None,
            stdout=stdout_fh,
            stderr=stderr_fh,
            text=True,
            **extra_kw,
        )
    except Exception as exc:
        # popen_factory raised — record as failed without ever
        # registering the process. This is the "claude binary not
        # found" path in production.
        stdout_fh.close()
        stderr_fh.close()
        stderr_path.write_text(f"failed to launch: {exc}\n")
        finished_at = datetime.now(timezone.utc)
        duration = time.monotonic() - start_mono
        _write_meta(
            meta_path,
            key=target.key,
            status="failed",
            exit_code=None,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration,
            timed_out=False,
            agent_argv=audit_argv,
            agent_env_keys=audit_env_keys,
        )
        if on_progress is not None:
            on_progress(target.key, "failed")
        return ExecResult(
            key=target.key,
            status="failed",
            exit_code=None,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration,
        )

    with ctx.lock:
        ctx.active[target.key] = proc

    timed_out = False
    try:
        if use_stdin:
            # Feed stdin without blocking on a deadlocked child: use a
            # background thread to write, then poll for completion.
            stdin_thread = threading.Thread(
                target=_feed_stdin, args=(proc, stdin_payload or ""), daemon=True
            )
            stdin_thread.start()

        deadline = start_mono + timeout_per_target
        while True:
            if proc.poll() is not None:
                break
            if time.monotonic() >= deadline:
                timed_out = True
                try:
                    proc.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass
                # Wait up to grace seconds for clean exit.
                kill_deadline = time.monotonic() + _TERM_TO_KILL_GRACE_SECONDS
                while time.monotonic() < kill_deadline:
                    if proc.poll() is not None:
                        break
                    time.sleep(_MONITOR_TICK_SECONDS)
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        proc.wait(timeout=_TERM_TO_KILL_GRACE_SECONDS)
                    except Exception:
                        pass
                break
            time.sleep(_MONITOR_TICK_SECONDS)
    finally:
        with ctx.lock:
            ctx.active.pop(target.key, None)
        stdout_fh.close()
        stderr_fh.close()

    exit_code = proc.returncode
    finished_at = datetime.now(timezone.utc)
    duration = time.monotonic() - start_mono

    if timed_out:
        status: ExecStatus = "timed-out"
        reported_exit: int | None = None
    elif exit_code == 0:
        status = "completed"
        reported_exit = exit_code
    else:
        status = "failed"
        reported_exit = exit_code

    _write_meta(
        meta_path,
        key=target.key,
        status=status,
        exit_code=reported_exit,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=duration,
        timed_out=timed_out,
        agent_argv=audit_argv,
        agent_env_keys=audit_env_keys,
    )
    if on_progress is not None:
        on_progress(target.key, status)

    return ExecResult(
        key=target.key,
        status=status,
        exit_code=reported_exit,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=duration,
    )


def _feed_stdin(proc: _PopenLike, text: str) -> None:
    """Write ``text`` to ``proc.stdin`` then close. Used by
    :func:`_run_one` when the target carries an ``input_text``."""
    try:
        proc.communicate(input=text)
    except Exception:
        # The child may have died before we could feed it; that path
        # is observed via ``proc.poll()`` in the monitor loop and
        # surfaced as ``failed`` via the non-zero exit code.
        pass


# ─── Public entrypoint ─────────────────────────────────────────────────────


def execute_targets(
    targets: list[ExecTarget],
    *,
    claude: ClaudeClient,
    log_dir: Path,
    concurrency: int = 2,
    timeout_per_target: float = 600.0,
    model: str | None = None,
    backends: dict[str, ClaudeClient] | None = None,
    on_progress: Callable[[str, str], None] | None = None,
    _popen_factory: PopenFactory | None = None,
) -> list[ExecResult]:
    """Run claude prompts in parallel against ``targets``.

    Returns one :class:`ExecResult` per input target, in input order,
    after every worker has finished (or been signaled).

    Parameters:
        targets: input list. Empty → empty result list.
        claude: the :class:`~gitbulk.claude.ClaudeClient` whose
            ``_claude_path``/``_default_model`` shape the argv. The
            kernel does NOT call ``claude.run_prompt`` on the parallel
            path (see module docstring).
        log_dir: directory under which per-target ``<key>.stdout.log``
            / ``<key>.stderr.log`` / ``<key>.meta.yaml`` are written.
            Created if absent.
        concurrency: bounded worker pool size. Per CLAUDE.md the
            user's machine tolerates at most ~2 concurrent claude
            children; default 2.
        timeout_per_target: per-target wall-clock limit before
            SIGTERM → wait → SIGKILL escalation.
        model: claude model override; ``None`` uses the client's
            default.
        backends: optional per-target backend map keyed by ``ExecTarget.key``.
            When a target's key is present, that backend is used instead of
            ``claude`` — this is how dispatch honors a per-repo ``agent:``
            override (this.i ``agprof4k``). Absent keys fall back to ``claude``.
        on_progress: optional ``(key, status)`` callback. Statuses
            observed: ``running`` (when work starts) and one of
            ``completed`` / ``failed`` / ``timed-out`` / ``interrupted``
            (when work finishes). Called from worker threads; the
            callback is responsible for its own thread-safety.

    Internal seam:
        ``_popen_factory`` swaps :class:`subprocess.Popen` for a fake
        in tests. Production callers leave it as ``None``.
    """
    if not targets:
        return []

    log_dir.mkdir(parents=True, exist_ok=True)
    popen_factory: PopenFactory = (
        _popen_factory if _popen_factory is not None else subprocess.Popen
    )

    ctx = _RunCtx()
    old_handler = _install_sigint_handler(ctx)

    # Pre-allocate result slots so we can write into them in input
    # order regardless of completion order.
    results: list[ExecResult | None] = [None] * len(targets)

    try:
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            future_to_index = {
                pool.submit(
                    _run_one,
                    target,
                    claude=(
                        backends.get(target.key, claude)
                        if backends
                        else claude
                    ),
                    log_dir=log_dir,
                    timeout_per_target=timeout_per_target,
                    model=model,
                    popen_factory=popen_factory,
                    ctx=ctx,
                    on_progress=on_progress,
                ): idx
                for idx, target in enumerate(targets)
            }
            for future in future_to_index:
                idx = future_to_index[future]
                results[idx] = future.result()
    finally:
        signal.signal(signal.SIGINT, old_handler)

    # Every slot must be filled by now; the cast is safe.
    return [r for r in results if r is not None]


# ─── Convenience helpers ───────────────────────────────────────────────────


def trigger_drain() -> None:
    """Send SIGINT to the current process.

    Public helper for tests and for higher-level orchestrators that
    want to drain a running :func:`execute_targets` programmatically
    (e.g., a watchdog that decided the run should stop). The signal
    goes through the same handler the kernel installed, so the drain
    semantics are identical to a user-typed CTRL+C.
    """
    os.kill(os.getpid(), signal.SIGINT)


__all__ = [
    "ExecResult",
    "ExecStatus",
    "ExecTarget",
    "execute_targets",
    "trigger_drain",
]
