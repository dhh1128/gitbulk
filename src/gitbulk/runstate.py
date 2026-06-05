"""Per-run audit trail for gitbulk.

A :class:`RunState` owns one ``~/.cache/gitbulk/runs/<runid>-<subcommand>/``
directory and is the single place each subcommand records its decisions.
See this.i nodes ``tp4kq2nr`` (the 4-layer notification model),
``kp7nw4mq`` (this module's schema and API contract), and ``schv4nrm``
(the schema-versioning convention applied to every artifact).
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from gitbulk import __version__, paths
from gitbulk.util import atomicio

#: Safety cap on runid-collision retries in :meth:`RunState.begin`. A genuine
#: collision needs two same-subcommand runs in the same UTC second; the cap
#: only guards against a pathological loop and is far above any real value.
_RUNID_COLLISION_LIMIT = 1000

#: Schema version stamped onto every artifact this module writes.
#: Bump (and document a corresponding decision node in ``this.i``) when
#: a breaking change to manifest.yaml / state.yaml / invariants.log /
#: errors.log shape lands.
SCHEMA_VERSION = 1

_VALID_INVARIANT_RESULTS = {"PASS", "SKIP", "FAIL"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (unique tmp + rename).

    Thin alias for :func:`gitbulk.util.atomicio.atomic_write_text`; kept as
    the module's internal vocabulary and for the per-run callers below.
    """
    atomicio.atomic_write_text(path, text)


def _atomic_write_symlink(symlink_path: Path, target: Path) -> None:
    """Create or replace a symlink atomically (unique tmp + rename).

    Thin alias for :func:`gitbulk.util.atomicio.atomic_write_symlink`.
    """
    atomicio.atomic_write_symlink(symlink_path, target)


def _append_jsonl(path: Path, event: dict[str, Any]) -> None:
    line = json.dumps(event) + "\n"
    with path.open("a") as f:
        f.write(line)


class RunState:
    """Owns one per-run directory and exposes the recording API."""

    def __init__(self, run_dir: Path, subcommand: str) -> None:
        self._run_dir = run_dir
        self._subcommand = subcommand
        self._per_repo: dict[str, dict[str, Any]] = {}
        self._extras: dict[str, Any] = {}

    @classmethod
    def begin(
        cls,
        subcommand: str,
        argv: list[str],
        config_snapshot: dict[str, Any],
        *,
        when: datetime | None = None,
    ) -> "RunState":
        # runid is a UTC timestamp to the second (node 3pw7qkn2). Two runs of
        # the SAME subcommand started in the same second would collide on the
        # run-dir name; mkdir(exist_ok=False) would then crash the second run.
        # A lock cannot fix this (both processes compute the same runid), so on
        # collision we advance the timestamp by one second and retry. This keeps
        # every runid a valid strptime-able timestamp — so gc's `-<sub>` suffix
        # match, the lexicographic-is-chronological sort, and every downstream
        # `_runid_from_run_dir` parser keep working (node rsclk7nq, Phase 0).
        base = when if when is not None else datetime.now(timezone.utc)
        for offset in range(_RUNID_COLLISION_LIMIT):
            runid = paths.new_runid(base + timedelta(seconds=offset))
            run_dir = paths.run_dir(runid, subcommand)
            try:
                run_dir.mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                continue
        else:
            raise FileExistsError(
                f"could not allocate a unique run dir for {subcommand!r} after "
                f"{_RUNID_COLLISION_LIMIT} attempts starting at {base.isoformat()}"
            )

        # Initial empty state.yaml so a crash before any record_repo_state
        # still leaves a parseable file.
        _atomic_write_text(
            run_dir / "state.yaml",
            yaml.safe_dump({"schema_version": SCHEMA_VERSION, "repos": {}}),
        )

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "gitbulk_version": __version__,
            "subcommand": subcommand,
            "argv": list(argv),
            "started_at": _utc_now_iso(),
            "config_snapshot": config_snapshot,
        }
        _atomic_write_text(
            run_dir / "manifest.yaml",
            yaml.safe_dump(manifest, sort_keys=False),
        )

        return cls(run_dir, subcommand)

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    def record_invariant(
        self,
        name: str,
        target: str,
        result: str,
        reason: str | None = None,
    ) -> None:
        if result not in _VALID_INVARIANT_RESULTS:
            raise ValueError(
                f"invalid invariant result {result!r}; "
                f"expected one of {sorted(_VALID_INVARIANT_RESULTS)}"
            )
        event = {
            "v": SCHEMA_VERSION,
            "ts": _utc_now_iso(),
            "name": name,
            "target": target,
            "result": result,
            "reason": reason,
        }
        _append_jsonl(self._run_dir / "invariants.log", event)

    def record_error(
        self,
        message: str,
        *,
        level: str = "ERROR",
        context: dict[str, Any] | None = None,
    ) -> None:
        event = {
            "v": SCHEMA_VERSION,
            "ts": _utc_now_iso(),
            "level": level,
            "message": message,
            "context": context if context is not None else {},
        }
        _append_jsonl(self._run_dir / "errors.log", event)

    def record_repo_state(self, slug: str, payload: dict[str, Any]) -> None:
        self._per_repo[slug] = payload
        self._rewrite_state()

    def set_repos(self, repos: dict[str, dict[str, Any]]) -> None:
        """Replace the entire per-repo map in a single state.yaml write.

        Bulk alternative to N :meth:`record_repo_state` calls: a run that
        records a whole carried-forward plan (node ``prnpl3kq``) would
        otherwise rewrite the growing state file once per repo — O(n²).
        ``record_extra`` values are preserved. The input is deep-copied so a
        later caller mutation can't reach the persisted state (a subsequent
        ``record_extra`` re-dumps ``_per_repo``)."""
        self._per_repo = copy.deepcopy(repos)
        self._rewrite_state()

    def record_extra(self, key: str, value: Any) -> None:
        """Add or replace a top-level key in state.yaml besides ``repos``.

        Used for cross-cutting findings that don't fit the per-repo
        ``repos`` map — e.g. ``report``'s ``recent_merges`` watchdog
        records. Repeated calls with the same ``key`` overwrite.
        Reserved keys (``schema_version``, ``repos``) raise ValueError
        to keep the file shape predictable.
        """
        if key in {"schema_version", "repos"}:
            raise ValueError(f"reserved state.yaml key: {key!r}")
        self._extras[key] = value
        self._rewrite_state()

    def _rewrite_state(self) -> None:
        full_state: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "repos": dict(self._per_repo),
        }
        full_state.update(self._extras)
        _atomic_write_text(
            self._run_dir / "state.yaml",
            yaml.safe_dump(full_state, sort_keys=False),
        )

    def write_summary(self, markdown: str) -> None:
        _atomic_write_text(self._run_dir / "summary.md", markdown)

    def complete(self, exit_code: int, *, retain_runs: int | None = None) -> None:
        """Finalize the run.

        Writes ``completed_at`` and ``exit_code`` into ``manifest.yaml``,
        atomically points ``latest-<subcommand>`` at this run, and (if
        ``retain_runs`` is provided) prunes older runs of the same
        subcommand beyond that count. Per Track A of this.i tension
        jw3kpn4q, callers in Phase 2+ pass the value from
        ``policy.defaults.retain_runs``.
        """
        manifest_path = self._run_dir / "manifest.yaml"
        with manifest_path.open() as f:
            manifest = yaml.safe_load(f)
        manifest["completed_at"] = _utc_now_iso()
        manifest["exit_code"] = exit_code
        _atomic_write_text(manifest_path, yaml.safe_dump(manifest, sort_keys=False))

        symlink_path = paths.latest_run_symlink(self._subcommand)
        _atomic_write_symlink(symlink_path, self._run_dir)

        if retain_runs is not None:
            # Local import to avoid a cycle: gc imports paths; runstate also
            # imports paths; gc must not import runstate.
            from gitbulk import gc

            gc.prune_runs(self._subcommand, retain=retain_runs)
