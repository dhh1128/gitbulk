"""ATTENTION sentinel file management.

See this.i node ``snk7p4qm`` for the API contract and
``tp4kq2nr`` for the broader 4-layer notification model.
"""

from __future__ import annotations

from gitbulk import paths


def set_attention(exit_code: int, subcommand: str, runid: str, summary: str) -> None:
    """Create or overwrite the ATTENTION sentinel with a single-line summary."""
    line = f"{exit_code} {subcommand} {runid} {summary}\n"
    paths.attention_sentinel().write_text(line)


def clear_attention() -> bool:
    """Remove the ATTENTION sentinel. Returns True if a file was removed,
    False if it was already absent. Never raises for the missing-file case."""
    sentinel = paths.attention_sentinel()
    if sentinel.exists():
        sentinel.unlink()
        return True
    return False


def has_attention() -> bool:
    return paths.attention_sentinel().exists()


def read_attention() -> str | None:
    sentinel = paths.attention_sentinel()
    if not sentinel.exists():
        return None
    return sentinel.read_text()
