"""Rewrite ~/.cache/gitbulk/dashboard.md from the latest run per subcommand.

See this.i node ``dwq3kpn4`` for the composition contract and
``tp4kq2nr`` for the broader notification model.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

import yaml

from gitbulk import paths
from gitbulk.cli import SUBCOMMANDS

_EXCERPT_LINES = 15


def _known_subcommands() -> list[str]:
    return [name for name, _ in SUBCOMMANDS]


def _read_yaml_if_present(path: Path) -> dict | None:
    if not path.exists():
        return None
    with path.open() as f:
        return yaml.safe_load(f)


def _excerpt(text: str, max_lines: int = _EXCERPT_LINES) -> tuple[str, bool]:
    """Return (excerpt, truncated_flag)."""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text, False
    return "\n".join(lines[:max_lines]), True


def _render_section(subcommand: str, run_dir: Path | None) -> str:
    header = f"## {subcommand}\n"
    if run_dir is None:
        return header + "\n_no runs yet_\n"
    manifest = _read_yaml_if_present(run_dir / "manifest.yaml") or {}
    runid = run_dir.name.split("-", 1)[0] if "-" in run_dir.name else run_dir.name
    exit_code = manifest.get("exit_code", "?")
    completed_at = manifest.get("completed_at")
    incomplete_tag = " **[INCOMPLETE]**" if completed_at is None else ""
    meta = (
        f"\n- Run: `{runid}`\n"
        f"- Exit: `{exit_code}`{incomplete_tag}\n"
        f"- Completed: `{completed_at or '—'}`\n"
        f"- Dir: `{run_dir}`\n"
    )

    summary_path = run_dir / "summary.md"
    if summary_path.exists():
        body, truncated = _excerpt(summary_path.read_text())
        excerpt_block = "\n```markdown\n" + body + "\n```\n"
        if truncated:
            excerpt_block += f"\n_… truncated; see `{summary_path}`_\n"
    else:
        excerpt_block = "\n_(no summary.md written)_\n"
    return header + meta + excerpt_block


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def rewrite_dashboard(subcommands: Iterable[str] | None = None) -> Path:
    """Rewrite dashboard.md and return its Path."""
    if subcommands is None:
        subcommands = _known_subcommands()
    sections: list[str] = ["# gitbulk dashboard\n"]
    for sub in subcommands:
        symlink = paths.latest_run_symlink(sub)
        if symlink.is_symlink() or symlink.exists():
            try:
                run_dir = symlink.resolve(strict=True)
            except (FileNotFoundError, OSError):
                run_dir = None
        else:
            run_dir = None
        sections.append(_render_section(sub, run_dir))
    out_path = paths.dashboard_file()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(out_path, "\n".join(sections) + "\n")
    return out_path
