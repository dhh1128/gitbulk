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


def clear_if_matches(subcommand: str, runid: str) -> dict[str, Any] | None:
    """Clear the sentinel iff it was set by the run identified by
    ``(subcommand, runid)``; return the cleared payload, else None.

    Per node ``aklr5pq3`` trigger 1: ``gitbulk show <sub>`` calls this so
    viewing the exact run that raised the alert dismisses it. A sentinel
    set by a DIFFERENT subcommand, or whose recorded runid differs, is left
    intact (clip7nm4's cross-subcommand concern). The ``"?"`` fallback
    runid written by :func:`gitbulk.cli._maybe_set_attention` never matches
    — those still require an explicit ``ack``.
    """
    payload = parse_attention()
    if payload is None:
        return None
    if payload.get("subcommand") != subcommand:
        return None
    sentinel_runid = payload.get("runid")
    if not sentinel_runid or sentinel_runid == "?" or sentinel_runid != runid:
        return None
    clear_attention()
    return payload


def clear_if_superseded(subcommand: str) -> dict[str, Any] | None:
    """Clear the sentinel iff it was set by an earlier run of the SAME
    subcommand; return the cleared payload, else None.

    Per node ``aklr5pq3`` trigger 3: a clean (exit 0) run of an
    attention-producing subcommand supersedes its own stale sentinel — the
    condition that raised the alert has resolved. A sentinel set by a
    different subcommand is left intact (a clean ``report`` must not dismiss
    a ``dispatch`` failure). The caller is responsible for only invoking
    this on a 0-exit.
    """
    payload = parse_attention()
    if payload is None:
        return None
    if payload.get("subcommand") != subcommand:
        return None
    clear_attention()
    return payload


def clear_and_describe() -> dict[str, Any] | None:
    """Clear whatever parseable sentinel is present; return its payload,
    else None.

    Per node ``aklr5pq3`` trigger 2: the bare ``gitbulk show`` dashboard
    aggregates every subcommand's latest-run summary, so viewing it is the
    broad "I looked" gesture that dismisses any outstanding attention. An
    unparseable/legacy sentinel is left for ``ack`` (we cannot describe what
    we cannot parse), matching the defensive treatment in
    :func:`parse_attention`.
    """
    payload = parse_attention()
    if payload is not None:
        clear_attention()
    return payload


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
