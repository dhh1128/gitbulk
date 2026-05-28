"""Per-run audit trail for gitbulk.

A :class:`RunState` owns one ``~/.cache/gitbulk/runs/<runid>-<subcommand>/``
directory and is the single place each subcommand records its decisions.
See this.i nodes ``tp4kq2nr`` (the 4-layer notification model),
``kp7nw4mq`` (this module's schema and API contract), and ``schv4nrm``
(the schema-versioning convention applied to every artifact).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from gitbulk import __version__, paths

#: Schema version stamped onto every artifact this module writes.
#: Bump (and document a corresponding decision node in ``this.i``) when
#: a breaking change to manifest.yaml / state.yaml / invariants.log /
#: errors.log shape lands.
SCHEMA_VERSION = 1

_VALID_INVARIANT_RESULTS = {"PASS", "SKIP", "FAIL"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically via .tmp + rename."""
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _atomic_write_symlink(symlink_path: Path, target: Path) -> None:
    """Create or replace a symlink atomically. The link target is stored
    as a path relative to the symlink's parent directory so the cache
    tree can be relocated without breaking symlinks."""
    tmp = symlink_path.parent / (symlink_path.name + ".tmp")
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    relative_target = os.path.relpath(target, start=symlink_path.parent)
    tmp.symlink_to(relative_target)
    os.replace(tmp, symlink_path)


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

    @classmethod
    def begin(
        cls,
        subcommand: str,
        argv: list[str],
        config_snapshot: dict[str, Any],
        *,
        when: datetime | None = None,
    ) -> "RunState":
        runid = paths.new_runid(when)
        run_dir = paths.run_dir(runid, subcommand)
        run_dir.mkdir(parents=True, exist_ok=False)

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
        full_state = {
            "schema_version": SCHEMA_VERSION,
            "repos": dict(self._per_repo),
        }
        _atomic_write_text(
            self._run_dir / "state.yaml",
            yaml.safe_dump(full_state, sort_keys=False),
        )

    def write_summary(self, markdown: str) -> None:
        _atomic_write_text(self._run_dir / "summary.md", markdown)

    def complete(self, exit_code: int) -> None:
        manifest_path = self._run_dir / "manifest.yaml"
        with manifest_path.open() as f:
            manifest = yaml.safe_load(f)
        manifest["completed_at"] = _utc_now_iso()
        manifest["exit_code"] = exit_code
        _atomic_write_text(manifest_path, yaml.safe_dump(manifest, sort_keys=False))

        symlink_path = paths.latest_run_symlink(self._subcommand)
        _atomic_write_symlink(symlink_path, self._run_dir)
