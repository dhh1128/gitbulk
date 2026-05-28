"""ATTENTION sentinel file management.

See this.i node ``snk7p4qm`` for the API contract,
``tp4kq2nr`` for the broader 4-layer notification model, and
``schv4nrm`` for the schema-versioning convention applied to the
on-disk wire format.

As of Phase 1D the sentinel is written as a one-line JSON object
(not the legacy whitespace-delimited format) so external readers
(tmux status integrations, future ``gitbulk show``) get a parseable
structure with explicit version. Pre-Phase-1D readers that grepped
fields out by position will break loudly on the format change — by
design (per the platform-architect adversarial review, 2026-05-27).
"""

from __future__ import annotations

import json
from typing import Any

from gitbulk import paths

#: Schema version stamped onto the ATTENTION sentinel JSON object.
#: Bump (with a corresponding decision node in ``this.i``) on any
#: breaking change to the sentinel's wire format.
SCHEMA_VERSION = 1


def set_attention(exit_code: int, subcommand: str, runid: str, summary: str) -> None:
    """Create or overwrite the ATTENTION sentinel with a one-line JSON object."""
    payload = {
        "v": SCHEMA_VERSION,
        "exit_code": exit_code,
        "subcommand": subcommand,
        "runid": runid,
        "summary": summary,
    }
    paths.attention_sentinel().write_text(json.dumps(payload) + "\n")


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
    """Return the raw sentinel file content, or None if absent.

    Most callers want :func:`parse_attention` instead — this lower-level
    accessor exists for forensic logging and for clients that want to
    handle parsing errors themselves.
    """
    sentinel = paths.attention_sentinel()
    if not sentinel.exists():
        return None
    return sentinel.read_text()


def parse_attention() -> dict[str, Any] | None:
    """Return the parsed sentinel content, or None if absent or unparseable.

    A sentinel file present but containing invalid JSON (e.g. left over from a
    pre-Phase-1D whitespace-format gitbulk) returns None rather than raising,
    matching the defensive treatment in locks.py for lock-file metadata.
    """
    raw = read_attention()
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
